"""
Ingest GOES X-ray flux into ``baseline_goes_xray``.

Modes
-----
default (delta):
    Fetches the SWPC primary GOES last-day JSON and upserts. Cheap;
    cron every 30 min.
        python -m deep_space_db.ingest.baseline.ingest_goes_xray

--backfill --from-dir DIR:
    Walk a directory of pre-downloaded NCEI monthly CSV files (one
    file per month, e.g. ``g16_xrs_1m_20170901_20170930.csv``) and
    upsert. Resumable via ``baseline_ingest_state.last_ingested_utc``.
        python -m deep_space_db.ingest.baseline.ingest_goes_xray --backfill --from-dir ./goes_archive

The NCEI raw format is NetCDF, but for the purposes of this corpus
we accept any CSV file with at least these columns (case-insensitive):
    ``time_tag`` (ISO-8601 or epoch), ``flux`` or ``xrsb_flux``
    (long band, W/m^2), and optional ``xrsa_flux`` (short band).

This keeps the script dependency-light (no NetCDF reader required on
EC2) — pre-export the NCEI files to CSV once, then feed them in.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from deep_space_db.ingest.baseline._common import (
    derive_flare_class,
    iso,
    update_state,
    upsert,
    with_conn,
)

SOURCE_KEY = "goes_xray"
SWPC_URL = "https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json"
HTTP_TIMEOUT = 30
LONG_BAND = "0.1-0.8nm"
SHORT_BAND = "0.05-0.4nm"


# ── Delta from SWPC ──────────────────────────────────────────────────────
def parse_swpc_payload(payload: list[dict]) -> list[dict]:
    """Group SWPC entries by (time_tag, satellite) and merge bands.

    SWPC publishes one record per band (``energy`` field). We pivot
    so each output row has both ``flux_long`` and (optionally)
    ``flux_short``.
    """
    if not payload:
        return []
    grouped: dict[tuple[str, str], dict] = {}
    for entry in payload:
        try:
            ts = iso(entry["time_tag"])
        except (KeyError, ValueError, TypeError):
            continue
        sat = entry.get("satellite")
        sat_str = f"GOES-{sat}" if sat else None
        flux = entry.get("flux")
        if flux is None:
            continue
        try:
            flux = float(flux)
        except (ValueError, TypeError):
            continue
        if flux <= 0:
            continue
        band = (entry.get("energy") or "").lower()
        key = (ts, sat_str or "")
        rec = grouped.setdefault(
            key,
            {
                "timestamp_utc": ts,
                "satellite": sat_str,
                "flux_long": None,
                "flux_short": None,
            },
        )
        if band == LONG_BAND:
            rec["flux_long"] = flux
        elif band == SHORT_BAND:
            rec["flux_short"] = flux
        else:
            # If energy band not specified, treat as long band
            rec["flux_long"] = rec["flux_long"] or flux
    out: list[dict] = []
    for rec in grouped.values():
        if rec["flux_long"] is None:
            continue
        cls, mag = derive_flare_class(rec["flux_long"])
        rec["flare_class"] = cls
        rec["flare_magnitude"] = mag
        rec["source"] = "noaa_swpc"
        out.append(rec)
    return out


def fetch_swpc_recent() -> list[dict]:
    if requests is None:
        raise RuntimeError("'requests' is not installed")
    resp = requests.get(SWPC_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return parse_swpc_payload(resp.json())


# ── Backfill from NCEI CSV files ─────────────────────────────────────────
def parse_ncei_csv(path: Path) -> list[dict]:
    """Parse a CSV file with columns time_tag + flux (long band).

    Tolerates a wide variety of NCEI export schemas. We only require
    a timestamp column and a long-band flux column.
    """
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return rows
        lower_map = {fn.lower(): fn for fn in reader.fieldnames}
        ts_col = (
            lower_map.get("time_tag")
            or lower_map.get("time")
            or lower_map.get("timestamp")
            or lower_map.get("date")
        )
        long_col = (
            lower_map.get("xrsb_flux")
            or lower_map.get("flux_long")
            or lower_map.get("flux")
        )
        short_col = lower_map.get("xrsa_flux") or lower_map.get("flux_short")
        sat_col = lower_map.get("satellite") or lower_map.get("sat")
        if ts_col is None or long_col is None:
            return rows
        for raw in reader:
            try:
                ts = iso(raw[ts_col])
                flux_long = float(raw[long_col])
            except (KeyError, ValueError, TypeError):
                continue
            if flux_long <= 0:
                continue
            flux_short = None
            if short_col:
                try:
                    flux_short = float(raw[short_col])
                    if flux_short <= 0:
                        flux_short = None
                except (ValueError, TypeError):
                    flux_short = None
            sat = raw.get(sat_col) if sat_col else None
            cls, mag = derive_flare_class(flux_long)
            rows.append(
                {
                    "timestamp_utc": ts,
                    "flux_long": flux_long,
                    "flux_short": flux_short,
                    "satellite": sat or None,
                    "flare_class": cls,
                    "flare_magnitude": mag,
                    "source": "ncei_archive",
                }
            )
    return rows


# ── Mode runners ─────────────────────────────────────────────────────────
def run_delta() -> tuple[int, str]:
    rows = fetch_swpc_recent()
    if not rows:
        return (0, "")
    last_ts = max(r["timestamp_utc"] for r in rows)
    with with_conn() as conn:
        n = upsert(conn, "baseline_goes_xray", rows,
                   conflict_cols=["timestamp_utc", "satellite"])
        cur = conn.execute("SELECT COUNT(*) FROM baseline_goes_xray")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts, "ok",
                     f"delta upsert: {n} rows", total)
    return (n, last_ts)


def run_backfill_dir(from_dir: Path) -> tuple[int, str]:
    if not from_dir.is_dir():
        raise NotADirectoryError(from_dir)
    files = sorted(from_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV files in {from_dir}")
    total_rows = 0
    last_ts = ""
    with with_conn() as conn:
        for path in files:
            rows = parse_ncei_csv(path)
            if not rows:
                continue
            n = upsert(conn, "baseline_goes_xray", rows,
                       conflict_cols=["timestamp_utc", "satellite"])
            total_rows += n
            file_last = max(r["timestamp_utc"] for r in rows)
            if file_last > last_ts:
                last_ts = file_last
            print(f"[OK] {path.name}: {n} rows")
        cur = conn.execute("SELECT COUNT(*) FROM baseline_goes_xray")
        total = cur.fetchone()[0]
        update_state(conn, SOURCE_KEY, last_ts or None, "ok",
                     f"backfill: {total_rows} rows from {len(files)} files", total)
    return (total_rows, last_ts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest GOES X-ray into baseline_goes_xray")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--from-dir", type=Path, default=None,
                   help="Directory of NCEI CSV exports for --backfill")
    args = p.parse_args(argv)
    try:
        if args.backfill:
            if args.from_dir is None:
                print("[ERROR] --backfill requires --from-dir DIR", file=sys.stderr)
                return 2
            n, last = run_backfill_dir(args.from_dir)
            mode = "backfill"
        else:
            n, last = run_delta()
            mode = "delta"
    except Exception as exc:  # pragma: no cover
        with with_conn() as conn:
            update_state(conn, SOURCE_KEY, None, "error", repr(exc))
        print(f"[ERROR] ingest_goes_xray: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] ingest_goes_xray {mode}: {n} rows, latest={last}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
