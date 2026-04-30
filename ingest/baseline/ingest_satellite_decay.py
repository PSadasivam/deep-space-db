"""
Ingest Space-Track decay (re-entry) records into ``baseline_satellite_decay``.

Modes
-----
default (delta):
    Queries records with ``decay_date>now-7``. Cron daily.
        python -m deep_space_db.ingest.baseline.ingest_satellite_decay

--backfill --years N:
    Pulls all decay records for the last N years (default 5),
    paginated by year to stay under Space-Track rate limits.
        python -m deep_space_db.ingest.baseline.ingest_satellite_decay --backfill --years 5

Auth: reuses ``SPACETRACK_USER`` / ``SPACETRACK_PASS`` env vars
(already configured on EC2 via systemd drop-in).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from deep_space_db.ingest.baseline._common import (
    derive_orbit_regime,
    iso,
    update_state,
    upsert,
    with_conn,
)

SOURCE_KEY = "satellite_decay"
ST_BASE = "https://www.space-track.org"
ST_LOGIN = f"{ST_BASE}/ajaxauth/login"
HTTP_TIMEOUT = 60
SLEEP_BETWEEN = 12.0  # seconds; conservative re: 300/hr rate limit


# ── Auth ─────────────────────────────────────────────────────────────────
def login() -> "requests.Session":
    if requests is None:
        raise RuntimeError("'requests' is not installed")
    user = os.environ.get("SPACETRACK_USER")
    pwd = os.environ.get("SPACETRACK_PASS")
    if not user or not pwd:
        raise RuntimeError(
            "SPACETRACK_USER / SPACETRACK_PASS not set; configure systemd drop-in"
        )
    sess = requests.Session()
    sess.headers.update({"User-Agent": "deep-space-db/1.0"})
    resp = sess.post(
        ST_LOGIN, data={"identity": user, "password": pwd}, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()
    if "login" in (resp.text or "").lower() and resp.status_code != 200:
        raise RuntimeError("Space-Track login rejected")
    return sess


# ── Parsing ──────────────────────────────────────────────────────────────
def parse_decay_records(records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for rec in records:
        try:
            norad = int(rec.get("NORAD_CAT_ID") or rec.get("norad_cat_id") or 0)
        except (TypeError, ValueError):
            continue
        if norad <= 0:
            continue
        decay_raw = rec.get("DECAY") or rec.get("DECAY_DATE") or rec.get("decay")
        if not decay_raw:
            continue
        try:
            decay_iso = iso(decay_raw)
        except ValueError:
            continue
        launch_raw = rec.get("LAUNCH") or rec.get("LAUNCH_DATE")
        try:
            launch_iso = iso(launch_raw) if launch_raw else None
        except ValueError:
            launch_iso = None
        # Orbit regime: derive only if perigee/apogee provided
        try:
            perigee = float(rec.get("PERIGEE")) if rec.get("PERIGEE") else None
        except (TypeError, ValueError):
            perigee = None
        try:
            apogee = float(rec.get("APOGEE")) if rec.get("APOGEE") else None
        except (TypeError, ValueError):
            apogee = None
        rows.append(
            {
                "norad_cat_id": norad,
                "object_name": (rec.get("OBJECT_NAME") or "").strip() or None,
                "object_type": (rec.get("OBJECT_TYPE") or "").strip() or None,
                "country": (rec.get("COUNTRY") or rec.get("COUNTRY_CODE") or "").strip() or None,
                "launch_date": launch_iso,
                "decay_date": decay_iso,
                "rcs_size": (rec.get("RCS_SIZE") or "").strip() or None,
                "orbit_regime": derive_orbit_regime(perigee, apogee),
                "source": "spacetrack",
            }
        )
    return rows


# ── Query helpers ────────────────────────────────────────────────────────
def query_decay(sess, predicate: str) -> list[dict]:
    """Run a Space-Track query of the form
    /basicspacedata/query/class/decay/<predicate>/format/json
    """
    url = f"{ST_BASE}/basicspacedata/query/class/decay/{predicate}/format/json"
    resp = sess.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return []


def run_delta() -> tuple[int, str]:
    sess = login()
    records = query_decay(sess, "decay_date/>now-7/orderby/decay_date desc")
    rows = parse_decay_records(records)
    if not rows:
        with with_conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM baseline_satellite_decay")
            total = cur.fetchone()[0]
            update_state(conn, SOURCE_KEY, None, "ok", "delta: 0 rows", total)
        return (0, "")
    last_ts = max(r["decay_date"] for r in rows)
    with with_conn() as conn:
        n = upsert(conn, "baseline_satellite_decay", rows,
                   conflict_cols=["norad_cat_id"])
        cur = conn.execute("SELECT COUNT(*) FROM baseline_satellite_decay")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts, "ok",
                     f"delta upsert: {n} rows", total)
    return (n, last_ts)


def run_backfill(years: int) -> tuple[int, str]:
    if years <= 0:
        raise ValueError("years must be > 0")
    sess = login()
    today = datetime.now(timezone.utc).date()
    total_rows = 0
    last_ts = ""
    with with_conn() as conn:
        for offset in range(years):
            year = today.year - offset
            predicate = (
                f"decay_date/{year}-01-01--{year}-12-31/"
                f"orderby/decay_date desc"
            )
            try:
                records = query_decay(sess, predicate)
            except Exception as exc:
                print(f"[WARN] {year}: {exc}", file=sys.stderr)
                time.sleep(SLEEP_BETWEEN)
                continue
            rows = parse_decay_records(records)
            if not rows:
                print(f"[OK] {year}: 0 rows")
                time.sleep(SLEEP_BETWEEN)
                continue
            n = upsert(conn, "baseline_satellite_decay", rows,
                       conflict_cols=["norad_cat_id"])
            total_rows += n
            year_last = max(r["decay_date"] for r in rows)
            if year_last > last_ts:
                last_ts = year_last
            print(f"[OK] {year}: {n} rows")
            time.sleep(SLEEP_BETWEEN)
        cur = conn.execute("SELECT COUNT(*) FROM baseline_satellite_decay")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts or None, "ok",
                     f"backfill {years}y: {total_rows} rows", total)
    return (total_rows, last_ts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest Space-Track decay records into baseline_satellite_decay"
    )
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--years", type=int, default=5)
    args = p.parse_args(argv)
    try:
        if args.backfill:
            n, last = run_backfill(args.years)
            mode = "backfill"
        else:
            n, last = run_delta()
            mode = "delta"
    except Exception as exc:  # pragma: no cover
        with with_conn() as conn:
            update_state(conn, SOURCE_KEY, None, "error", repr(exc))
        print(f"[ERROR] ingest_satellite_decay: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] ingest_satellite_decay {mode}: {n} rows, latest={last}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
