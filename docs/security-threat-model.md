# Deep Space Research — Security Threat Model

**Author:** Prabhu Sadasivam  
**Classification:** Public   

## Table of Contents

1. [Scope & System Boundaries](#1-scope--system-boundaries)
2. [Data Flow Diagram](#2-data-flow-diagram)
3. [Trust Boundaries](#3-trust-boundaries)
4. [Asset Inventory](#4-asset-inventory)
5. [Threat Analysis (STRIDE)](#5-threat-analysis-stride)
6. [Attack Surface Assessment](#6-attack-surface-assessment)
7. [Controls in Place](#7-controls-in-place)
8. [Residual Risks & Mitigations](#8-residual-risks--mitigations)
9. [Secrets Management](#9-secrets-management)
10. [Dependency & Supply Chain Security](#10-dependency--supply-chain-security)
11. [Incident Response](#11-incident-response)
12. [Security Checklist for Public Repository](#12-security-checklist-for-public-repository)

## 1. Scope & System Boundaries

This threat model covers the **deep_space_db** subsystem — a local SQLite analytics database with S3 backup. It does NOT cover the Flask web application (`voyager1_web_app.py`) or the EC2 deployment, which have their own security postures.

### In Scope

| Component | Description |
|-----------|-------------|
| `schema.sql` | DDL definitions for 15 tables |
| `init_db.py` | Schema creation + data ingestion from CSV/JSON/computed |
| `s3_backup.py` | S3 backup, list, and restore operations |
| `deep_space_research.db` | SQLite database file (local, not committed) |
| S3 bucket | Remote backup storage (`S3_BACKUP_BUCKET` env var) |
| Upstream data files | CSVs, JSONs from sibling project directories |

### Out of Scope

- Flask web application and its API endpoints
- EC2 instance, Nginx, HTTPS/TLS configuration
- GitHub repository access controls
- User workstation security

## 2. Data Flow Diagram

```
                                    TRUST BOUNDARY: Local Workstation
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  ┌─────────────────────┐                                                │
│  │  Upstream Data Files │     File-system read (trusted)                │
│  │  CSV, JSON, computed │─────────────────────┐                         │
│  └─────────────────────┘                      │                         │
│                                               ▼                         │
│                                    ┌─────────────────────┐              │
│                                    │    init_db.py        │             │
│                                    │    (Python stdlib)   │             │
│                                    │                      │             │
│                                    │  ┌────────────────┐  │             │
│                                    │  │ Parameterized  │  │             │
│                                    │  │ SQL inserts    │  │             │
│                                    │  └───────┬────────┘  │             │
│                                    └──────────┼───────────┘             │
│                                               │                         │
│                                               ▼                         │
│                                    ┌─────────────────────┐              │
│                                    │ deep_space_research  │             │
│                                    │       .db            │             │
│                                    │  (SQLite, WAL mode)  │             │
│                                    └──────────┬───────────┘             │
│                                               │                         │
│                                               ▼                         │
│                                    ┌─────────────────────┐              │
│                                    │   s3_backup.py       │             │
│                                    │   (Python + AWS CLI) │             │
│                                    └──────────┬───────────┘             │
│                                               │                         │
└───────────────────────────────────────────────┼─────────────────────────┘
                                                │
                             TRUST BOUNDARY: Network (HTTPS)
                                                │
                                                ▼
                                    ┌─────────────────────┐
                                    │   AWS S3 Bucket     │
                                    │   (Versioned,       │
                                    │    Private,         │
                                    │    SSE-S3 encrypted)│
                                    └─────────────────────┘
```

## 3. Trust Boundaries

| Boundary | From → To | Protocol | Authentication |
|----------|-----------|----------|----------------|
| **TB-1: File system** | Upstream CSVs/JSONs → `init_db.py` | Local file read | OS file permissions |
| **TB-2: Database** | `init_db.py` → SQLite | In-process `sqlite3` | File permissions |
| **TB-3: S3 upload** | `s3_backup.py` → S3 | HTTPS | AWS IAM credentials (CLI profile) |
| **TB-4: S3 download** | S3 → `s3_backup.py` | HTTPS | AWS IAM credentials (CLI profile) |
| **TB-5: User queries** | Analyst → SQLite | In-process `sqlite3` or pandas | File permissions |

## 4. Asset Inventory

### 4.1 Data Assets

| Asset | Confidentiality | Integrity | Availability | Notes |
|-------|:-:|:-:|:-:|-------|
| Research telemetry (Voyager 1, 3I/ATLAS) | Low | **High** | Medium | Public NASA data — integrity matters most for correct analysis |
| Simulation parameters (Black Hole) | Low | **High** | Medium | Derived from physical constants — must be accurate |
| Research insights | **Medium** | **High** | Medium | Intellectual property — unpublished hypotheses |
| Ingestion/backup logs | Low | Medium | Low | Operational metadata |
| AWS credentials | **Critical** | **Critical** | High | IAM keys grant S3 write access |
| S3 backups | Low | **High** | **High** | Disaster recovery depends on backup availability |

### 4.2 System Assets

| Asset | Description | Risk if Compromised |
|-------|-------------|---------------------|
| `deep_space_research.db` | SQLite database file | Data corruption or loss |
| `s3_backup.py` | Backup script | Malicious backup/overwrite of S3 data |
| AWS CLI credentials | `~/.aws/credentials` | Unauthorized S3 access, data exfiltration, cost abuse |
| S3 bucket | Versioned backup storage | Backup deletion or poisoning |

## 5. Threat Analysis (STRIDE)

### 5.1 Spoofing

| ID | Threat | Target | Likelihood | Impact | Mitigation |
|----|--------|--------|:----------:|:------:|------------|
| S-1 | Attacker spoofs AWS credentials to access S3 bucket | S3 bucket | Low | High | IAM user with least-privilege policy; MFA on AWS account |
| S-2 | Malicious data file injected into upstream directory | `init_db.py` | Low | Medium | File-system permissions on data directories; ingestion log provides audit trail |

### 5.2 Tampering

| ID | Threat | Target | Likelihood | Impact | Mitigation |
|----|--------|--------|:----------:|:------:|------------|
| T-1 | Database file modified directly on disk | `.db` file | Low | High | OS file permissions (`chmod 600`); S3 backup for recovery; WAL mode provides crash consistency |
| T-2 | Upstream CSV/JSON tampered before ingestion | Source files | Low | **High** | `source` column on every row tracks provenance; Git history on source project files |
| T-3 | S3 backup replaced with corrupted file | S3 bucket | Very Low | High | S3 versioning allows recovery of previous versions; restore validates with `sqlite3` integrity check |
| T-4 | Man-in-the-middle on S3 upload/download | Network | Very Low | High | AWS CLI enforces HTTPS/TLS for all S3 operations |

### 5.3 Repudiation

| ID | Threat | Target | Likelihood | Impact | Mitigation |
|----|--------|--------|:----------:|:------:|------------|
| R-1 | Data ingested without attribution | `init_db.py` | Medium | Medium | `ingestion_log` table records source file, timestamp, row count, and status for every ingestion |
| R-2 | S3 backup claimed but not actually uploaded | `s3_backup.py` | Low | Medium | `s3_backup_log` table records backup metadata; S3 server access logging can be enabled |

### 5.4 Information Disclosure

| ID | Threat | Target | Likelihood | Impact | Mitigation |
|----|--------|--------|:----------:|:------:|------------|
| I-1 | `.db` file committed to public Git repo | GitHub | **High** (if no `.gitignore`) | Medium | `.gitignore` excludes `*.db`, `*.db-wal`, `*.db-shm` |
| I-2 | AWS credentials leaked in source code | GitHub | Medium | **Critical** | Credentials stored in `~/.aws/credentials`, never in code; bucket name via env var |
| I-3 | AWS account ID exposed in documentation | GitHub | Medium | Medium | Redacted from `database-architecture.md`; uses `<YOUR_AWS_ACCOUNT_ID>` placeholders |
| I-4 | S3 bucket name enables probing | GitHub | Medium | Low | Bucket name moved to env var `S3_BACKUP_BUCKET`; all public access blocked on bucket |
| I-5 | Research insights visible to unauthorized users | `.db` file | Low | Medium | File permissions; not committed to repo; S3 bucket is private |

### 5.5 Denial of Service

| ID | Threat | Target | Likelihood | Impact | Mitigation |
|----|--------|--------|:----------:|:------:|------------|
| D-1 | Large data ingestion fills disk | Local disk | Low | Medium | SQLite file grows incrementally; monitor disk space; WAL checkpoint keeps `.db-wal` bounded |
| D-2 | S3 bucket storage cost abuse | AWS bill | Very Low | Low | S3 lifecycle rules to archive old backups; AWS budget alarms |
| D-3 | SQLite write lock held indefinitely | Database | Very Low | Low | Single-user workflow; no long-running write transactions |

### 5.6 Elevation of Privilege

| ID | Threat | Target | Likelihood | Impact | Mitigation |
|----|--------|--------|:----------:|:------:|------------|
| E-1 | Compromised AWS credentials used to access other services | AWS account | Low | **Critical** | Least-privilege IAM policy scoped to single S3 bucket; no `s3:*` wildcard |
| E-2 | SQL injection via crafted CSV data | `init_db.py` | Very Low | Medium | All SQL uses parameterized `?` placeholders — not string concatenation |

## 6. Attack Surface Assessment

### 6.1 Entry Points

| Entry Point | Type | Exposed To | Risk Level |
|-------------|------|-----------|:----------:|
| Upstream CSV/JSON files | File system | Local user / other processes | Low |
| `init_db.py` CLI | Command line | Local user | Low |
| `s3_backup.py` CLI | Command line | Local user | Low |
| AWS CLI credentials | Config file | Local user / malware | Medium |
| S3 API (via AWS CLI) | Network (HTTPS) | AWS IAM-authenticated callers | Medium |

### 6.2 No Network Listeners

The deep_space_db system runs entirely as local CLI tools. There are:

- **No open ports** — SQLite is an embedded library, not a server
- **No HTTP/REST endpoints** — queries are in-process via Python `sqlite3`
- **No RPC or socket interfaces**

This eliminates entire classes of network-based attacks (remote code execution, DDoS, protocol exploits).

## 7. Controls in Place

### 7.1 Data Integrity Controls

| Control | Implementation | Protects Against |
|---------|----------------|------------------|
| **Parameterized SQL** | All `INSERT`/`SELECT` use `?` placeholders in `init_db.py` | SQL injection (T-2, E-2) |
| **WAL mode** | `PRAGMA journal_mode=WAL` in schema and connection | Database corruption on crash (T-1) |
| **WAL checkpoint before backup** | `PRAGMA wal_checkpoint(TRUNCATE)` in `s3_backup.py` | Inconsistent backup snapshots (T-3) |
| **Restore validation** | `SELECT COUNT(*) FROM sqlite_master` on downloaded `.db` | Corrupted restore files (T-3) |
| **Pre-restore backup** | Copy `.db` → `.db.pre-restore` before overwrite | Accidental data loss during restore |
| **Source provenance** | `source` column on every data row | Data quality disputes (R-1, T-2) |
| **Idempotent ingestion** | DELETE-then-INSERT scoped by source | Row duplication; safe re-runs |
| **Transaction wrapping** | Single `conn.commit()` or full `conn.rollback()` | Partial ingestion state |

### 7.2 Access Controls

| Control | Implementation | Protects Against |
|---------|----------------|------------------|
| **S3 public access block** | All four block settings enabled on bucket | Unauthorized public access (S-1, I-4) |
| **S3 versioning** | Enabled on bucket | Accidental or malicious backup deletion (T-3) |
| **S3 SSE-S3 encryption** | AWS default server-side encryption | Data exposure if storage media stolen |
| **HTTPS enforcement** | AWS CLI always uses TLS for S3 operations | Man-in-the-middle (T-4) |
| **No credentials in code** | Bucket name via `S3_BACKUP_BUCKET` env var; AWS creds in `~/.aws/` | Credential leakage (I-2) |
| **`.gitignore`** | Excludes `*.db`, `*.db-wal`, `*.db-shm`, `.env` | Database and secrets committed to repo (I-1, I-2) |

### 7.3 Audit Controls

| Control | Implementation | Protects Against |
|---------|----------------|------------------|
| **Ingestion log** | `ingestion_log` table: source, table, rows, status, timestamp | Repudiation of data changes (R-1) |
| **Backup log** | `s3_backup_log` table: S3 key, file size, table/row counts | Repudiation of backup operations (R-2) |
| **S3 versioning** | All objects retain version history | Forensics after tampering (T-3) |
| **`ingested_at` on every row** | `DEFAULT (datetime('now'))` | Temporal auditing of data freshness |

## 8. Residual Risks & Mitigations

### 8.1 Accepted Risks

| Risk ID | Risk | Severity | Rationale for Acceptance |
|---------|------|:--------:|--------------------------|
| I-5 | Research insights readable by anyone with file access | Medium | Single-user workstation; no PII; acceptable for research context |
| D-1 | Disk fill from large ingestion | Medium | Manual ingestion with small datasets; monitoring is proportional to scale |
| T-1 | Local file tampering | Medium | Workstation compromise implies all-asset compromise; out of scope |

### 8.2 Risks Requiring Action

| Risk ID | Risk | Current State | Recommended Action | Priority |
|---------|------|:------------:|---------------------|:--------:|
| E-1 | Over-privileged AWS credentials | Root account in use | Create scoped IAM user with S3-only policy | **High** |
| I-2 | AWS credential format in `~/.aws/` | Long-lived access keys | Rotate keys; consider SSO or temporary credentials | **High** |
| R-2 | Backup not independently verifiable | Log is in the same `.db` | Enable S3 server access logging for independent audit | Medium |
| D-2 | No cost alerts on S3 usage | No budget alarm | Add AWS Budgets alarm at $1/month threshold | Low |

## 9. Secrets Management

### 9.1 Secrets Inventory

| Secret | Storage Location | Rotation Policy | Scope |
|--------|-----------------|-----------------|-------|
| AWS Access Key ID | `~/.aws/credentials` | Manual (rotate quarterly recommended) | S3 backup operations |
| AWS Secret Access Key | `~/.aws/credentials` | Manual | S3 backup operations |
| S3 bucket name | `S3_BACKUP_BUCKET` env var | N/A (not a secret, but operational detail) | `s3_backup.py` |

### 9.2 What is NOT a Secret

| Item | Reason |
|------|--------|
| Database file | Contains public NASA data + research insights (no PII) |
| Schema SQL | Table definitions are not sensitive |
| CSV/JSON source files | Public research data |
| NASA API key | `DEMO_KEY` used; rate-limited but not privileged |

### 9.3 Secrets NOT in Version Control

The following are confirmed excluded from Git:

- `*.db` files — via `.gitignore`
- `.env` files — via `.gitignore`
- `~/.aws/credentials` — outside repo entirely
- No hardcoded credentials found in any `.py` or `.sql` file

## 10. Dependency & Supply Chain Security

### 10.1 Direct Dependencies

| Component | Source | Pinned | Supply Chain Risk |
|-----------|--------|:------:|:-----------------:|
| Python `sqlite3` | Python stdlib | Yes (bundled) | **None** — ships with Python |
| Python `csv` | Python stdlib | Yes (bundled) | **None** |
| Python `json` | Python stdlib | Yes (bundled) | **None** |
| Python `math` | Python stdlib | Yes (bundled) | **None** |
| Python `subprocess` | Python stdlib | Yes (bundled) | **None** |
| AWS CLI | System install | Version varies | Low — official AWS distribution |

### 10.2 No Third-Party Packages

The `init_db.py` and `s3_backup.py` scripts use **zero third-party packages**. There is:

- No `requirements.txt` for the database subsystem
- No `pip install` needed
- No transitive dependency tree to audit
- No exposure to typosquatting, dependency confusion, or compromised packages

This is a deliberate architectural decision to minimize supply chain attack surface.

### 10.3 Upstream Data Trust

| Source | Trust Level | Validation |
|--------|:-----------:|------------|
| NASA SPDF / PDS / JPL | High | Official US government scientific data |
| MAST (STScI) | High | Curated astronomical archive |
| MPC (Minor Planet Center) | High | IAU-sanctioned orbital data |
| Local CSV/JSON | Medium | Git-tracked; diff-auditable |
| Computed values (`blackhole_simulations`) | High | Derived from well-known physical constants in-code |

## 11. Incident Response

### 11.1 Database Corruption

```
1. Stop any running ingestion or backup process
2. Check WAL file: ls -la deep_space_research.db-wal
3. Attempt recovery: sqlite3 deep_space_research.db "PRAGMA integrity_check"
4. If corrupt: python s3_backup.py restore
5. Verify: python -c "import sqlite3; c=sqlite3.connect('deep_space_research.db'); print(c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone())"
6. Re-ingest if needed: python init_db.py --ingest-only
```

### 11.2 AWS Credential Compromise

```
1. IMMEDIATELY deactivate the exposed key:
   aws iam update-access-key --access-key-id <KEY_ID> --status Inactive
2. Create new credentials:
   aws iam create-access-key --user-name <IAM_USER>
3. Update local profile:
   aws configure
4. Review S3 access logs for unauthorized operations
5. Verify S3 bucket versioning — restore overwritten backups if needed
6. Rotate to temporary credentials (AWS SSO or STS AssumeRole) to prevent recurrence
```

### 11.3 S3 Backup Tampering

```
1. List object versions:
   aws s3api list-object-versions --bucket $S3_BACKUP_BUCKET --prefix db-backups/
2. Restore previous version:
   aws s3api get-object --bucket $S3_BACKUP_BUCKET --key db-backups/deep_space_research_latest.db --version-id <PREVIOUS_VERSION_ID> restored.db
3. Validate restored file:
   sqlite3 restored.db "PRAGMA integrity_check"
4. Investigate: check CloudTrail for who modified the object
```

## 12. Security Checklist for Public Repository

Before publishing `deep_space_db` as a public GitHub repository:

### Pre-Publish Checklist

- [x] **AWS Account ID removed** from all documentation
- [x] **S3 bucket name externalized** to `S3_BACKUP_BUCKET` environment variable
- [x] **AWS region externalized** to `AWS_DEFAULT_REGION` environment variable
- [x] **IAM ARN references removed** from documentation (replaced with placeholders)
- [x] **`.gitignore` created** excluding `*.db`, `*.db-wal`, `*.db-shm`, `*.db.restore`, `*.db.pre-restore`, `.env`
- [x] **No credentials in source code** — verified across all `.py` and `.sql` files
- [x] **No IP addresses** in any tracked file
- [x] **No SSH key paths** in any tracked file
- [x] **No personal names or emails** in any tracked file
- [x] **Parameterized SQL** — all queries use `?` placeholders, no string interpolation
- [x] **No third-party dependencies** — stdlib only, no supply chain risk
- [ ] **IAM user created** (replace root credentials) — *pending operator action*
- [ ] **S3 server access logging enabled** — *pending operator action*
- [ ] **AWS Budgets alarm configured** — *pending operator action*

### Post-Publish Monitoring

- Run `git log --all -p | grep -iE "AKIA|aws_secret|password|token"` to verify no secrets in Git history
- Enable GitHub secret scanning on the repository
- Enable Dependabot alerts (minimal surface since no dependencies, but good practice)
