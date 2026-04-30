"""Tests for ingest parsers (offline, no network).

We exercise the pure parse functions on synthetic payloads so the
ingest scripts can be validated without hitting NOAA / JPL / Space-Track.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from deep_space_db.ingest.baseline import (
    ingest_goes_xray,
    ingest_kp,
    ingest_neo_close_approach,
    ingest_satellite_decay,
)


# ── Kp parsers ───────────────────────────────────────────────────────────
class TestKpParsers:
    def test_storm_class(self):
        assert ingest_kp.storm_class_from_kp(3.0) is None
        assert ingest_kp.storm_class_from_kp(5.0) == "G1"
        assert ingest_kp.storm_class_from_kp(6.5) == "G2"
        assert ingest_kp.storm_class_from_kp(7.0) == "G3"
        assert ingest_kp.storm_class_from_kp(8.5) == "G4"
        assert ingest_kp.storm_class_from_kp(9.0) == "G5"

    def test_swpc_payload_parse(self):
        payload = [
            ["time_tag", "Kp", "a_running", "station_count"],
            ["2024-01-01 00:00:00.000", "2.33", "5", "8"],
            ["2024-01-01 03:00:00.000", "9.0", "400", "8"],
            ["bad", "row"],            # length mismatch -> dropped
            ["2024-01-01 06:00:00.000", "not-a-number", "0", "0"],  # dropped
        ]
        rows = ingest_kp.parse_swpc_payload(payload)
        assert len(rows) == 2
        assert rows[0]["kp_value"] == 2.33
        assert rows[1]["kp_value"] == 9.0
        assert rows[1]["storm_class"] == "G5"

    def test_swpc_empty(self):
        assert ingest_kp.parse_swpc_payload([]) == []
        assert ingest_kp.parse_swpc_payload([["only_header"]]) == []

    def test_swpc_dict_format(self):
        # NOAA SWPC switched to list-of-dicts format around 2026
        payload = [
            {"time_tag": "2026-04-23T00:00:00", "Kp": 2.33,
             "a_running": 9, "station_count": 8},
            {"time_tag": "2026-04-23T03:00:00", "Kp": 9.0,
             "a_running": 400, "station_count": 8},
        ]
        rows = ingest_kp.parse_swpc_payload(payload)
        assert len(rows) == 2
        assert rows[0]["kp_value"] == 2.33
        assert rows[1]["storm_class"] == "G5"

    def test_gfz_archive_parse(self):
        # Synthetic GFZ-style line: YYYY MM DD hh.h hh._m days days_m Kp ap D
        sample = (
            "# header line ignored\n"
            "1932 01 01 1.5 1.5  0.0625  0.0625  4.0  27 1\n"
            "1932 01 01 4.5 4.5  0.1875  0.1875  6.0  80 1\n"
            "2003 10 29 13.5 13.5 26966.5625 26966.5625  9.0 400 1\n"
            "2024 01 01 1.5 1.5  33603.5625 33603.5625 -1.0 -1 0\n"  # missing data
        )
        rows = ingest_kp.parse_gfz_archive(sample)
        assert len(rows) == 3
        assert rows[2]["kp_value"] == 9.0
        assert rows[2]["timestamp_utc"] == "2003-10-29T13:00:00Z"
        assert rows[2]["storm_class"] == "G5"


# ── GOES X-ray parsers ───────────────────────────────────────────────────
class TestGoesParsers:
    def test_swpc_groups_bands(self):
        payload = [
            {"time_tag": "2017-09-06T11:53:00Z", "satellite": 16,
             "flux": 9.3e-4, "energy": "0.1-0.8nm"},
            {"time_tag": "2017-09-06T11:53:00Z", "satellite": 16,
             "flux": 1.1e-4, "energy": "0.05-0.4nm"},
            {"time_tag": "2024-01-01T00:00:00Z", "satellite": 16,
             "flux": 0, "energy": "0.1-0.8nm"},  # zero -> dropped
            {"time_tag": "2024-01-01T00:01:00Z", "satellite": 16,
             "flux": 5.2e-5, "energy": "0.1-0.8nm"},
        ]
        rows = ingest_goes_xray.parse_swpc_payload(payload)
        assert len(rows) == 2
        x = next(r for r in rows if r["timestamp_utc"] == "2017-09-06T11:53:00Z")
        assert x["flux_long"] == 9.3e-4
        assert x["flux_short"] == 1.1e-4
        assert x["flare_class"] == "X"
        assert x["flare_magnitude"] == pytest.approx(9.3, rel=1e-3)
        m = next(r for r in rows if r["timestamp_utc"] == "2024-01-01T00:01:00Z")
        assert m["flare_class"] == "M"

    def test_ncei_csv_parse(self, tmp_path: Path):
        csv = tmp_path / "g16_xrs_1m_sample.csv"
        csv.write_text(
            "time_tag,xrsb_flux,xrsa_flux,satellite\n"
            "2017-09-06T11:53:00Z,9.3e-4,1.1e-4,GOES-16\n"
            "2017-09-06T11:54:00Z,8.5e-4,1.0e-4,GOES-16\n"
            "2017-09-06T11:55:00Z,-1,-1,GOES-16\n"   # negative -> dropped
            "bad,bad,bad,bad\n",                     # invalid -> dropped
            encoding="utf-8",
        )
        rows = ingest_goes_xray.parse_ncei_csv(csv)
        assert len(rows) == 2
        assert rows[0]["flux_long"] == 9.3e-4
        assert rows[0]["flare_class"] == "X"


# ── JPL CAD parser ───────────────────────────────────────────────────────
class TestNeoCadParser:
    def test_parse_close_approach(self):
        payload = {
            "fields": ["des", "fullname", "cd", "dist", "v_rel", "h"],
            "data": [
                ["2024 BX1", "(2024 BX1)", "2024-Jan-15 00:32",
                 "0.0000845", "15.2", "32.6"],
                ["2018 GE3", "(2018 GE3)", "2018-Apr-15 06:41",
                 "0.0013", "30.1", "23.8"],
                ["bad", "bad", "not a date", "0.001", "10", "20"],  # dropped
            ],
        }
        rows = ingest_neo_close_approach.parse_cad_payload(payload)
        assert len(rows) == 2
        bx = next(r for r in rows if r["object_designation"] == "2024 BX1")
        assert bx["cd_utc"] == "2024-01-15T00:32:00Z"
        assert bx["miss_distance_au"] == 0.0000845
        assert bx["miss_distance_lunar"] == pytest.approx(0.0000845 * 389.17,
                                                          rel=1e-4)
        # H=32.6 => not PHA
        assert bx["is_pha"] == 0

    def test_pha_proxy(self):
        # H=18 (~1km), miss=0.04 AU -> PHA proxy True
        payload = {
            "fields": ["des", "fullname", "cd", "dist", "v_rel", "h"],
            "data": [
                ["1999 AN10", "(1999 AN10)", "2027-Aug-07 09:14",
                 "0.0026", "9.4", "17.9"],
            ],
        }
        rows = ingest_neo_close_approach.parse_cad_payload(payload)
        assert rows[0]["is_pha"] == 1

    def test_empty_payload(self):
        assert ingest_neo_close_approach.parse_cad_payload({}) == []
        assert ingest_neo_close_approach.parse_cad_payload(
            {"fields": [], "data": []}
        ) == []


# ── Space-Track decay parser ─────────────────────────────────────────────
class TestDecayParser:
    def test_parse_decay_records(self):
        records = [
            {
                "NORAD_CAT_ID": "12345",
                "OBJECT_NAME": "TIANGONG-1",
                "OBJECT_TYPE": "PAYLOAD",
                "COUNTRY": "PRC",
                "LAUNCH": "2011-09-29",
                "DECAY": "2018-04-02 00:16:00",
                "RCS_SIZE": "LARGE",
                "PERIGEE": "200",
                "APOGEE": "210",
            },
            {
                "NORAD_CAT_ID": "0",            # invalid -> dropped
                "DECAY": "2024-01-01",
            },
            {
                "NORAD_CAT_ID": "67890",
                "DECAY": "not-a-date",          # invalid date -> dropped
            },
        ]
        rows = ingest_satellite_decay.parse_decay_records(records)
        assert len(rows) == 1
        assert rows[0]["norad_cat_id"] == 12345
        assert rows[0]["decay_date"] == "2018-04-02T00:16:00Z"
        assert rows[0]["orbit_regime"] == "LEO"
        assert rows[0]["object_type"] == "PAYLOAD"
