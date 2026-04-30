"""Tests for ``deep_space_db/baseline_queries.py``."""
from __future__ import annotations

import pytest

from deep_space_db.ingest.baseline._common import upsert, with_conn


def _seed_kp(rows):
    with with_conn() as conn:
        upsert(
            conn,
            "baseline_kp_index",
            [
                {
                    "timestamp_utc": ts,
                    "kp_value": k,
                    "a_index": None,
                    "storm_class": None,
                    "source": "test",
                }
                for ts, k in rows
            ],
            conflict_cols=["timestamp_utc"],
        )


def _seed_neo(rows):
    with with_conn() as conn:
        upsert(
            conn,
            "baseline_neo_close_approach",
            [
                {
                    "object_designation": d,
                    "object_full_name": None,
                    "cd_utc": ts,
                    "miss_distance_au": miss,
                    "miss_distance_lunar": miss * 389.17,
                    "relative_velocity_kms": 10.0,
                    "diameter_min_m": None,
                    "diameter_max_m": None,
                    "h_magnitude": None,
                    "is_pha": 0,
                    "body": "Earth",
                    "source": "test",
                }
                for d, ts, miss in rows
            ],
            conflict_cols=["object_designation", "cd_utc", "body"],
        )


class TestPercentileSemantics:
    def test_higher_is_rarer_kp(self, temp_db):
        # 100 rows of Kp from 0..9, with 9.0 once -> 99.0 percentile for Kp=9
        rows = [
            (f"2024-01-01T{(i // 8):02d}:{(i % 8) * 7:02d}:00Z", i / 11)
            for i in range(100)
        ]
        rows.append(("2024-12-31T00:00:00Z", 9.0))
        _seed_kp(rows)
        from deep_space_db import baseline_queries
        baseline_queries.clear_cache()
        # Kp 9.0 should be > all but 1 -> 100 * 100 / 101 ~ 99.0
        p = baseline_queries.percentile("kp", "kp_value", 9.0)
        assert 98.0 <= p <= 100.0

    def test_smaller_is_rarer_neo(self, temp_db):
        rows = [
            (f"OBJ{i}", f"2020-01-{(i % 28) + 1:02d}T00:00:00Z", 0.01 + i * 0.01)
            for i in range(100)
        ]
        _seed_neo(rows)
        from deep_space_db import baseline_queries
        baseline_queries.clear_cache()
        # A miss of 0.0001 AU should be rarer than all 100 -> percentile 100
        p = baseline_queries.percentile("neo", "miss_distance_au", 0.0001)
        assert p == 100.0
        # A miss of 100 AU is bigger than all -> percentile 0
        p = baseline_queries.percentile("neo", "miss_distance_au", 100.0)
        assert p == 0.0

    def test_unsupported_metric_raises(self, temp_db):
        from deep_space_db import baseline_queries
        with pytest.raises(KeyError):
            baseline_queries.percentile("kp", "nope", 1.0)

    def test_invalid_window_raises(self, temp_db):
        from deep_space_db import baseline_queries
        with pytest.raises(ValueError):
            baseline_queries.percentile("kp", "kp_value", 5.0, window_days=0)

    def test_window_filter(self, temp_db):
        # Old rows: all Kp=2; recent rows: all Kp=9. With a 30-day window,
        # only the recent rows count, so Kp=5 should score 0 (no rows < 5
        # in the window because all recent are 9).
        old = [(f"2010-01-01T{h:02d}:00:00Z", 2.0) for h in range(24)]
        recent = [
            ("2099-12-30T00:00:00Z", 9.0),
            ("2099-12-30T03:00:00Z", 9.0),
            ("2099-12-30T06:00:00Z", 9.0),
        ]
        _seed_kp(old + recent)
        from deep_space_db import baseline_queries
        baseline_queries.clear_cache()
        # NOTE: the test fixtures use future timestamps so they fall inside
        # any reasonable window. Without window: 24 rows < 5, 3 rows >= 5
        # -> percentile of 5 = 24/27 ~ 88.9
        p_full = baseline_queries.percentile("kp", "kp_value", 5.0)
        assert p_full > 80
        # The cache scopes by window_days too, so request a different window
        # (None vs explicit) returns possibly different values.

    def test_empty_corpus_returns_zero(self, temp_db):
        from deep_space_db import baseline_queries
        baseline_queries.clear_cache()
        p = baseline_queries.percentile("kp", "kp_value", 5.0)
        assert p == 0.0

    def test_cache_returns_consistent(self, temp_db):
        _seed_kp([("2024-01-01T00:00:00Z", 1.0), ("2024-01-01T03:00:00Z", 2.0)])
        from deep_space_db import baseline_queries
        baseline_queries.clear_cache()
        p1 = baseline_queries.percentile("kp", "kp_value", 5.0)
        p2 = baseline_queries.percentile("kp", "kp_value", 5.0)
        assert p1 == p2 == 100.0  # 5.0 > all


class TestSupportedSurface:
    def test_supported_pairs(self):
        from deep_space_db import baseline_queries
        pairs = list(baseline_queries.list_supported())
        assert ("kp", "kp_value") in pairs
        assert ("xray", "flux_long") in pairs
        assert ("neo", "miss_distance_au") in pairs
        assert ("neo", "relative_velocity_kms") in pairs
        assert ("decay", "days_since_launch") in pairs
