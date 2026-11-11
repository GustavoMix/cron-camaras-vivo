"""Offline tests for the quality layer, using httpx.MockTransport."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cameras.curation import state as st
from cameras.curation.hls import parse_playlist, pick_variant, tizen_playable
from cameras.curation.pipeline import CurateConfig, curate
from cameras.curation.probe import Prober
from cameras.curation.rights import RightsPolicy

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
hi/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160,CODECS="hvc1.2.4.L153"
uhd/index.m3u8
"""


def media_playlist(seq: int, endlist: bool = False) -> str:
    body = f"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:{seq}\n"
    for i in range(3):
        body += f"#EXTINF:4.0,\nseg{seq + i}.ts\n"
    return body + ("#EXT-X-ENDLIST\n" if endlist else "")


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 6000


def make_transport(*, live: bool = True, advancing: bool = True, down: bool = False):
    calls = {"media": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if down:
            return httpx.Response(503)
        if path.endswith("/master.m3u8"):
            return httpx.Response(200, text=MASTER)
        if path.endswith("/hi/index.m3u8"):
            calls["media"] += 1
            seq = 100 + (calls["media"] - 1 if advancing else 0)
            return httpx.Response(200, text=media_playlist(seq, endlist=not live))
        if path.endswith(".ts"):
            return httpx.Response(206, content=b"\x47" * 100)
        if path.endswith("/tiny.jpg"):
            return httpx.Response(200, content=b"\xff\xd8\xff")
        if path.endswith(".jpg"):
            return httpx.Response(200, content=JPEG)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def probe(url: str, fmt: str | None, deep: bool = False, **kwargs):
    async with httpx.AsyncClient(transport=make_transport(**kwargs)) as client:
        return await Prober(client, deep_wait_s=0).probe(url, fmt, deep=deep)


class TestHls:
    def test_master_variants_and_selection(self):
        playlist = parse_playlist(MASTER, "https://cdn.example/x/master.m3u8")
        assert playlist.is_master and len(playlist.variants) == 3
        best = pick_variant(playlist.variants)
        assert best.height == 1080  # 4K is above the cap
        assert best.url == "https://cdn.example/x/hi/index.m3u8"

    def test_rejects_non_hls(self):
        with pytest.raises(ValueError):
            parse_playlist("<html>nope</html>", "https://x.example/a")

    def test_codec_policy(self):
        assert tizen_playable("avc1.640028,mp4a.40.2")
        assert not tizen_playable("hvc1.2.4.L153,mp4a.40.2")
        assert tizen_playable(None)


class TestProbe:
    async def test_live_hls_ok(self):
        result = await probe("https://cdn.example/master.m3u8", "hls")
        assert result.ok and result.height == 1080 and result.tizen_ok

    async def test_vod_is_rejected(self):
        result = await probe("https://cdn.example/master.m3u8", "hls", live=False)
        assert not result.ok and result.reason == "not_live"

    async def test_frozen_stream_detected_by_deep_probe(self):
        light = await probe("https://cdn.example/master.m3u8", "hls", advancing=False)
        deep = await probe("https://cdn.example/master.m3u8", "hls", deep=True, advancing=False)
        assert light.ok  # a light probe cannot see a frozen playlist
        assert not deep.ok and deep.reason == "stalled"

    async def test_deep_probe_passes_advancing_stream(self):
        assert (await probe("https://cdn.example/master.m3u8", "hls", deep=True)).ok

    async def test_http_failure_has_reason(self):
        result = await probe("https://cdn.example/master.m3u8", "hls", down=True)
        assert not result.ok and result.reason == "http_503"

    async def test_image_ok_and_too_small(self):
        assert (await probe("https://cdn.example/a.jpg", None)).ok
        small = await probe("https://cdn.example/tiny.jpg", None)
        assert not small.ok and small.reason == "too_small"


TODAY = date(2026, 11, 11)


class TestRights:
    def policy(self, **source):
        record = {
            "status": "verified", "commercial_use": True,
            "evidence_url": "https://example.org/terms", "verified_on": "2026-11-01",
            "review_by": "2027-11-01", **source,
        }
        return RightsPolicy({"a": record, "pending": {"status": "pending"}})

    def test_verified_source_passes(self):
        assert self.policy().verdict("a", None, TODAY).ok

    @pytest.mark.parametrize(
        ("override", "reason"),
        [
            ({"commercial_use": None}, "commercial_use_not_confirmed"),
            ({"commercial_use": "yes"}, "commercial_use_not_confirmed"),
            ({"evidence_url": ""}, "no_evidence"),
            ({"verified_on": None}, "no_verification_date"),
            ({"review_by": "2026-01-01"}, "review_overdue"),
        ],
    )
    def test_each_requirement_is_enforced(self, override, reason):
        verdict = self.policy(**override).verdict("a", None, TODAY)
        assert not verdict.ok and verdict.reason == reason

    def test_default_deny(self):
        policy = self.policy()
        assert policy.verdict("pending", None, TODAY).reason == "status_pending"
        assert policy.verdict("who", None, TODAY).reason == "unknown_source"


class TestState:
    NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)

    def entry(self):
        return st.new_entry({"id": "x", "source": "s"}, self.NOW)

    def test_promotion_requires_two_good_probes(self):
        e = self.entry()
        st.apply_probe(e, {}, True, self.NOW)
        assert e["status"] == "candidate"
        st.apply_probe(e, {}, True, self.NOW)
        assert e["status"] == "active"

    def test_one_failure_degrades_three_take_it_offline(self):
        e = self.entry()
        for _ in range(2):
            st.apply_probe(e, {}, True, self.NOW)
        st.apply_probe(e, {}, False, self.NOW)
        assert e["status"] == "degraded" and e["status"] in st.PUBLISHED
        st.apply_probe(e, {}, False, self.NOW)
        st.apply_probe(e, {}, False, self.NOW)
        assert e["status"] == "offline" and e["status"] not in st.PUBLISHED

    def test_degraded_recovers_on_one_good_probe(self):
        e = self.entry()
        for ok in (True, True, False, True):
            st.apply_probe(e, {}, ok, self.NOW)
        assert e["status"] == "active"

    def test_offline_for_a_month_is_retired_not_deleted(self):
        e = self.entry()
        st.apply_probe(e, {}, True, self.NOW)
        later = self.NOW + timedelta(days=40)
        for _ in range(3):
            st.apply_probe(e, {}, False, later)
        assert e["status"] == "retired"

    def test_stable_cameras_are_probed_half_as_often(self):
        e = self.entry()
        for _ in range(8):
            st.apply_probe(e, {}, True, self.NOW)
        assert st.interval(e) == timedelta(hours=24)
        assert not st.is_due(e, self.NOW + timedelta(hours=12))
        assert st.is_due(e, self.NOW + timedelta(hours=24))

    def test_flaky_cameras_are_probed_every_run(self):
        e = self.entry()
        for ok in (True, True, True, False, True, True, True, True):
            st.apply_probe(e, {}, ok, self.NOW)
        assert st.interval(e) == timedelta(0)


# ----------------------------------------------------------------- pipeline


def write_inputs(root: Path, n: int = 12) -> None:
    (root / "policy").mkdir()
    (root / "policy" / "rights.json").write_text(json.dumps({"sources": {}}))
    (root / "curated").mkdir()
    rights = {
        "license": "CC0", "commercial_use": True, "evidence_url": "https://example.org/t",
        "verified_on": "2026-11-01", "review_by": "2027-11-01",
    }
    cameras = [
        {
            "native_id": f"cam{i}", "name": f"Beach {i}", "country": "MV", "city": "Male",
            "lat": 4.1, "lon": 73.5, "kind": "scenic", "tags": ["beach"],
            "stream": "https://cdn.example/master.m3u8",
            "image": f"https://cdn.example/{i}.jpg", "attribution": "Example", "rights": rights,
        }
        for i in range(n)
    ]
    # Same stream URL on every camera would be de-duplicated; give each its own.
    for i, camera in enumerate(cameras):
        camera["stream"] = f"https://cdn.example/c{i}/master.m3u8"
    cameras.append({
        "native_id": "no-rights", "name": "Mystery beach", "kind": "scenic", "tags": ["beach"],
        "image": "https://cdn.example/x.jpg",
    })
    cameras.append({
        "native_id": "road", "name": "Highway", "kind": "traffic", "image": "https://cdn.example/r.jpg",
        "rights": rights,
    })
    (root / "curated" / "cameras.json").write_text(json.dumps({"cameras": cameras}))


_seq = itertools.count(500)


def route_all(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/master.m3u8"):
        return httpx.Response(200, text=MASTER)
    if request.url.path.endswith("/hi/index.m3u8"):
        return httpx.Response(200, text=media_playlist(next(_seq)))
    if request.url.path.endswith(".ts"):
        return httpx.Response(206, content=b"x" * 10)
    return httpx.Response(200, content=JPEG)


def config(root: Path, handler=route_all, **kw) -> CurateConfig:
    return CurateConfig(
        out_dir=root / "data", state_path=root / "state" / "s.json",
        policy_path=root / "policy" / "rights.json", transport=httpx.MockTransport(handler),
        deep_wait_s=0, min_score=40, **kw,
    )


class TestPipeline:
    @pytest.fixture
    def root(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        write_inputs(tmp_path)
        return tmp_path

    async def test_gates_then_promotes_after_two_runs(self, root):
        now = datetime(2026, 11, 11, 12, tzinfo=UTC)
        first = await curate(config(root), now)
        assert first.published == 0  # candidates are not published yet
        assert first.stats["off_theme"] == 1
        assert first.stats["rejected"]["unknown_source"] == 1  # the camera without rights

        second = await curate(config(root), now + timedelta(hours=12))
        assert second.published == 12 and second.changed
        catalog = json.loads((root / "data/v2/catalog.json").read_text())
        names = {c["name"] for c in catalog["cameras"]}
        assert "Mystery beach" not in names and "Highway" not in names
        camera = catalog["cameras"][0]
        assert camera["media"]["type"] == "video" and camera["rights"]["commercial_use"] is True

    async def test_quiet_run_does_not_change_published_files(self, root):
        now = datetime(2026, 11, 11, 12, tzinfo=UTC)
        for hours in (0, 12):
            await curate(config(root), now + timedelta(hours=hours))
        before = (root / "data/v2/catalog.json").read_bytes()
        third = await curate(config(root), now + timedelta(hours=24))
        assert not third.changed
        assert (root / "data/v2/catalog.json").read_bytes() == before

    async def test_outage_does_not_poison_state(self, root):
        now = datetime(2026, 11, 11, 12, tzinfo=UTC)
        for hours in (0, 12):
            await curate(config(root), now + timedelta(hours=hours))
        state_before = (root / "state/s.json").read_text()

        down = await curate(config(root, handler=lambda r: httpx.Response(503)),
                            now + timedelta(hours=24))
        assert down.outage_suspected
        assert (root / "state/s.json").read_text() == state_before
        assert down.published == 12  # nothing was taken off the air

    async def test_probe_budget_defers_work_instead_of_failing(self, root):
        report = await curate(config(root, probe_budget_s=-1), datetime(2026, 11, 11, tzinfo=UTC))
        assert report.stats["probed"] == 0 and report.stats["skipped_budget"] == 12

    async def test_drop_guard_refuses_catastrophic_shrink(self, root):
        now = datetime(2026, 11, 11, 12, tzinfo=UTC)
        for hours in (0, 12):
            await curate(config(root), now + timedelta(hours=hours))
        # Rights lapse for everything (review date passes) -> would publish 0.
        late = now + timedelta(days=400)
        report = await curate(config(root), late)
        assert report.refused
        assert json.loads((root / "data/v2/manifest.json").read_text())["count"] == 12
