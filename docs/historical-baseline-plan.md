# Historical Baseline Service — Schema & Ingest Plan

**Status:** Draft · Apr 29, 2026
**Owner:** Phase 1.0 of the Space Intelligence Platform (see
[deep_space_portal/docs/space-intelligence-platform-p2.md](../../deep_space_portal/docs/space-intelligence-platform-p2.md))
**Prerequisite for:** Signal Engine scoring, Interstellar Watch,
Earth Impact Layer, What-Changed delta engine.

---

## 1. Why this exists

The Signal vs Noise Engine requires a `percentile(event_type, metric, value) -> float`
function so claims like *"largest flare in 47 days"* and *"99th-percentile
geomagnetic storm"* are auditable rather than vibes. That function needs a
multi-year corpus of historical observations, organized for fast distribution
queries. This document defines that corpus.

**Scope:** four data sources, four tables, four ingest jobs, one query helper.
No UI in this phase.

**Out of scope (deferred to Phase 1.1):** scoring engine, panel rendering,
real-time refresh, "My Perspective" content.

---

## 2. Data sources

| Source | Cadence (native) | Backfill window | Volume estimate | License / access |
|---|---|---|---|---|
| NOAA SWPC — Planetary Kp index | 3 hours | 1932-01-01 → present | ~270 k rows | Public domain, no auth |
| NOAA SWPC — GOES X-ray flux (1-min) | 1 minute | 2010-01-01 → present (GOES-15 onward, well-calibrated) | ~8 M rows | Public domain, no auth |
| JPL SBDB — close-approach archive | per-encounter (sparse) | All historical to ~+2200 (predicted) | ~50 k rows | Public domain, no auth |
| Space-Track — GP / decay history | continuous | last 5 years (sufficient for orbit-regime baselines) | ~500 k decay records | Free account, already configured |

> **Volume note.** GOES X-ray at 1-min cadence over 16 years is the largest
> dataset by ~10× — but at ~50 bytes/row in SQLite that's still well under
> 1 GB. No need for Parquet/DuckDB at this scale.

---

## 3. Schema additions to `deep_space_db/schema.sql`

All tables follow the existing conventions: `INTEGER PRIMARY KEY AUTOINCREMENT`,
`timestamp_utc TEXT NOT NULL`, `source TEXT`, `ingested_at TEXT DEFAULT (datetime('now'))`.

### 3.1 `baseline_kp_index`

Planetary Kp from NOAA SWPC. Three-hourly granularity.

```sql
CREATE TABLE IF NOT EXISTS baseline_kp_index (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,           -- ISO-8601, start of 3-hr window
    kp_value        REAL    NOT NULL,           -- 0.0 .. 9.0
    a_index         REAL,                       -- daily Ap if available
    storm_class     TEXT,                       -- G1..G5 if applicable, else NULL
    source          TEXT    DEFAULT 'noaa_swpc',
    ingested_at     TEXT    DEFAULT (datetime('now')),
    UNIQUE(timestamp_utc)
);
CREATE INDEX IF NOT EXISTS idx_baseline_kp_ts ON baseline_kp_index(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_baseline_kp_val ON baseline_kp_index(kp_value);
```

### 3.2 `baseline_goes_xray`

GOES X-ray flux (long band, 0.1–0.8 nm) at 1-min cadence. Used to detect
flares (C/M/X classes correspond to flux thresholds).

```sql
CREATE TABLE IF NOT EXISTS baseline_goes_xray (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc   TEXT    NOT NULL,           -- ISO-8601, 1-min cadence
    flux_long       REAL    NOT NULL,           -- W/m², 0.1-0.8 nm band
    flux_short      REAL,                       -- W/m², 0.05-0.4 nm band (optional)
    satellite       TEXT,                       -- e.g. GOES-16, GOES-18
    flare_class     TEXT,                       -- A/B/C/M/X derived; NULL during quiet sun
    flare_magnitude REAL,                       -- e.g. 5.2 for an M5.2 flare
    source          TEXT    DEFAULT 'noaa_swpc',
    ingested_at     TEXT    DEFAULT (datetime('now')),
    UNIQUE(timestamp_utc, satellite)
);
CREATE INDEX IF NOT EXISTS idx_baseline_xray_ts ON baseline_goes_xray(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_baseline_xray_flux ON baseline_goes_xray(flux_long);
CREATE INDEX IF NOT EXISTS idx_baseline_xray_class ON baseline_goes_xray(flare_class) WHERE flare_class IS NOT NULL;
```

### 3.3 `baseline_neo_close_approach`

JPL SBDB close-approach records. One row per encounter (object × date).

```sql
CREATE TABLE IF NOT EXISTS baseline_neo_close_approach (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    object_designation TEXT NOT NULL,           -- e.g. "(2024 BX1)"
    object_full_name TEXT,                      -- if available
    cd_utc          TEXT    NOT NULL,           -- close-approach date/time UTC
    miss_distance_au REAL   NOT NULL,
    miss_distance_lunar REAL,                   -- miss in lunar distances
    relative_velocity_kms REAL,                 -- km/s at close approach
    diameter_min_m  REAL,
    diameter_max_m  REAL,
    h_magnitude     REAL,                       -- absolute magnitude
    is_pha          INTEGER DEFAULT 0,          -- Potentially Hazardous Asteroid flag
    body            TEXT    DEFAULT 'Earth',    -- target body (almost always Earth)
    source          TEXT    DEFAULT 'jpl_sbdb',
    ingested_at     TEXT    DEFAULT (datetime('now')),
    UNIQUE(object_designation, cd_utc, body)
);
CREATE INDEX IF NOT EXISTS idx_baseline_neo_date ON baseline_neo_close_approach(cd_utc);
CREATE INDEX IF NOT EXISTS idx_baseline_neo_miss ON baseline_neo_close_approach(miss_distance_au);
CREATE INDEX IF NOT EXISTS idx_baseline_neo_pha ON baseline_neo_close_approach(is_pha) WHERE is_pha = 1;
```

### 3.4 `baseline_satellite_decay`

Re-entry events from Space-Track. One row per decayed object.

```sql
CREATE TABLE IF NOT EXISTS baseline_satellite_decay (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    norad_cat_id    INTEGER NOT NULL,
    object_name     TEXT,
    object_type     TEXT,                       -- PAYLOAD | ROCKET BODY | DEBRIS | UNKNOWN
    country         TEXT,
    launch_date     TEXT,
    decay_date      TEXT    NOT NULL,
    rcs_size        TEXT,                       -- SMALL | MEDIUM | LARGE
    orbit_regime    TEXT,                       -- LEO | MEO | GEO | HEO | derived
    source          TEXT    DEFAULT 'spacetrack',
    ingested_at     TEXT    DEFAULT (datetime('now')),
    UNIQUE(norad_cat_id)
);
CREATE INDEX IF NOT EXISTS idx_baseline_decay_date ON baseline_satellite_decay(decay_date);
CREATE INDEX IF NOT EXISTS idx_baseline_decay_orbit ON baseline_satellite_decay(orbit_regime);
```

### 3.5 `baseline_ingest_state`

Tracks the high-water mark for each source so nightly delta jobs are idempotent.

```sql
CREATE TABLE IF NOT EXISTS baseline_ingest_state (
    source_key      TEXT    PRIMARY KEY,        -- 'kp' | 'goes_xray' | 'neo_ca' | 'satellite_decay'
    last_ingested_utc TEXT,                     -- newest timestamp_utc successfully ingested
    last_run_utc    TEXT,
    last_status     TEXT,                       -- ok | partial | error
    last_message    TEXT,                       -- short note or error trace
    rows_total      INTEGER DEFAULT 0,
    updated_at      TEXT    DEFAULT (datetime('now'))
);
```

---

## 4. Ingest scripts

All scripts live under `deep_space_db/ingest/baseline/` (new folder).
Pattern follows the existing [s3_backup.py](../s3_backup.py) style:
straight Python, `requests` for HTTP, `sqlite3` for writes, idempotent
on re-run.

### 4.1 Common helper

`deep_space_db/ingest/baseline/_common.py`

- `db_path()` — resolves to `deep_space_db/deep_space_research.db`.
- `with_conn()` — context manager opening WAL-mode connection.
- `update_state(source_key, last_ts, status, msg, rows)` — writes
  `baseline_ingest_state`.
- `upsert(table, rows, conflict_cols)` — bulk INSERT with `ON CONFLICT(...)
  DO UPDATE`.
- `iso(ts)` — normalize any input timestamp to ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`).

### 4.2 `ingest_kp.py`

- **Source:** <https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json>
  (last 30 days, 3-hourly).
- **Backfill source:** GFZ Potsdam Kp archive
  <https://kp.gfz-potsdam.de/app/files/Kp_ap_since_1932.txt>
  (single text file, all-time).
- **Run modes:**
  - `--backfill` → parse the GFZ file once, populate ~270 k rows.
  - default → fetch SWPC last-30-days JSON, upsert by `timestamp_utc`.
- **Writes to:** `baseline_kp_index`.
- **Cadence (delta job):** every 6 hours (Kp is published every 3).

### 4.3 `ingest_goes_xray.py`

- **Source (recent):** <https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json>
  (last 24 h, 1-min cadence).
- **Backfill source:** NOAA NCEI archive
  <https://www.ncei.noaa.gov/data/goes-space-environment-monitor/access/avg/>
  (per-month files, NetCDF).
- **Run modes:**
  - `--backfill --start YYYY-MM` → walk monthly archive files, parse,
    insert. Resumable via `baseline_ingest_state.last_ingested_utc`.
  - default → fetch the 1-day JSON, upsert.
- **Derived fields:**
  - `flare_class` = `A` if flux < 1e-7, `B` < 1e-6, `C` < 1e-5, `M` < 1e-4, `X` ≥ 1e-4.
  - `flare_magnitude` = scale within band (e.g. flux 5.2e-5 → `M5.2`).
  - These derivations are pure functions, unit-tested.
- **Writes to:** `baseline_goes_xray`.
- **Cadence (delta job):** every 30 minutes.

### 4.4 `ingest_neo_close_approach.py`

- **Source:** JPL SBDB close-approach API
  `https://ssd-api.jpl.nasa.gov/cad.api?date-min=YYYY-MM-DD&date-max=YYYY-MM-DD&fullname=true&body=Earth`.
- **Backfill source:** same API, queried in 1-year chunks from
  1900-01-01 (records start ~1900s).
- **Run modes:**
  - `--backfill` → loop year-by-year, sleep 1 s between calls, write.
  - default → query last 30 days through next 30 days, upsert.
- **Writes to:** `baseline_neo_close_approach`.
- **Cadence (delta job):** daily.

### 4.5 `ingest_satellite_decay.py`

- **Source:** Space-Track REST query
  `https://www.space-track.org/basicspacedata/query/class/decay/orderby/decay_date desc/format/json`.
- **Auth:** reuses `SPACETRACK_USER`/`SPACETRACK_PASS` env vars already
  configured on EC2 (via systemd drop-in).
- **Run modes:**
  - `--backfill --years 5` → paginate by year.
  - default → query records with `decay_date>now-7`, upsert by `norad_cat_id`.
- **Derived fields:**
  - `orbit_regime` = LEO / MEO / GEO / HEO derived from final epoch
    semi-major axis where available; else NULL.
- **Writes to:** `baseline_satellite_decay`.
- **Cadence (delta job):** daily.

### 4.6 Scheduler

For local dev: a Make-style `python -m deep_space_db.ingest.baseline.run --all` driver.

For EC2: cron entries (or a systemd timer) — keep it simple, one timer per source,
staggered to avoid spikes:

```cron
# /etc/cron.d/baseline-ingest  (root)
17 */6 * * * ec2-user /home/ec2-user/deep-space-portal/venv/bin/python -m deep_space_db.ingest.baseline.ingest_kp
*/30 * * * * ec2-user /home/ec2-user/deep-space-portal/venv/bin/python -m deep_space_db.ingest.baseline.ingest_goes_xray
23 02 * * * ec2-user /home/ec2-user/deep-space-portal/venv/bin/python -m deep_space_db.ingest.baseline.ingest_neo_close_approach
41 02 * * * ec2-user /home/ec2-user/deep-space-portal/venv/bin/python -m deep_space_db.ingest.baseline.ingest_satellite_decay
```

---

## 5. Query helper

`deep_space_db/baseline_queries.py` — the single function the Signal
Engine consumes. **No I/O outside this module touches baseline tables.**

```python
def percentile(event_type: str, metric: str, value: float,
               window_days: int | None = None) -> float:
    """Return the percentile rank (0..100) of `value` against the
    historical distribution of `metric` for `event_type`.

    event_type / metric pairs supported in v1:
      ('kp',          'kp_value')
      ('xray',        'flux_long')
      ('neo',         'miss_distance_au')   # smaller = rarer
      ('neo',         'relative_velocity_kms')
      ('decay',       'days_since_launch')

    `window_days` optionally restricts the comparison corpus (e.g. last
    365 days for "rarest in the last year"). Default = full history.
    """
```

**Implementation notes:**
- Backed by a single SQL query per call: `SELECT COUNT(*) FILTER (WHERE m <= :v) * 100.0 / COUNT(*) FROM baseline_X WHERE ...`.
- Cached at module level for 1 hour per `(event_type, metric, window_days)` —
  the distribution is stable enough that recomputing every call is
  wasteful.
- For `miss_distance_au`, the helper inverts so lower = higher
  percentile (rarer events score higher).

---

## 6. Acceptance criteria for Phase 1.0

Phase 1.0 is **done** when *all* of these are true:

1. The four `baseline_*` tables exist in `deep_space_research.db` and
   contain a backfilled corpus:
   - `baseline_kp_index`: ≥ 200 k rows, oldest record < 1950
   - `baseline_goes_xray`: ≥ 5 M rows, oldest record < 2012
   - `baseline_neo_close_approach`: ≥ 30 k rows, oldest record < 1950
   - `baseline_satellite_decay`: ≥ 50 k rows
2. All four ingest scripts pass `--backfill` and `default` modes
   idempotently (re-running produces zero new rows when source unchanged).
3. `baseline_ingest_state` shows a `last_status='ok'` for every
   source after the cron jobs have completed at least one cycle.
4. `percentile()` returns sane values for known historical events:
   - Halloween 2003 storms → Kp percentile > 99.9
   - 2024 BX1 close approach → miss-distance percentile > 99
   - X9.3 flare of Sep 6 2017 → flux percentile > 99.9
5. Unit-tests: ≥ 12 tests covering the derivation functions
   (flare class, orbit regime), the query helper, and one
   integration test per ingest script using a tiny SQLite fixture.
6. Documentation: this file, plus a `deep_space_db/README.md`
   section pointing at `baseline_queries.percentile` as the public API.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| GOES NetCDF backfill is slow (16 years × 12 months × ~50 MB) | Run once on EC2 in a tmux session, not on the laptop. Resumable via `last_ingested_utc`. ~24 hr total, single run. |
| Space-Track rate limit (300/hr) during decay backfill | Paginate by year, sleep 12 s between requests, accept ~6 hr backfill time |
| SBDB API ambiguity for predicted vs observed close approaches | Filter on `data_arc > 0` to keep observed-only; predictions go in a separate table only if needed later |
| SQLite write contention with read traffic from the portal | WAL mode is already on; ingest writes in 1000-row transactions; readers are unaffected |
| Schema drift between dev and EC2 | `init_db.py` runs `CREATE TABLE IF NOT EXISTS` for new tables; no destructive migrations in this phase |

---

## 8. What this unlocks

Once Phase 1.0 is shipped:

- The Signal Engine (Phase 1.1) can be implemented in ~50 lines:
  `score = composite(percentile(magnitude), surprise(percentile, recent_window), reach_lookup(event_type))`.
- Interstellar Watch (Phase 2) can use `baseline_neo_close_approach`
  as the negative class when scoring `e > 1` candidates.
- Earth Impact Layer (Phase 3) gets a real "this storm is in the top
  N% of the last 30 years" anchor for severity claims.
- What-Changed (Phase 4) gets a free time-series corpus of *computed*
  scores, simply by snapshotting the engine output nightly.

This is the single piece of infrastructure that makes the rest of
the platform claims defensible. Build it first, build it carefully,
ship nothing user-facing on top of it until it's stable.
