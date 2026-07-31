"""Offline tests for normalization, de-duplication and output writing.

Nothing here touches the network: adapters are exercised against captured
response shapes so the suite stays fast and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cameras.build import deduplicate, write_dataset
from cameras.models import make_camera
from cameras.sources import all_sources
from cameras.sources.base import SourceResult
from cameras.sources.caltrans import Caltrans
from cameras.sources.castlerock import Castle511
from cameras.sources.nyctmc import NycTmc
from cameras.sources.tfl import TflJamCams
from cameras.util import guess_stream_format, normalize_url, stable_url_key


def camera(**overrides):
    defaults = {
        "source": "test",
        "native_id": "1",
        "name": "Test camera",
        "country": "US",
        "lat": 40.0,
        "lon": -73.0,
        "image": "https://example.com/a.jpg",
    }
    return make_camera(**{**defaults, **overrides})


class TestNormalization:
    def test_rejects_camera_without_media(self):
        assert camera(image=None, stream=None) is None

    def test_rejects_unusable_native_id(self):
        assert camera(native_id="") is None
        assert camera(native_id=None) is None

    def test_id_is_stable_and_source_scoped(self):
        assert camera().id == camera().id
        assert camera(source="a").id != camera(source="b").id

    def test_out_of_range_coordinates_are_dropped_not_clamped(self):
        result = camera(lat=91.0, lon=-73.0)
        assert result.lat is None and result.lon is None
        assert "lat" not in result.to_json()

    def test_null_island_is_treated_as_missing(self):
        result = camera(lat=0.0, lon=0.0)
        assert result.lat is None

    def test_string_coordinates_are_parsed(self):
        result = camera(lat="38.6", lon="-121.5")
        assert result.lat == pytest.approx(38.6)

    def test_stream_holding_a_still_image_is_reclassified(self):
        result = camera(image=None, stream="https://example.com/snap.jpg")
        assert result.image == "https://example.com/snap.jpg"
        assert result.stream is None

    def test_image_field_holding_a_playlist_is_reclassified(self):
        result = camera(image="https://example.com/live.m3u8", stream=None)
        assert result.stream == "https://example.com/live.m3u8"
        assert result.stream_format == "hls"
        assert result.image is None

    def test_protocol_relative_url_is_made_absolute(self):
        assert normalize_url("//example.com/a.jpg") == "https://example.com/a.jpg"

    def test_non_http_scheme_is_rejected(self):
        assert normalize_url("javascript:alert(1)") is None
        assert normalize_url("data:image/png;base64,AAAA") is None

    def test_rtsp_is_preserved(self):
        assert guess_stream_format("rtsp://example.com/live") == "rtsp"

    def test_unknown_kind_falls_back_to_other(self):
        assert camera(kind="nonsense").kind == "other"

    def test_missing_name_falls_back_to_identifier(self):
        assert "42" in camera(name=None, native_id="42").name

    def test_whitespace_is_collapsed(self):
        assert camera(name="  I-5   at\n45th  ").name == "I-5 at 45th"


class TestDeduplication:
    def test_same_media_url_across_sources_is_merged(self):
        a = camera(source="alpha", native_id="1", image="https://cdn.example/x.jpg")
        b = camera(source="beta", native_id="9", image="https://cdn.example/x.jpg")
        merged, duplicates = deduplicate(
            [SourceResult("alpha", [a]), SourceResult("beta", [b])]
        )
        assert len(merged) == 1 and duplicates == 1

    def test_cache_busting_parameters_do_not_defeat_dedup(self):
        a = camera(source="alpha", native_id="1", image="https://cdn.example/x.jpg?t=111")
        b = camera(source="beta", native_id="9", image="https://cdn.example/x.jpg?t=222")
        merged, duplicates = deduplicate(
            [SourceResult("alpha", [a]), SourceResult("beta", [b])]
        )
        assert len(merged) == 1 and duplicates == 1

    def test_same_place_and_name_is_merged_across_hosts(self):
        a = camera(source="alpha", native_id="1", image="https://a.example/x.jpg")
        b = camera(source="beta", native_id="2", image="https://b.example/y.jpg")
        merged, _ = deduplicate([SourceResult("alpha", [a]), SourceResult("beta", [b])])
        assert len(merged) == 1

    def test_distinct_cameras_are_kept(self):
        a = camera(native_id="1", name="North view", image="https://a.example/1.jpg")
        b = camera(native_id="2", name="South view", image="https://a.example/2.jpg")
        merged, duplicates = deduplicate([SourceResult("test", [a, b])])
        assert len(merged) == 2 and duplicates == 0

    def test_result_order_does_not_change_the_winner(self):
        a = camera(source="alpha", native_id="1", image="https://cdn.example/x.jpg")
        b = camera(source="beta", native_id="9", image="https://cdn.example/x.jpg")
        forward, _ = deduplicate([SourceResult("alpha", [a]), SourceResult("beta", [b])])
        reverse, _ = deduplicate([SourceResult("beta", [b]), SourceResult("alpha", [a])])
        assert forward[0].id == reverse[0].id

    def test_output_is_sorted_deterministically(self):
        cams = [
            camera(native_id="1", country="US", name="a", image="https://e.example/1.jpg"),
            camera(native_id="2", country="AR", name="b", image="https://e.example/2.jpg"),
            camera(native_id="3", country="CA", name="c", image="https://e.example/3.jpg"),
        ]
        merged, _ = deduplicate([SourceResult("test", cams)])
        assert [c.country for c in merged] == ["AR", "CA", "US"]


class TestAdapters:
    """Adapters are checked against the response shapes documented in each module."""

    def test_castlerock_parses_multiple_views(self):
        source = Castle511(
            source_id="511x", name="Test 511", base_url="https://example.org",
            country="CA", region="ON",
        )
        cameras = source._parse(
            {
                "Id": 77,
                "RoadwayName": "Highway 401",
                "Location": "near Yonge St",
                "DirectionOfTravel": "East",
                "Latitude": "43.65",
                "Longitude": "-79.38",
                "Views": [
                    {"Id": 1, "Url": "https://example.org/a.jpg", "Status": "Enabled"},
                    {"Id": 2, "Url": "https://example.org/b.jpg", "Status": "Enabled"},
                ],
            }
        )
        assert len(cameras) == 2
        assert {c.native_id for c in cameras} == {"77:1", "77:2"}
        assert cameras[0].name == "Highway 401 - near Yonge St (East)"
        assert cameras[0].lat == pytest.approx(43.65)

    def test_castlerock_skips_disabled_views(self):
        source = Castle511(
            source_id="511x", name="Test", base_url="https://example.org", country="CA"
        )
        cameras = source._parse(
            {
                "Id": 1,
                "Location": "Somewhere",
                "Views": [{"Id": 1, "Url": "https://e.org/a.jpg", "Status": "Disabled"}],
            }
        )
        assert cameras == []

    def test_castlerock_survives_unexpected_shapes(self):
        source = Castle511(
            source_id="511x", name="Test", base_url="https://example.org", country="CA"
        )
        assert source._parse({"Id": 1, "Views": "not-a-list"}) == []
        assert source._parse("garbage") == []
        assert source._parse({}) == []

    def test_caltrans_parses_nested_record(self):
        result = Caltrans()._parse(
            {
                "cctv": {
                    "index": "12",
                    "inService": "true",
                    "location": {
                        "locationName": "SR-99 at Elkhorn",
                        "nearbyPlace": "Sacramento",
                        "route": "99",
                        "latitude": "38.63",
                        "longitude": "-121.51",
                    },
                    "imageData": {
                        "streamingVideoURL": "https://ca.example/live.m3u8",
                        "static": {"currentImageURL": "https://ca.example/a.jpg"},
                    },
                }
            },
            3,
        )
        assert result.native_id == "d3-12"
        assert result.stream_format == "hls"
        assert result.region == "CA"

    def test_caltrans_skips_out_of_service(self):
        assert Caltrans()._parse({"cctv": {"index": "1", "inService": "false"}}, 3) is None

    def test_nyc_parses_flat_record(self):
        result = NycTmc()._parse(
            {
                "id": "abc",
                "name": "1 Ave @ 42 St",
                "latitude": 40.75,
                "longitude": -73.97,
                "imageUrl": "https://nyc.example/a.jpg",
                "isOnline": "true",
            }
        )
        assert result.city == "New York"
        assert result.lat == pytest.approx(40.75)

    def test_nyc_parses_geojson_feature(self):
        result = NycTmc()._parse(
            {
                "geometry": {"type": "Point", "coordinates": [-73.97, 40.75]},
                "properties": {"id": "abc", "name": "X", "imageUrl": "https://n.example/a.jpg"},
            }
        )
        assert result.lat == pytest.approx(40.75)
        assert result.lon == pytest.approx(-73.97)

    def test_nyc_skips_offline(self):
        assert (
            NycTmc()._parse(
                {"id": "a", "name": "X", "imageUrl": "https://n.example/a.jpg", "isOnline": "false"}
            )
            is None
        )

    def test_tfl_flattens_additional_properties(self):
        result = TflJamCams()._parse(
            {
                "id": "JamCams_00001",
                "commonName": "A2 Sun in the Sands",
                "lat": 51.47,
                "lon": 0.02,
                "additionalProperties": [
                    {"key": "imageUrl", "value": "https://tfl.example/a.jpg"},
                    {"key": "videoUrl", "value": "https://tfl.example/a.mp4"},
                ],
            }
        )
        assert result.image.endswith(".jpg")
        assert result.stream.endswith(".mp4")
        assert result.country == "GB"


class TestRegistry:
    def test_source_ids_are_unique(self):
        ids = [source.id for source in all_sources()]
        assert len(ids) == len(set(ids))

    def test_every_source_is_fully_described(self):
        for source in all_sources():
            assert source.id and source.name, f"{source!r} is missing identity"
            assert source.homepage, f"{source.id} is missing a homepage"


class TestOutputs:
    def test_write_dataset_produces_expected_files(self, tmp_path: Path):
        cams = [
            camera(native_id="1", country="US", image="https://e.example/1.jpg"),
            camera(native_id="2", country="AR", name="Obelisco", lat=-34.6, lon=-58.4,
                   image="https://e.example/2.jpg"),
        ]
        merged, _ = deduplicate([SourceResult("test", cams)])
        write_dataset(
            merged, [SourceResult("test", cams)], all_sources(),
            out_dir=tmp_path, generated_at="2026-07-31T11:00:00Z",
        )

        index = json.loads((tmp_path / "v1" / "index.json").read_text())
        assert index["count"] == 2
        assert index["countries"] == {"AR": 1, "US": 1}

        everything = json.loads((tmp_path / "v1" / "cameras.json").read_text())
        assert len(everything["cameras"]) == 2

        shard = json.loads((tmp_path / "v1" / "countries" / "AR.json").read_text())
        assert shard["cameras"][0]["name"] == "Obelisco"

    def test_writing_twice_is_byte_identical(self, tmp_path: Path):
        cams = [camera(native_id=str(i), name=f"Cam {i}",
                       image=f"https://e.example/{i}.jpg") for i in range(20)]
        merged, _ = deduplicate([SourceResult("test", cams)])

        args = {"out_dir": tmp_path, "generated_at": "2026-07-31T11:00:00Z"}
        write_dataset(merged, [SourceResult("test", cams)], all_sources(), **args)
        first = (tmp_path / "v1" / "cameras.json").read_bytes()
        write_dataset(merged, [SourceResult("test", cams)], all_sources(), **args)
        assert (tmp_path / "v1" / "cameras.json").read_bytes() == first

    def test_stale_country_shards_are_removed(self, tmp_path: Path):
        stale = tmp_path / "v1" / "countries" / "XX.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}")

        cams = [camera(native_id="1", country="US", image="https://e.example/1.jpg")]
        write_dataset(cams, [SourceResult("test", cams)], all_sources(),
                      out_dir=tmp_path, generated_at="2026-07-31T11:00:00Z")
        assert not stale.exists()

    def test_cameras_without_country_land_in_the_unknown_shard(self, tmp_path: Path):
        cams = [camera(native_id="1", country=None, image="https://e.example/1.jpg")]
        write_dataset(cams, [SourceResult("test", cams)], all_sources(),
                      out_dir=tmp_path, generated_at="2026-07-31T11:00:00Z")
        assert (tmp_path / "v1" / "countries" / "ZZ.json").exists()


class TestUrlKeys:
    def test_cache_busters_are_stripped(self):
        assert stable_url_key("https://e.com/a.jpg?t=1") == stable_url_key("https://e.com/a.jpg?t=2")

    def test_meaningful_parameters_are_kept(self):
        assert stable_url_key("https://e.com/a?cam=1") != stable_url_key("https://e.com/a?cam=2")

    def test_host_case_and_trailing_slash_are_normalized(self):
        assert stable_url_key("https://E.com/a/") == stable_url_key("https://e.com/a")


class TestEndToEnd:
    """Exercises build() itself, with a stub source instead of the network."""

    @staticmethod
    def _stub_source(cameras, source_id="stub"):
        from cameras.sources.base import Source

        class Stub(Source):
            id = source_id
            name = "Stub"
            homepage = "https://example.org"

            async def fetch(self, http):
                return cameras

        return Stub()

    def _run(self, monkeypatch, cameras, tmp_path, **kwargs):
        import asyncio

        from cameras import build as build_module

        source = self._stub_source(cameras)
        monkeypatch.setattr(build_module, "select_sources", lambda *a, **k: [source])
        return asyncio.run(build_module.build(out_dir=tmp_path, **kwargs))

    def test_full_run_writes_a_usable_dataset(self, monkeypatch, tmp_path: Path):
        cams = [
            camera(native_id=str(i), name=f"Cam {i}", country="AR",
                   lat=-34.6 + i / 100, lon=-58.4,
                   image=f"https://e.example/{i}.jpg")
            for i in range(5)
        ]
        report = self._run(monkeypatch, cams, tmp_path)

        assert report.total == 5
        assert report.by_country == {"AR": 5}
        assert len(report.ok_sources) == 1

        index = json.loads((tmp_path / "v1" / "index.json").read_text())
        assert index["count"] == 5
        assert index["sources"][0]["ok"] is True
        # generated_at must be a parseable UTC instant for clients.
        assert index["generated_at"].endswith("Z")

    def test_min_cameras_aborts_before_writing_anything(self, monkeypatch, tmp_path: Path):
        cams = [camera(native_id="1", image="https://e.example/1.jpg")]
        with pytest.raises(RuntimeError, match="below the --min-cameras"):
            self._run(monkeypatch, cams, tmp_path, min_cameras=100)
        # The critical guarantee: a failed run leaves the previous data intact.
        assert not (tmp_path / "v1").exists()

    def test_a_failing_source_does_not_abort_the_run(self, monkeypatch, tmp_path: Path):
        import asyncio

        from cameras import build as build_module
        from cameras.sources.base import Source

        class Broken(Source):
            id = "broken"
            name = "Broken"
            homepage = "https://example.org"

            async def fetch(self, http):
                raise RuntimeError("upstream changed its schema")

        good = self._stub_source([camera(native_id="1", image="https://e.example/1.jpg")])
        monkeypatch.setattr(build_module, "select_sources", lambda *a, **k: [good, Broken()])
        report = asyncio.run(build_module.build(out_dir=tmp_path))

        assert report.total == 1
        assert len(report.failed_sources) == 1
        assert "upstream changed its schema" in report.failed_sources[0].error

        index = json.loads((tmp_path / "v1" / "index.json").read_text())
        broken = next(s for s in index["sources"] if s["id"] == "broken")
        assert broken["ok"] is False and "error" in broken
