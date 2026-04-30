"""
Ingest the NOAA Planetary Kp index into ``baseline_kp_index``.

Modes
-----
default (delta):
    Fetches the NOAA SWPC last-30-days JSON and upserts. Cheap and
    safe to run hourly.
        python -m deep_space_db.ingest.baseline.ingest_kp

--backfill:
    Reads the GFZ Potsdam ``Kp_ap_since_1932.txt`` archive and inserts
    everything from 1932 to present. Writes a single transaction in
    1000-row chunks. Idempotent on re-run.
        python -m deep_space_db.ingest.baseline.ingest_kp --backfill

--from-file PATH:
    Use a pre-downloaded GFZ archive file (handy for tests, slow
    networks, or reproducible runs).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - exercised by environment without requests
    requests = None  # type: ignore

from deep_space_db.ingest.baseline._common import (
    iso,
    update_state,
    upsert,
    with_conn,
)

SOURCE_KEY = "kp"
SWPC_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
GFZ_URL = "https://kp.gfz-potsdam.de/app/files/Kp_ap_since_1932.txt"
HTTP_TIMEOUT = 30


# ── Storm class helper ───────────────────────────────────────────────────
def storm_class_from_kp(kp: float) -> str | None:
    """NOAA G-scale: G1 Kp=5, G2 Kp=6, G3 Kp=7, G4 Kp=8, G5 Kp=9."""
    if kp is None or kp < 5:
        return None
    if kp < 6:
        return "G1"
    if kp < 7:
        return "G2"
    if kp < 8:
        return "G3"
    if kp < 9:
        return "G4"
    return "G5"


# ── Delta mode (last 30 days from SWPC) ──────────────────────────────────
def parse_swpc_payload(payload: list) -> list[dict]:
    """SWPC has used two formats over time. We handle both:

    1. Header-then-rows array of arrays (legacy):
       [["time_tag", "Kp", "a_running", "station_count"], ["...", ...], ...]
    2. List of dicts (current as of 2026):
       [{"time_tag": "...", "Kp": 2.33, "a_running": 9, ...}, ...]
    """
    if not payload:
        return []

    # Detect format
    first = payload[0]
    if isinstance(first, dict):
        return _parse_swpc_dicts(payload)
    if isinstance(first, list):
        return _parse_swpc_arrays(payload)
    return []


def _parse_swpc_dicts(payload: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for entry in payload:
        try:
            ts = iso(entry.get("time_tag"))
            kp = float(entry.get("Kp"))
        except (ValueError, TypeError):
            continue
        a_val = entry.get("a_running")
        try:
            a_val = float(a_val) if a_val is not None else None
        except (ValueError, TypeError):
            a_val = None
        rows.append(
            {
                "timestamp_utc": ts,
                "kp_value": kp,
                "a_index": a_val,
                "storm_class": storm_class_from_kp(kp),
                "source": "noaa_swpc",
            }
        )
    return rows


def _parse_swpc_arrays(payload: list[list]) -> list[dict]:
    if len(payload) < 2:
        return []
    header = [c.lower() for c in payload[0]]
    try:
        ts_idx = header.index("time_tag")
        kp_idx = header.index("kp")
    except ValueError:
        return []
    a_idx = header.index("a_running") if "a_running" in header else None
    rows: list[dict] = []
    for raw in payload[1:]:
        try:
            ts = iso(raw[ts_idx])
            kp = float(raw[kp_idx])
        except (ValueError, TypeError, IndexError):
            continue
        a_val = None
        if a_idx is not None:
            try:
                a_val = float(raw[a_idx])
            except (ValueError, TypeError, IndexError):
                a_val = None
        rows.append(
            {
                "timestamp_utc": ts,
                "kp_value": kp,
                "a_index": a_val,
                "storm_class": storm_class_from_kp(kp),
                "source": "noaa_swpc",
            }
        )
    return rows


def fetch_swpc_recent() -> list[dict]:
    if requests is None:
        raise RuntimeError("'requests' is not installed")
    resp = requests.get(SWPC_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return parse_swpc_payload(resp.json())


# ── Backfill mode (GFZ archive) ──────────────────────────────────────────
def parse_gfz_archive(text: str) -> list[dict]:
    """Parse the GFZ ``Kp_ap_since_1932.txt`` file format.

    Modern GFZ format is whitespace-separated. The columns of interest
    after the comment header (``#`` lines) are:

        YYYY MM DD hh.h hh._m days days_m Kp ap D
    where Kp is at index 7 (0-based) in the data tokens. We extract
    the start-of-3hr-window timestamp and the Kp value.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 8:
            continue
        try:
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            hour_frac = float(parts[3])
            kp = float(parts[7])
        except ValueError:
            continue
        if kp < 0:  # missing data marker
            continue
        hour = int(hour_frac)
        try:
            dt = datetime(year, month, day, hour, tzinfo=timezone.utc)
        except ValueError:
            continue
        ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if ts in seen:
            continue
        seen.add(ts)
        rows.append(
            {
                "timestamp_utc": ts,
                "kp_value": kp,
                "a_index": None,
                "storm_class": storm_class_from_kp(kp),
                "source": "gfz_potsdam",
            }
        )
    return rows


def fetch_gfz_archive() -> str:
    if requests is None:
        raise RuntimeError("'requests' is not installed")
    resp = requests.get(GFZ_URL, timeout=HTTP_TIMEOUT * 4)
    resp.raise_for_status()
    return resp.text


# ── CLI ──────────────────────────────────────────────────────────────────
def run_delta() -> tuple[int, str]:
    rows = fetch_swpc_recent()
    if not rows:
        return (0, "")
    last_ts = max(r["timestamp_utc"] for r in rows)
    with with_conn() as conn:
        n = upsert(conn, "baseline_kp_index", rows,
                   conflict_cols=["timestamp_utc"])
        cur = conn.execute("SELECT COUNT(*) FROM baseline_kp_index")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts, "ok",
                     f"delta upsert: {n} rows", total)
    return (n, last_ts)


def run_backfill(from_file: Path | None) -> tuple[int, str]:
    if from_file is not None:
        text = from_file.read_text(encoding="utf-8")
    else:
        text = fetch_gfz_archive()
    rows = parse_gfz_archive(text)
    if not rows:
        return (0, "")
    last_ts = max(r["timestamp_utc"] for r in rows)
    with with_conn() as conn:
        n = upsert(conn, "baseline_kp_index", rows,
                   conflict_cols=["timestamp_utc"])
        cur = conn.execute("SELECT COUNT(*) FROM baseline_kp_index")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts, "ok",
                     f"backfill: {n} rows", total)
    return (n, last_ts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest NOAA Kp into baseline_kp_index")
    p.add_argument("--backfill", action="store_true",
                   help="Run GFZ Potsdam full-history backfill")
    p.add_argument("--from-file", type=Path, default=None,
                   help="Use a local copy of the GFZ archive file")
    args = p.parse_args(argv)
    try:
        if args.backfill or args.from_file:
            n, last = run_backfill(args.from_file)
            mode = "backfill"
        else:
            n, last = run_delta()
            mode = "delta"
    except Exception as exc:  # pragma: no cover - CLI safety net
        with with_conn() as conn:
            update_state(conn, SOURCE_KEY, None, "error", repr(exc))
        print(f"[ERROR] ingest_kp {mode if 'mode' in locals() else '?'}: {exc}",
              file=sys.stderr)
        return 1
    print(f"[OK] ingest_kp {mode}: {n} rows, latest={last}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
