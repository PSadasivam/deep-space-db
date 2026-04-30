"""
``baseline_queries.percentile`` — the single public API by which the
Signal vs Noise Engine consumes the historical baseline corpus.

No other module is permitted to query the ``baseline_*`` tables
directly. This is the abstraction boundary between the corpus and
the engine.

Example
-------
    >>> from deep_space_db.baseline_queries import percentile
    >>> percentile("kp", "kp_value", 9.0)
    99.987    # Halloween 2003 storm class
    >>> percentile("neo", "miss_distance_au", 0.0001, window_days=365)
    99.9      # extremely close encounter (smaller = rarer)
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

# ── DB path resolution (mirrors ingest/_common.py) ───────────────────────
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "deep_space_research.db"


def _db_path() -> Path:
    override = os.environ.get("DEEP_SPACE_DB_PATH")
    return Path(override) if override else DEFAULT_DB


# ── Spec table: which (event_type, metric) pairs are supported ──────────
# Each spec describes:
#   table       SQL table to query
#   ts_column   timestamp column for window filtering
#   metric_col  SQL expression evaluating to the metric
#   higher_is_rarer  True for kp/flux (big = rare),
#                    False for miss_distance (small = rare)
_SPECS: dict[tuple[str, str], dict] = {
    ("kp", "kp_value"): {
        "table": "baseline_kp_index",
        "ts_column": "timestamp_utc",
        "metric_col": "kp_value",
        "higher_is_rarer": True,
    },
    ("xray", "flux_long"): {
        "table": "baseline_goes_xray",
        "ts_column": "timestamp_utc",
        "metric_col": "flux_long",
        "higher_is_rarer": True,
    },
    ("neo", "miss_distance_au"): {
        "table": "baseline_neo_close_approach",
        "ts_column": "cd_utc",
        "metric_col": "miss_distance_au",
        "higher_is_rarer": False,
    },
    ("neo", "relative_velocity_kms"): {
        "table": "baseline_neo_close_approach",
        "ts_column": "cd_utc",
        "metric_col": "relative_velocity_kms",
        "higher_is_rarer": True,
    },
    ("decay", "days_since_launch"): {
        "table": "baseline_satellite_decay",
        "ts_column": "decay_date",
        # Computed metric: days between launch_date and decay_date
        "metric_col": (
            "(julianday(decay_date) - julianday(launch_date))"
        ),
        "higher_is_rarer": False,
    },
}

SUPPORTED = tuple(_SPECS.keys())


# ── Cache ────────────────────────────────────────────────────────────────
_CACHE_TTL = 3600  # 1 hour
_cache: dict[tuple, tuple[float, float]] = {}  # key -> (expires_at, percentile)
_cache_lock = threading.Lock()


def _cache_get(key: tuple) -> float | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if time.time() > expires:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key: tuple, value: float) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + _CACHE_TTL, value)


def clear_cache() -> None:
    """Drop all cached percentiles. Useful in tests after fresh writes."""
    with _cache_lock:
        _cache.clear()


# ── Public API ───────────────────────────────────────────────────────────
def percentile(
    event_type: str,
    metric: str,
    value: float,
    window_days: int | None = None,
) -> float:
    """Return the percentile rank (0..100) of ``value`` against the
    historical distribution of ``metric`` for ``event_type``.

    Conventions
    -----------
    For "higher is rarer" metrics (Kp, X-ray flux, relative velocity),
    the result is the fraction of historical observations strictly less
    than ``value``, scaled to 0..100. A value larger than every
    historical row scores 100.

    For "smaller is rarer" metrics (miss-distance), the result is the
    fraction of historical observations strictly greater than ``value``.
    A miss closer than every historical encounter scores 100.

    Parameters
    ----------
    event_type : str    one of {"kp", "xray", "neo", "decay"}
    metric     : str    metric column name (see SUPPORTED)
    value      : float  the observed value to score
    window_days: int | None
        If given, restrict the comparison corpus to the last
        ``window_days`` days. ``None`` = full history.

    Raises
    ------
    KeyError      unsupported (event_type, metric) pair
    ValueError    invalid window_days
    """
    key = (event_type, metric)
    spec = _SPECS.get(key)
    if spec is None:
        raise KeyError(
            f"Unsupported (event_type, metric)={key!r}. Supported: {SUPPORTED}"
        )
    if window_days is not None and window_days <= 0:
        raise ValueError("window_days must be positive")

    cache_key = (event_type, metric, float(value), window_days)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    table = spec["table"]
    ts_col = spec["ts_column"]
    metric_col = spec["metric_col"]
    higher_is_rarer = spec["higher_is_rarer"]
    op = "<" if higher_is_rarer else ">"

    where = [f"{metric_col} IS NOT NULL"]
    params: list = []
    if window_days is not None:
        where.append(f"{ts_col} >= datetime('now', ?)")
        params.append(f"-{int(window_days)} days")
    where_sql = " AND ".join(where)

    sql = (
        f"SELECT "
        f"  COUNT(*) AS total, "
        f"  SUM(CASE WHEN {metric_col} {op} ? THEN 1 ELSE 0 END) AS rarer "
        f"FROM {table} WHERE {where_sql}"
    )
    query_params = [float(value), *params]

    conn = sqlite3.connect(str(_db_path()))
    try:
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(sql, query_params)
        row = cur.fetchone()
    finally:
        conn.close()

    total = (row[0] or 0) if row else 0
    rarer = (row[1] or 0) if row else 0
    if total == 0:
        result = 0.0
    else:
        result = round(100.0 * rarer / total, 4)
    _cache_set(cache_key, result)
    return result


def list_supported() -> Iterable[tuple[str, str]]:
    """Return the (event_type, metric) pairs the engine can score."""
    return SUPPORTED
