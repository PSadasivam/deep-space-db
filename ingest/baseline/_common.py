"""
Common helpers for baseline-corpus ingest jobs.

All ingest scripts under ``deep_space_db/ingest/baseline/`` use these
utilities for DB access, idempotent upserts, timestamp normalization,
and ingest-state bookkeeping. The goal is that every job is small,
idempotent, and honest about its high-water mark.

Public surface:
    db_path()                        -> Path to the SQLite database
    with_conn()                      -> contextmanager yielding a WAL connection
    iso(ts)                          -> normalized ISO-8601 UTC string
    upsert(conn, table, rows, conflict_cols)
    update_state(conn, source_key, last_ingested_utc, status, message, rows_total)
    get_state(conn, source_key)      -> dict | None
    log_run(source_key, started_at, ok, rows, message)  helper for CLI
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # deep_space_db/
DEFAULT_DB = BASE_DIR / "deep_space_research.db"


def db_path() -> Path:
    """Return the resolved path to the SQLite database.

    Honors ``DEEP_SPACE_DB_PATH`` env var so tests can point at a
    temp file without monkey-patching.
    """
    override = os.environ.get("DEEP_SPACE_DB_PATH")
    return Path(override) if override else DEFAULT_DB


@contextlib.contextmanager
def with_conn():
    """Yield a SQLite connection in WAL mode with foreign keys on."""
    conn = sqlite3.connect(str(db_path()))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Time normalization ───────────────────────────────────────────────────
def iso(ts) -> str:
    """Normalize any input timestamp to ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepts: ``datetime`` (naive treated as UTC), epoch float/int,
    or a string (ISO-8601 with optional ``Z`` or ``+00:00`` suffix,
    or ``YYYY-MM-DD HH:MM:SS``).
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if isinstance(ts, str):
        s = ts.strip()
        # common variants we accept
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # space separator -> T
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"Cannot parse timestamp: {ts!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise TypeError(f"Unsupported timestamp type: {type(ts).__name__}")


# ── Bulk upsert ──────────────────────────────────────────────────────────
def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, object]],
    conflict_cols: Sequence[str],
) -> int:
    """Bulk INSERT ... ON CONFLICT DO UPDATE for the given rows.

    ``rows`` is a sequence of dicts with identical keys. Returns the
    number of rows attempted. SQLite's ``INSERT OR IGNORE`` is not used
    because we want re-runs to refresh updated fields (e.g. a corrected
    Kp value from NOAA's preliminary -> final transition).
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    col_list = ", ".join(cols)
    update_clause = ", ".join(
        f"{c}=excluded.{c}" for c in cols if c not in conflict_cols
    )
    conflict_list = ", ".join(conflict_cols)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_list}) DO UPDATE SET {update_clause}"
    )
    payload = [tuple(r[c] for c in cols) for r in rows]
    # Chunk to keep transactions reasonable on large backfills
    chunk = 1000
    cur = conn.cursor()
    for i in range(0, len(payload), chunk):
        cur.executemany(sql, payload[i : i + chunk])
    return len(payload)


# ── Ingest-state bookkeeping ─────────────────────────────────────────────
VALID_SOURCE_KEYS = {"kp", "goes_xray", "neo_ca", "satellite_decay"}


def update_state(
    conn: sqlite3.Connection,
    source_key: str,
    last_ingested_utc: str | None,
    status: str,
    message: str = "",
    rows_total: int | None = None,
) -> None:
    """Upsert a row into ``baseline_ingest_state``."""
    if source_key not in VALID_SOURCE_KEYS:
        raise ValueError(f"Unknown source_key: {source_key}")
    if status not in {"ok", "partial", "error"}:
        raise ValueError(f"Invalid status: {status}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO baseline_ingest_state
            (source_key, last_ingested_utc, last_run_utc, last_status,
             last_message, rows_total, updated_at)
        VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), ?)
        ON CONFLICT(source_key) DO UPDATE SET
            last_ingested_utc=COALESCE(excluded.last_ingested_utc, baseline_ingest_state.last_ingested_utc),
            last_run_utc=excluded.last_run_utc,
            last_status=excluded.last_status,
            last_message=excluded.last_message,
            rows_total=COALESCE(excluded.rows_total, baseline_ingest_state.rows_total),
            updated_at=excluded.updated_at
        """,
        (
            source_key,
            last_ingested_utc,
            now,
            status,
            message[:1000],
            rows_total,
            now,
        ),
    )


def get_state(conn: sqlite3.Connection, source_key: str) -> dict | None:
    cur = conn.execute(
        "SELECT source_key, last_ingested_utc, last_run_utc, last_status, "
        "last_message, rows_total, updated_at "
        "FROM baseline_ingest_state WHERE source_key = ?",
        (source_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = [
        "source_key",
        "last_ingested_utc",
        "last_run_utc",
        "last_status",
        "last_message",
        "rows_total",
        "updated_at",
    ]
    return dict(zip(keys, row))


# ── Derivation helpers (pure, unit-tested) ───────────────────────────────
def derive_flare_class(flux_long: float) -> tuple[str | None, float | None]:
    """Return ``(class_letter, magnitude)`` for a GOES X-ray long-band flux.

    Bands per NOAA convention (W/m^2):
        A:  < 1e-7
        B:  1e-7 .. < 1e-6
        C:  1e-6 .. < 1e-5
        M:  1e-5 .. < 1e-4
        X:  >= 1e-4

    ``magnitude`` is the mantissa within the band, e.g. flux 5.2e-5 -> ('M', 5.2).
    Returns ``(None, None)`` for non-positive or invalid input.
    """
    if flux_long is None or flux_long <= 0:
        return (None, None)
    if flux_long < 1e-7:
        return ("A", flux_long / 1e-8)
    if flux_long < 1e-6:
        return ("B", flux_long / 1e-7)
    if flux_long < 1e-5:
        return ("C", flux_long / 1e-6)
    if flux_long < 1e-4:
        return ("M", flux_long / 1e-5)
    return ("X", flux_long / 1e-4)


def derive_orbit_regime(perigee_km: float | None, apogee_km: float | None) -> str | None:
    """Coarse orbit-regime classifier from perigee/apogee in km.

    LEO  : both < 2000 km
    MEO  : both 2000-35000 km
    GEO  : both ~35786 km +/- 1000 km
    HEO  : highly eccentric (apogee >> perigee, apogee > 35000)
    """
    if perigee_km is None or apogee_km is None:
        return None
    if perigee_km < 0 or apogee_km < perigee_km:
        return None
    geo_target = 35786
    if abs(perigee_km - geo_target) <= 1500 and abs(apogee_km - geo_target) <= 1500:
        return "GEO"
    if apogee_km > 35000 and (apogee_km - perigee_km) > 10000:
        return "HEO"
    if apogee_km < 2000:
        return "LEO"
    if 2000 <= perigee_km < 35000 and apogee_km < 35000:
        return "MEO"
    return None
