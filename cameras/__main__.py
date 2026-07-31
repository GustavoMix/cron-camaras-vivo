"""Command line entry point.

    python -m cameras build                     # full run into ./data
    python -m cameras build --only 511on        # single source, for development
    python -m cameras list                      # show the registry
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .build import build
from .sources import all_sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cameras", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="fetch every source and write the dataset")
    build_parser.add_argument(
        "--out", type=Path, default=Path("data"), help="output directory (default: ./data)"
    )
    build_parser.add_argument(
        "--only",
        action="append",
        help="restrict the run to this source id; repeatable",
    )
    build_parser.add_argument(
        "--concurrency", type=int, default=8, help="parallel HTTP requests (default: 8)"
    )
    build_parser.add_argument(
        "--include-gated",
        action="store_true",
        help="also run sources whose API key is missing (they will fail; useful for debugging)",
    )
    build_parser.add_argument(
        "--min-cameras",
        type=int,
        default=0,
        help="abort without writing if fewer than this many cameras were collected",
    )
    build_parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="append a Markdown run summary to this file (e.g. $GITHUB_STEP_SUMMARY)",
    )

    subparsers.add_parser("list", help="list registered sources")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "list":
        return _list_sources()
    return _build(args)


def _list_sources() -> int:
    try:
        print(f"{'ID':<14} {'COUNTRY':<8} {'GATED BY':<20} NAME")
        for source in all_sources():
            gate = source.requires_env or "-"
            if source.requires_env and not os.environ.get(source.requires_env, "").strip():
                gate += " (unset)"
            print(f"{source.id:<14} {source.country or '--':<8} {gate:<20} {source.name}")
    except BrokenPipeError:
        # Downstream closed the pipe (`... | head`). Exit quietly instead of
        # letting the interpreter print a traceback at shutdown.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0


def _build(args: argparse.Namespace) -> int:
    try:
        report = asyncio.run(
            build(
                out_dir=args.out,
                only=args.only,
                concurrency=args.concurrency,
                include_gated=args.include_gated,
                min_cameras=args.min_cameras,
            )
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a CLI error
        logging.error("build failed: %s", exc)
        return 1

    logging.info(
        "wrote %d cameras from %d/%d sources (%d duplicates removed)",
        report.total,
        len(report.ok_sources),
        len(report.results),
        report.duplicates,
    )

    if args.summary:
        _write_summary(args.summary, report)

    # A run where every source failed is a failure, even though the pipeline
    # itself completed. Partial success is fine and expected.
    if not report.ok_sources:
        logging.error("every source failed")
        return 1
    return 0


def _write_summary(path: Path, report) -> None:
    """Append a Markdown summary, for the GitHub Actions run page."""
    lines = [
        "## Camera dataset updated",
        "",
        f"- **Cameras:** {report.total:,}",
        f"- **Generated:** `{report.generated_at}`",
        f"- **Duplicates removed:** {report.duplicates:,}",
        f"- **Sources:** {len(report.ok_sources)} ok / {len(report.failed_sources)} failed",
        "",
        "| Source | Status | Cameras | Time |",
        "| --- | --- | ---: | ---: |",
    ]
    for result in sorted(report.results, key=lambda r: (-r.count, r.source_id)):
        status = "ok" if result.ok else "**failed**"
        lines.append(
            f"| `{result.source_id}` | {status} | {result.count:,} | {result.duration_s:.1f}s |"
        )

    if report.failed_sources:
        lines += ["", "<details><summary>Failure details</summary>", ""]
        for result in report.failed_sources:
            lines.append(f"- `{result.source_id}`: {result.error}")
        lines += ["", "</details>"]

    top = sorted(report.by_country.items(), key=lambda kv: -kv[1])[:15]
    if top:
        lines += ["", "**Top countries:** " + ", ".join(f"{c} ({n:,})" for c, n in top)]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
