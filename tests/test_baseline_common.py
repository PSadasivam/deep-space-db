"""Tests for ``deep_space_db/ingest/baseline/_common.py``."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deep_space_db.ingest.baseline._common import (
    derive_flare_class,
    derive_orbit_regime,
    get_state,
    iso,
    update_state,
    upsert,
    with_conn,
)


# ── iso() ────────────────────────────────────────────────────────────────
class TestIso:
    def test_datetime_utc(self):
        dt = datetime(2017, 9, 6, 11, 53, tzinfo=timezone.utc)
        assert iso(dt) == "2017-09-06T11:53:00Z"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime(2003, 10, 29, 6, 0)
        assert iso(dt) == "2003-10-29T06:00:00Z"

    def test_epoch_int(self):
        assert iso(0) == "1970-01-01T00:00:00Z"

    def test_iso_with_z(self):
        assert iso("2024-01-01T00:00:00Z") == "2024-01-01T00:00:00Z"

    def test_iso_with_space_separator(self):
        assert iso("2024-01-01 12:30:45") == "2024-01-01T12:30:45Z"

    def test_iso_with_offset(self):
        assert iso("2024-01-01T12:00:00+05:30") == "2024-01-01T06:30:00Z"

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            iso("not a date")

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError):
            iso([2024, 1, 1])  # type: ignore[arg-type]


# ── derive_flare_class ───────────────────────────────────────────────────
class TestFlareClass:
    @pytest.mark.parametrize(
        "flux,cls,mag",
        [
            (5e-8, "A", 5.0),
            (1e-7, "B", 1.0),
            (2.5e-6, "C", 2.5),
            (5.2e-5, "M", 5.2),
            (9.3e-4, "X", 9.3),  # X9.3 flare — Sep 6 2017
            (1e-4, "X", 1.0),    # boundary
        ],
    )
    def test_classification(self, flux, cls, mag):
        c, m = derive_flare_class(flux)
        assert c == cls
        assert m == pytest.approx(mag, rel=1e-3)

    def test_zero_or_negative(self):
        assert derive_flare_class(0) == (None, None)
        assert derive_flare_class(-1e-5) == (None, None)
        assert derive_flare_class(None) == (None, None)  # type: ignore[arg-type]


# ── derive_orbit_regime ──────────────────────────────────────────────────
class TestOrbitRegime:
    def test_leo(self):
        assert derive_orbit_regime(400, 410) == "LEO"

    def test_meo(self):
        assert derive_orbit_regime(20000, 20200) == "MEO"

    def test_geo(self):
        assert derive_orbit_regime(35780, 35790) == "GEO"

    def test_heo(self):
        assert derive_orbit_regime(500, 36000) == "HEO"

    def test_missing_values(self):
        assert derive_orbit_regime(None, 400) is None
        assert derive_orbit_regime(400, None) is None

    def test_invalid_values(self):
        assert derive_orbit_regime(500, 400) is None  # apogee < perigee


# ── upsert + state ───────────────────────────────────────────────────────
class TestUpsertAndState:
    def test_upsert_inserts_then_updates(self, temp_db):
        rows = [
            {"timestamp_utc": "2024-01-01T00:00:00Z", "kp_value": 3.0,
             "a_index": None, "storm_class": None, "source": "test"},
            {"timestamp_utc": "2024-01-01T03:00:00Z", "kp_value": 4.5,
             "a_index": None, "storm_class": None, "source": "test"},
        ]
        with with_conn() as conn:
            n = upsert(conn, "baseline_kp_index", rows,
                       conflict_cols=["timestamp_utc"])
            assert n == 2

        # Re-run with updated value -> idempotent count, but value updated
        rows[0] = {**rows[0], "kp_value": 3.7}
        with with_conn() as conn:
            n = upsert(conn, "baseline_kp_index", rows,
                       conflict_cols=["timestamp_utc"])
            assert n == 2  # attempted
            cur = conn.execute(
                "SELECT kp_value FROM baseline_kp_index "
                "WHERE timestamp_utc = '2024-01-01T00:00:00Z'"
            )
            assert cur.fetchone()[0] == 3.7
            cur = conn.execute("SELECT COUNT(*) FROM baseline_kp_index")
            assert cur.fetchone()[0] == 2  # no duplicates

    def test_update_state_inserts_and_updates(self, temp_db):
        with with_conn() as conn:
            update_state(conn, "kp", "2024-01-01T00:00:00Z", "ok",
                         "first run", 100)
            s = get_state(conn, "kp")
            assert s is not None
            assert s["last_status"] == "ok"
            assert s["rows_total"] == 100

            update_state(conn, "kp", "2024-02-01T00:00:00Z", "partial",
                         "retry", 150)
            s = get_state(conn, "kp")
            assert s["last_status"] == "partial"
            assert s["last_ingested_utc"] == "2024-02-01T00:00:00Z"
            assert s["rows_total"] == 150

    def test_update_state_rejects_unknown_source(self, temp_db):
        with with_conn() as conn:
            with pytest.raises(ValueError):
                update_state(conn, "garbage", None, "ok")

    def test_update_state_rejects_invalid_status(self, temp_db):
        with with_conn() as conn:
            with pytest.raises(ValueError):
                update_state(conn, "kp", None, "weird")

    def test_upsert_empty_returns_zero(self, temp_db):
        with with_conn() as conn:
            assert upsert(conn, "baseline_kp_index", [], ["timestamp_utc"]) == 0
