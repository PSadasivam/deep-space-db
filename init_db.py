"""
Deep Space Research — Unified Analytics Database
Initializes the SQLite database and ingests data from all three projects.

Usage:
    python init_db.py                  # Full init + ingest
    python init_db.py --schema-only    # Create tables only
    python init_db.py --ingest-only    # Ingest data (assumes tables exist)
"""

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "deep_space_research.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

VOYAGER_DIR = BASE_DIR.parent / "voyager1_project"
ATLAS_DIR = BASE_DIR.parent / "3I-Atlas-Research"
BLACKHOLE_DIR = BASE_DIR.parent / "universe-inside-blackhole"


def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn):
    """Create all tables from schema.sql."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    print(f"[OK] Schema initialized: {DB_PATH}")


def log_ingestion(conn, source_file, table_name, rows, status="success", error=None):
    conn.execute(
        "INSERT INTO ingestion_log (source_file, table_name, rows_ingested, status, error_message) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(source_file), table_name, rows, status, error),
    )


# ── Voyager 1 Ingestion ────────────────────────────────────────────────

def ingest_voyager1_magnetometer(conn):
    """Ingest magnetic field test data."""
    csv_path = VOYAGER_DIR / "tests" / "voyager1_magnetometer_unittest.csv"
    if not csv_path.exists():
        print(f"[SKIP] {csv_path} not found")
        return

    conn.execute("DELETE FROM voyager1_magnetic_field WHERE source = 'test_fixture'")

    count = 0
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            conn.execute(
                "INSERT INTO voyager1_magnetic_field (timestamp_utc, b_nT, source) VALUES (?, ?, ?)",
                (row["time"].strip(), float(row["B_nT"].strip()), "test_fixture"),
            )
            count += 1

    log_ingestion(conn, csv_path, "voyager1_magnetic_field", count)
    print(f"[OK] voyager1_magnetic_field: {count} rows from test fixture")


def ingest_voyager1_events(conn):
    """Ingest Voyager 1 mission milestones."""
    conn.execute("DELETE FROM voyager1_events WHERE 1=1")

    events = [
        ("1977-09-05", "Launch", "Voyager 1 launched from Cape Canaveral aboard Titan IIIE/Centaur", None),
        ("1979-03-05", "Jupiter Flyby", "Closest approach to Jupiter at 349,000 km", 5.2),
        ("1980-11-12", "Saturn Flyby", "Closest approach to Saturn at 124,000 km; Titan encounter", 9.5),
        ("1990-02-14", "Pale Blue Dot", "Famous image of Earth from 6 billion km", 40.5),
        ("1998-02-17", "Most Distant Object", "Surpassed Pioneer 10 as most distant human-made object", 69.4),
        ("2004-12-16", "Termination Shock", "Crossed the termination shock at ~94 AU", 94.0),
        ("2012-08-25", "Heliopause", "Entered interstellar space at ~121 AU", 121.7),
        ("2025-01-01", "Interstellar Cruise", "Continuing through interstellar medium at ~164 AU", 164.0),
    ]

    for date, name, desc, dist in events:
        conn.execute(
            "INSERT INTO voyager1_events (event_date, event_name, description, distance_au) "
            "VALUES (?, ?, ?, ?)",
            (date, name, desc, dist),
        )

    log_ingestion(conn, "builtin_events", "voyager1_events", len(events))
    print(f"[OK] voyager1_events: {len(events)} milestones")


# ── 3I/ATLAS Ingestion ─────────────────────────────────────────────────

def ingest_atlas_ephemerides(conn):
    """Ingest 3I/ATLAS ephemerides from CSV."""
    csv_path = ATLAS_DIR / "ephemerides.csv"
    if not csv_path.exists():
        print(f"[SKIP] {csv_path} not found")
        return

    conn.execute("DELETE FROM atlas_3i_ephemerides WHERE source = 'jpl_horizons'")

    count = 0
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            conn.execute(
                "INSERT INTO atlas_3i_ephemerides "
                "(timestamp_utc, ra_deg, dec_deg, r_au, delta_au, v_mag, object_name, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("datetime_str", "").strip(),
                    float(row.get("RA", 0)),
                    float(row.get("DEC", 0)),
                    float(row.get("r", 0)) if row.get("r") else None,
                    float(row.get("delta", 0)) if row.get("delta") else None,
                    float(row.get("V", 0)) if row.get("V") else None,
                    row.get("object_name", "3I/ATLAS").strip(),
                    "jpl_horizons",
                ),
            )
            count += 1

    log_ingestion(conn, csv_path, "atlas_3i_ephemerides", count)
    print(f"[OK] atlas_3i_ephemerides: {count} rows")


def ingest_atlas_mast(conn):
    """Ingest MAST archival observations."""
    csv_path = ATLAS_DIR / "mast_catalog.csv"
    if not csv_path.exists():
        print(f"[SKIP] {csv_path} not found")
        return

    conn.execute("DELETE FROM atlas_3i_mast_observations WHERE source = 'mast'")

    count = 0
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            conn.execute(
                "INSERT INTO atlas_3i_mast_observations "
                "(obsid, mission, instrument, target_name, obs_start_utc, obs_end_utc, "
                "filters, proposal_id, data_url, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("obsid", "").strip(),
                    row.get("mission", row.get("source", "")).strip(),
                    row.get("instrument_name", "").strip(),
                    row.get("target_name", "").strip(),
                    row.get("datetime_obs_start_utc", row.get("t_observe_start", "")).strip(),
                    row.get("datetime_obs_end_utc", row.get("t_observe_end", "")).strip(),
                    row.get("filters", "").strip(),
                    row.get("proposal_id", "").strip(),
                    row.get("dataURL", row.get("data_url", "")).strip(),
                    "mast",
                ),
            )
            count += 1

    log_ingestion(conn, csv_path, "atlas_3i_mast_observations", count)
    print(f"[OK] atlas_3i_mast_observations: {count} rows")


def ingest_atlas_datasets(conn):
    """Ingest curated dataset catalog with metadata."""
    csv_path = ATLAS_DIR / "3I_ATLAS_Public_Datasets_with_Metadata.csv"
    if not csv_path.exists():
        print(f"[SKIP] {csv_path} not found")
        return

    conn.execute("DELETE FROM atlas_3i_datasets")

    count = 0
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            conn.execute(
                "INSERT INTO atlas_3i_datasets "
                "(dataset_source, observation_date, data_type, download_link, notes, "
                "instrument, program_id, filters_bands, exposure_details, pixel_scale_fov, archive_hint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("Dataset Source", "").strip(),
                    row.get("Observation Date", "").strip(),
                    row.get("Data Type", "").strip(),
                    row.get("Download Link", "").strip(),
                    row.get("Notes", "").strip(),
                    row.get("Instrument", "").strip(),
                    row.get("Program ID", "").strip(),
                    row.get("Filters/Bands", "").strip(),
                    row.get("Exposure Details", "").strip(),
                    row.get("Pixel Scale / Field of View", "").strip(),
                    row.get("Archive Search Hint", "").strip(),
                ),
            )
            count += 1

    log_ingestion(conn, csv_path, "atlas_3i_datasets", count)
    print(f"[OK] atlas_3i_datasets: {count} rows")


def ingest_atlas_orbital_elements(conn):
    """Ingest MPC orbital elements from JSON."""
    json_path = ATLAS_DIR / "Mpc-metadata" / "3I_mpc_orb.json"
    if not json_path.exists():
        print(f"[SKIP] {json_path} not found")
        return

    conn.execute("DELETE FROM atlas_3i_orbital_elements WHERE source = 'mpc'")

    with open(json_path, "r") as f:
        data = json.load(f)

    count = 0
    for rep_key in ("CAR", "COM"):
        rep = data.get(rep_key, {})
        names = rep.get("coefficient_names", [])
        values = rep.get("coefficient_values", [])
        uncertainties = rep.get("coefficient_uncertainties", [])
        for i, name in enumerate(names):
            val = values[i] if i < len(values) else None
            unc = uncertainties[i] if i < len(uncertainties) else None
            if val is not None:
                conn.execute(
                    "INSERT INTO atlas_3i_orbital_elements "
                    "(representation, param_name, param_value, param_uncertainty, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rep_key, name, float(val), float(unc) if unc else None, "mpc"),
                )
                count += 1

    log_ingestion(conn, json_path, "atlas_3i_orbital_elements", count)
    print(f"[OK] atlas_3i_orbital_elements: {count} parameters")


# ── Black Hole Ingestion ────────────────────────────────────────────────

def ingest_blackhole_simulation(conn):
    """Ingest black hole simulation constants and derived values."""
    conn.execute("DELETE FROM blackhole_simulations WHERE run_label = 'baseline_v1'")

    G = 6.674e-11
    c = 299792458.0
    H0_si = 67.15e3 / 3.0857e22  # km/s/Mpc → s⁻¹
    R_obs = 46.6e9 * 9.461e15     # Gly → meters
    rho_crit = 3.0 * H0_si**2 / (8.0 * math.pi * G)
    volume = (4.0 / 3.0) * math.pi * R_obs**3
    mass_total = rho_crit * volume
    R_g = 2.0 * G * mass_total / (c**2)
    ratio = R_obs / R_g

    params = [
        ("G", G, "m³/(kg·s²)", "Gravitational constant"),
        ("c", c, "m/s", "Speed of light"),
        ("H0", 67.15, "km/s/Mpc", "Hubble constant (Planck 2018)"),
        ("rho_crit", rho_crit, "kg/m³", "Critical density of the universe"),
        ("R_obs", R_obs, "m", "Observable universe radius"),
        ("volume_universe", volume, "m³", "Volume of observable universe"),
        ("mass_total", mass_total, "kg", "Total mass from critical density × volume"),
        ("R_schwarzschild", R_g, "m", "Schwarzschild radius of total mass"),
        ("radius_ratio", ratio, "dimensionless", "Observable radius / Schwarzschild radius"),
        ("a_min", 1.0, "dimensionless", "Minimum bounce scale factor"),
        ("t0", 1e17, "s", "Bounce timescale parameter"),
    ]

    count = 0
    for name, value, unit, desc in params:
        conn.execute(
            "INSERT INTO blackhole_simulations "
            "(run_label, param_name, param_value, param_unit, description) "
            "VALUES (?, ?, ?, ?, ?)",
            ("baseline_v1", name, value, unit, desc),
        )
        count += 1

    log_ingestion(conn, "black_hole_universe_simulation.py", "blackhole_simulations", count)
    print(f"[OK] blackhole_simulations: {count} parameters")


# ── Research Insights (seed) ────────────────────────────────────────────

def ingest_seed_insights(conn):
    """Seed cross-project research insights."""
    existing = conn.execute("SELECT COUNT(*) FROM research_insights").fetchone()[0]
    if existing > 0:
        print(f"[SKIP] research_insights: {existing} rows already exist")
        return

    insights = [
        ("voyager1", "milestone", "Interstellar Medium Entry",
         "Voyager 1 crossed the heliopause in Aug 2012 at ~121.7 AU, confirmed by plasma density jump.",
         "voyager1_electron_density", "heliopause,interstellar,density"),
        ("voyager1", "observation", "Magnetic Field Persistence",
         "Magnetic field magnitude remains ~0.1 nT in interstellar space, suggesting draped interstellar field lines.",
         "voyager1_magnetic_field", "magnetic,interstellar,field"),
        ("atlas_3i", "milestone", "Third Interstellar Object Confirmed",
         "C/2025 N1 (ATLAS) confirmed as 3I — third known interstellar object after Oumuamua and Borisov.",
         "atlas_3i_ephemerides", "interstellar,comet,discovery"),
        ("atlas_3i", "observation", "Hyperbolic Orbit",
         "Eccentricity well above 1.0 confirms unbound hyperbolic trajectory — extrasolar origin.",
         "atlas_3i_orbital_elements", "orbit,hyperbolic,eccentricity"),
        ("blackhole", "hypothesis", "Universe Inside a Black Hole",
         "Observable universe radius is within an order of magnitude of the Schwarzschild radius of its total mass.",
         "blackhole_simulations", "schwarzschild,cosmology,hypothesis"),
        ("cross_project", "observation", "Interstellar Medium Connects All",
         "Voyager 1 measures the interstellar medium that 3I/ATLAS traveled through — same physical environment.",
         "voyager1_electron_density,atlas_3i_ephemerides", "interstellar,cross-project,medium"),
    ]

    count = 0
    for proj, cat, title, desc, ref, tags in insights:
        conn.execute(
            "INSERT INTO research_insights (project, category, title, description, data_ref, tags) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (proj, cat, title, desc, ref, tags),
        )
        count += 1

    log_ingestion(conn, "seed_insights", "research_insights", count)
    print(f"[OK] research_insights: {count} seed insights")


# ── Main ────────────────────────────────────────────────────────────────

def run_all_ingestions(conn):
    print("\n─── Voyager 1 ───")
    ingest_voyager1_magnetometer(conn)
    ingest_voyager1_events(conn)

    print("\n─── 3I/ATLAS ───")
    ingest_atlas_ephemerides(conn)
    ingest_atlas_mast(conn)
    ingest_atlas_datasets(conn)
    ingest_atlas_orbital_elements(conn)

    print("\n─── Black Hole ───")
    ingest_blackhole_simulation(conn)

    print("\n─── Research Insights ───")
    ingest_seed_insights(conn)


def print_summary(conn):
    print("\n═══ Database Summary ═══")
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    total = 0
    for (table_name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()[0]
        total += count
        print(f"  {table_name:40s} {count:>6d} rows")
    print(f"  {'TOTAL':40s} {total:>6d} rows")
    size_kb = os.path.getsize(DB_PATH) / 1024
    print(f"\n  Database size: {size_kb:.1f} KB")
    print(f"  Location: {DB_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Deep Space Research — DB Init & Ingest")
    parser.add_argument("--schema-only", action="store_true", help="Create tables only")
    parser.add_argument("--ingest-only", action="store_true", help="Ingest data only (tables must exist)")
    args = parser.parse_args()

    conn = get_connection()

    try:
        if not args.ingest_only:
            init_schema(conn)

        if not args.schema_only:
            run_all_ingestions(conn)

        conn.commit()
        print_summary(conn)

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
