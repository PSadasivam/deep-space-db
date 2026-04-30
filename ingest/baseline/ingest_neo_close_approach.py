"""
Ingest JPL SBDB close-approach archive into ``baseline_neo_close_approach``.

Modes
-----
default (delta):
    Queries last 30 days through next 30 days. Cron daily.
        python -m deep_space_db.ingest.baseline.ingest_neo_close_approach

--backfill --start YYYY --end YYYY:
    Walks year-by-year from --start to --end (inclusive). Sleeps
    between calls to be polite. Resumable: re-running with the same
    range is a no-op except for newly observed rows.
        python -m deep_space_db.ingest.baseline.ingest_neo_close_approach --backfill --start 1900 --end 2026

JPL CAD API: https://ssd-api.jpl.nasa.gov/cad.api
Public, no auth, JSON.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from deep_space_db.ingest.baseline._common import (
    iso,
    update_state,
    upsert,
    with_conn,
)

SOURCE_KEY = "neo_ca"
CAD_URL = "https://ssd-api.jpl.nasa.gov/cad.api"
HTTP_TIMEOUT = 60
SLEEP_BETWEEN = 1.0  # seconds, courtesy delay during backfill
AU_TO_LD = 389.17  # 1 AU in lunar distances


def parse_cad_payload(payload: dict) -> list[dict]:
    """Convert a single CAD response into row dicts.

    CAD response schema:
        { "fields": [...], "data": [[...], ...] }
    Field names of interest: des, orbit_id, jd, cd, dist, dist_min,
    dist_max, v_rel, v_inf, t_sigma_f, h, fullname.
    The CAD API returns ``cd`` as ``YYYY-MMM-DD HH:MM`` (note: month
    is alphabetic). We normalize to ISO.
    """
    fields = payload.get("fields") or []
    data = payload.get("data") or []
    if not fields or not data:
        return []
    fmap = {name: i for i, name in enumerate(fields)}

    def get(row, key, default=None):
        i = fmap.get(key)
        if i is None or i >= len(row):
            return default
        v = row[i]
        return v if v not in (None, "") else default

    rows: list[dict] = []
    for row in data:
        des = get(row, "des")
        cd_raw = get(row, "cd")
        dist = get(row, "dist")
        if not des or not cd_raw or dist is None:
            continue
        try:
            cd_dt = datetime.strptime(cd_raw, "%Y-%b-%d %H:%M")
            cd_dt = cd_dt.replace(tzinfo=timezone.utc)
            cd_iso = cd_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            miss_au = float(dist)
        except (ValueError, TypeError):
            continue
        if miss_au <= 0:
            continue
        v_rel = get(row, "v_rel")
        h = get(row, "h")
        fullname = get(row, "fullname")
        diameter_min = get(row, "diameter")  # CAD does not always have these
        diameter_max = get(row, "diameter_max")
        rows.append(
            {
                "object_designation": str(des).strip(),
                "object_full_name": str(fullname).strip() if fullname else None,
                "cd_utc": cd_iso,
                "miss_distance_au": miss_au,
                "miss_distance_lunar": miss_au * AU_TO_LD,
                "relative_velocity_kms": float(v_rel) if v_rel is not None else None,
                "diameter_min_m": float(diameter_min) * 1000 if diameter_min else None,
                "diameter_max_m": float(diameter_max) * 1000 if diameter_max else None,
                "h_magnitude": float(h) if h is not None else None,
                # CAD API does not return the official PHA flag. We use the
                # rough proxy H <= 22 (~140 m diameter) AND miss <= 0.05 AU.
                # Authoritative PHA flag should come from a future SBDB join.
                "is_pha": 1 if (h is not None and float(h) <= 22.0
                                and miss_au <= 0.05) else 0,
                "body": "Earth",
                "source": "jpl_sbdb",
            }
        )
    return rows


def fetch_window(date_min: str, date_max: str) -> list[dict]:
    if requests is None:
        raise RuntimeError("'requests' is not installed")
    params = {
        "date-min": date_min,
        "date-max": date_max,
        "body": "Earth",
        "fullname": "true",
        "sort": "date",
    }
    resp = requests.get(CAD_URL, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return parse_cad_payload(resp.json())


# ── Mode runners ─────────────────────────────────────────────────────────
def run_delta() -> tuple[int, str]:
    today = datetime.now(timezone.utc).date()
    date_min = (today - timedelta(days=30)).isoformat()
    date_max = (today + timedelta(days=30)).isoformat()
    rows = fetch_window(date_min, date_max)
    if not rows:
        return (0, "")
    last_ts = max(r["cd_utc"] for r in rows)
    with with_conn() as conn:
        n = upsert(conn, "baseline_neo_close_approach", rows,
                   conflict_cols=["object_designation", "cd_utc", "body"])
        cur = conn.execute("SELECT COUNT(*) FROM baseline_neo_close_approach")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts, "ok",
                     f"delta upsert: {n} rows ({date_min}..{date_max})", total)
    return (n, last_ts)


def run_backfill(start_year: int, end_year: int) -> tuple[int, str]:
    if start_year > end_year:
        raise ValueError("start_year must be <= end_year")
    total_rows = 0
    last_ts = ""
    with with_conn() as conn:
        for year in range(start_year, end_year + 1):
            date_min = f"{year}-01-01"
            date_max = f"{year}-12-31"
            try:
                rows = fetch_window(date_min, date_max)
            except Exception as exc:
                print(f"[WARN] {year}: {exc}", file=sys.stderr)
                continue
            if not rows:
                print(f"[OK] {year}: 0 rows")
                time.sleep(SLEEP_BETWEEN)
                continue
            n = upsert(conn, "baseline_neo_close_approach", rows,
                       conflict_cols=["object_designation", "cd_utc", "body"])
            total_rows += n
            year_last = max(r["cd_utc"] for r in rows)
            if year_last > last_ts:
                last_ts = year_last
            print(f"[OK] {year}: {n} rows")
            time.sleep(SLEEP_BETWEEN)
        cur = conn.execute("SELECT COUNT(*) FROM baseline_neo_close_approach")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts or None, "ok",
                     f"backfill {start_year}-{end_year}: {total_rows} rows", total)
    return (total_rows, last_ts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest JPL SBDB close-approach into baseline_neo_close_approach"
    )
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--start", type=int, default=1900,
                   help="Start year for --backfill (inclusive)")
    p.add_argument("--end", type=int, default=None,
                   help="End year for --backfill (default: current year)")
    args = p.parse_args(argv)
    try:
        if args.backfill:
            end = args.end or datetime.now(timezone.utc).year
            n, last = run_backfill(args.start, end)
            mode = "backfill"
        else:
            n, last = run_delta()
            mode = "delta"
    except Exception as exc:  # pragma: no cover
        with with_conn() as conn:
            update_state(conn, SOURCE_KEY, None, "error", repr(exc))
        print(f"[ERROR] ingest_neo_close_approach: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] ingest_neo_close_approach {mode}: {n} rows, latest={last}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
