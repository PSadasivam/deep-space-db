-- Deep Space Research — Unified Analytics Database
-- Schema v1.0 — SQLite

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- VOYAGER 1: Magnetic Field Measurements
-- ============================================================
CREATE TABLE IF NOT EXISTS voyager1_magnetic_field (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,
    b_nT            REAL    NOT NULL,          -- magnetic field magnitude (nanoTesla)
    source          TEXT    DEFAULT 'nasa_spdf', -- nasa_spdf | synthetic
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v1_mag_ts ON voyager1_magnetic_field(timestamp_utc);

-- ============================================================
-- VOYAGER 1: Plasma Wave Spectrogram
-- ============================================================
CREATE TABLE IF NOT EXISTS voyager1_plasma_wave (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,
    frequency_hz    REAL    NOT NULL,          -- frequency channel (Hz)
    intensity       REAL    NOT NULL,          -- wave intensity (V²/m²/Hz)
    electric_field  REAL,                      -- mean electric field at this timestep
    source          TEXT    DEFAULT 'nasa_pds',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v1_pw_ts ON voyager1_plasma_wave(timestamp_utc);

-- ============================================================
-- VOYAGER 1: Electron Density (derived from plasma frequency)
-- ============================================================
CREATE TABLE IF NOT EXISTS voyager1_electron_density (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,
    plasma_freq_hz  REAL    NOT NULL,          -- plasma frequency (Hz)
    density_cm3     REAL    NOT NULL,          -- electron density (cm⁻³)
    source          TEXT    DEFAULT 'derived',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v1_den_ts ON voyager1_electron_density(timestamp_utc);

-- ============================================================
-- VOYAGER 1: Trajectory (position over time)
-- ============================================================
CREATE TABLE IF NOT EXISTS voyager1_trajectory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,
    x_au            REAL    NOT NULL,          -- heliocentric X (AU)
    y_au            REAL    NOT NULL,          -- heliocentric Y (AU)
    z_au            REAL    NOT NULL,          -- heliocentric Z (AU)
    distance_au     REAL,                      -- heliocentric distance (AU)
    gal_longitude   REAL,                      -- galactic longitude (degrees)
    gal_latitude    REAL,                      -- galactic latitude (degrees)
    source          TEXT    DEFAULT 'jpl_horizons',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v1_traj_ts ON voyager1_trajectory(timestamp_utc);

-- ============================================================
-- VOYAGER 1: Mission Events / Milestones
-- ============================================================
CREATE TABLE IF NOT EXISTS voyager1_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date      TEXT    NOT NULL,
    event_name      TEXT    NOT NULL,
    description     TEXT,
    distance_au     REAL,
    ingested_at     TEXT    DEFAULT (datetime('now'))
);

-- ============================================================
-- 3I/ATLAS: Ephemerides
-- ============================================================
CREATE TABLE IF NOT EXISTS atlas_3i_ephemerides (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,
    ra_deg          REAL    NOT NULL,          -- Right Ascension (degrees)
    dec_deg         REAL    NOT NULL,          -- Declination (degrees)
    r_au            REAL,                      -- heliocentric distance (AU)
    delta_au        REAL,                      -- geocentric distance (AU)
    v_mag           REAL,                      -- apparent visual magnitude
    object_name     TEXT    DEFAULT '3I/ATLAS',
    source          TEXT    DEFAULT 'jpl_horizons',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_3i_eph_ts ON atlas_3i_ephemerides(timestamp_utc);

-- ============================================================
-- 3I/ATLAS: MAST Archival Observations
-- ============================================================
CREATE TABLE IF NOT EXISTS atlas_3i_mast_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    obsid           TEXT    NOT NULL,
    mission         TEXT,                      -- HST | JWST
    instrument      TEXT,
    target_name     TEXT,
    obs_start_utc   TEXT,
    obs_end_utc     TEXT,
    filters         TEXT,
    proposal_id     TEXT,
    data_url        TEXT,
    source          TEXT    DEFAULT 'mast',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);

-- ============================================================
-- 3I/ATLAS: Orbital Elements
-- ============================================================
CREATE TABLE IF NOT EXISTS atlas_3i_orbital_elements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    representation  TEXT    NOT NULL,          -- CAR (Cartesian) | COM (Cometary)
    param_name      TEXT    NOT NULL,          -- e.g. x, y, z, vx, vy, vz or q, e, i, node, argperi
    param_value     REAL    NOT NULL,
    param_uncertainty REAL,
    epoch           TEXT,
    source          TEXT    DEFAULT 'mpc',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);

-- ============================================================
-- 3I/ATLAS: Dataset Catalog
-- ============================================================
CREATE TABLE IF NOT EXISTS atlas_3i_datasets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_source  TEXT    NOT NULL,          -- HST, JWST, NASA Horizons, etc.
    observation_date TEXT,
    data_type       TEXT,                      -- imaging, spectroscopy, ephemeris, etc.
    download_link   TEXT,
    notes           TEXT,
    instrument      TEXT,
    program_id      TEXT,
    filters_bands   TEXT,
    exposure_details TEXT,
    pixel_scale_fov TEXT,
    archive_hint    TEXT,
    ingested_at     TEXT    DEFAULT (datetime('now'))
);

-- ============================================================
-- BLACK HOLE: Simulation Parameters & Results
-- ============================================================
CREATE TABLE IF NOT EXISTS blackhole_simulations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_label       TEXT    NOT NULL,
    param_name      TEXT    NOT NULL,          -- G, c, H0, rho_crit, mass_total, R_g, etc.
    param_value     REAL    NOT NULL,
    param_unit      TEXT,
    description     TEXT,
    ingested_at     TEXT    DEFAULT (datetime('now'))
);

-- ============================================================
-- SPACE INTELLIGENCE: Near-Earth Objects
-- ============================================================
CREATE TABLE IF NOT EXISTS space_intel_neos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    neo_name        TEXT    NOT NULL,
    close_approach_date TEXT NOT NULL,
    is_hazardous    INTEGER DEFAULT 0,         -- 0/1
    diameter_min_m  REAL,
    diameter_max_m  REAL,
    velocity_kmh    REAL,
    miss_distance_km REAL,
    miss_distance_lunar REAL,
    source          TEXT    DEFAULT 'nasa_neows',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_neo_date ON space_intel_neos(close_approach_date);

-- ============================================================
-- SPACE INTELLIGENCE: Solar Activity (Flares, CMEs, Storms)
-- ============================================================
CREATE TABLE IF NOT EXISTS space_intel_solar (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT    NOT NULL,          -- flare | cme | storm
    event_time_utc  TEXT    NOT NULL,
    class_type      TEXT,                      -- e.g. X1.2, M5.0 for flares
    details         TEXT,                      -- JSON blob for extra fields
    source          TEXT    DEFAULT 'nasa_donki',
    ingested_at     TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_solar_ts ON space_intel_solar(event_time_utc);

-- ============================================================
-- CROSS-PROJECT: Research Insights & Notes
-- ============================================================
CREATE TABLE IF NOT EXISTS research_insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT    NOT NULL,          -- voyager1 | atlas_3i | blackhole | cross_project
    category        TEXT,                      -- observation | anomaly | milestone | hypothesis
    title           TEXT    NOT NULL,
    description     TEXT,
    data_ref        TEXT,                      -- reference to table/query that supports this insight
    tags            TEXT,                      -- comma-separated tags for search
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_insight_proj ON research_insights(project);
CREATE INDEX IF NOT EXISTS idx_insight_cat ON research_insights(category);

-- ============================================================
-- METADATA: Ingestion Log
-- ============================================================
CREATE TABLE IF NOT EXISTS ingestion_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file     TEXT    NOT NULL,
    table_name      TEXT    NOT NULL,
    rows_ingested   INTEGER NOT NULL,
    status          TEXT    DEFAULT 'success', -- success | error
    error_message   TEXT,
    ingested_at     TEXT    DEFAULT (datetime('now'))
);

-- ============================================================
-- METADATA: S3 Backup Log
-- ============================================================
CREATE TABLE IF NOT EXISTS s3_backup_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    s3_bucket       TEXT    NOT NULL,
    s3_key          TEXT    NOT NULL,
    file_size_bytes INTEGER,
    db_tables       INTEGER,                   -- count of tables at backup time
    db_total_rows   INTEGER,                   -- total rows across all data tables
    backup_at       TEXT    DEFAULT (datetime('now'))
);
