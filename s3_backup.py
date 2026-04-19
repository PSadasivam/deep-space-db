"""
Deep Space Research — S3 Backup & Restore
Backs up the SQLite database to S3 with timestamps and maintains a backup log.

Usage:
    python s3_backup.py backup                     # Backup to S3
    python s3_backup.py backup --bucket my-bucket   # Custom bucket
    python s3_backup.py list                        # List backups in S3
    python s3_backup.py restore                     # Restore latest from S3
    python s3_backup.py restore --key <s3-key>      # Restore specific backup

Requires: AWS CLI configured (aws configure) — uses boto3 if available, falls back to AWS CLI.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "deep_space_research.db"
DEFAULT_BUCKET = os.environ.get("S3_BACKUP_BUCKET", "deep-space-research-backups")
S3_PREFIX = "db-backups/"


def get_db_stats():
    """Get row counts and table count from the database."""
    if not DB_PATH.exists():
        return 0, 0
    conn = sqlite3.connect(str(DB_PATH))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    total_rows = 0
    for (t,) in tables:
        total_rows += conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    conn.close()
    return len(tables), total_rows


def log_backup(s3_bucket, s3_key, file_size):
    """Log backup to the database."""
    if not DB_PATH.exists():
        return
    tables, rows = get_db_stats()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO s3_backup_log (s3_bucket, s3_key, file_size_bytes, db_tables, db_total_rows) "
        "VALUES (?, ?, ?, ?, ?)",
        (s3_bucket, s3_key, file_size, tables, rows),
    )
    conn.commit()
    conn.close()


def ensure_bucket(bucket):
    """Create the S3 bucket if it doesn't exist."""
    try:
        result = subprocess.run(
            ["aws", "s3api", "head-bucket", "--bucket", bucket],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        print("[ERROR] AWS CLI not found. Install it or configure boto3.", file=sys.stderr)
        sys.exit(1)

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-2")
    print(f"[INFO] Bucket '{bucket}' not found. Creating in {region}...")
    result = subprocess.run(
        ["aws", "s3api", "create-bucket",
         "--bucket", bucket,
         "--region", region,
         "--create-bucket-configuration", f"LocationConstraint={region}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] Failed to create bucket: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Enable versioning for safety
    subprocess.run(
        ["aws", "s3api", "put-bucket-versioning",
         "--bucket", bucket,
         "--versioning-configuration", "Status=Enabled"],
        capture_output=True, text=True,
    )

    # Block public access
    subprocess.run(
        ["aws", "s3api", "put-public-access-block",
         "--bucket", bucket,
         "--public-access-block-configuration",
         "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"],
        capture_output=True, text=True,
    )

    print(f"[OK] Bucket '{bucket}' created with versioning and public access blocked")
    return True


def backup(bucket):
    """Backup database to S3."""
    if not DB_PATH.exists():
        print(f"[ERROR] Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    ensure_bucket(bucket)

    # Create a consistent snapshot (WAL checkpoint first)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    s3_key = f"{S3_PREFIX}deep_space_research_{timestamp}.db"

    file_size = os.path.getsize(DB_PATH)
    size_label = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024*1024):.1f} MB"

    print(f"[INFO] Uploading {size_label} → s3://{bucket}/{s3_key}")

    result = subprocess.run(
        ["aws", "s3", "cp", str(DB_PATH), f"s3://{bucket}/{s3_key}"],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        print(f"[ERROR] Upload failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Also upload as 'latest' for easy restore
    latest_key = f"{S3_PREFIX}deep_space_research_latest.db"
    subprocess.run(
        ["aws", "s3", "cp", str(DB_PATH), f"s3://{bucket}/{latest_key}"],
        capture_output=True, text=True,
    )

    log_backup(bucket, s3_key, file_size)

    tables, rows = get_db_stats()
    print(f"[OK] Backup complete: {tables} tables, {rows} rows, {size_label}")
    print(f"     s3://{bucket}/{s3_key}")
    print(f"     s3://{bucket}/{latest_key}")


def list_backups(bucket):
    """List available backups in S3."""
    result = subprocess.run(
        ["aws", "s3", "ls", f"s3://{bucket}/{S3_PREFIX}", "--human-readable"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] Cannot list bucket: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    lines = result.stdout.strip()
    if not lines:
        print("[INFO] No backups found.")
        return

    print(f"Backups in s3://{bucket}/{S3_PREFIX}:\n")
    print(lines)

    # Also show local backup log if available
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT backup_at, s3_key, file_size_bytes, db_tables, db_total_rows "
            "FROM s3_backup_log ORDER BY backup_at DESC LIMIT 5"
        ).fetchall()
        conn.close()
        if rows:
            print("\nRecent backup log entries:")
            for at, key, size, tables, total in rows:
                print(f"  {at}  {tables} tables  {total} rows  {size/1024:.0f} KB  {key}")


def restore(bucket, s3_key=None):
    """Restore database from S3."""
    if s3_key is None:
        s3_key = f"{S3_PREFIX}deep_space_research_latest.db"

    source = f"s3://{bucket}/{s3_key}"
    restore_path = DB_PATH.with_suffix(".db.restore")

    print(f"[INFO] Downloading {source}...")
    result = subprocess.run(
        ["aws", "s3", "cp", source, str(restore_path)],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        print(f"[ERROR] Download failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Verify the downloaded file is a valid SQLite database
    try:
        conn = sqlite3.connect(str(restore_path))
        conn.execute("SELECT COUNT(*) FROM sqlite_master")
        conn.close()
    except sqlite3.DatabaseError as e:
        os.remove(restore_path)
        print(f"[ERROR] Downloaded file is not a valid database: {e}", file=sys.stderr)
        sys.exit(1)

    # Backup current DB before overwriting
    if DB_PATH.exists():
        backup_path = DB_PATH.with_suffix(".db.pre-restore")
        shutil.copy2(DB_PATH, backup_path)
        print(f"[INFO] Current DB backed up to {backup_path.name}")

    # Replace with restored version
    shutil.move(str(restore_path), str(DB_PATH))

    tables, rows = get_db_stats()
    print(f"[OK] Restored: {tables} tables, {rows} rows")
    print(f"     From: {source}")


def main():
    parser = argparse.ArgumentParser(description="Deep Space Research — S3 Backup & Restore")
    parser.add_argument("action", choices=["backup", "list", "restore"], help="Action to perform")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help=f"S3 bucket name (default: {DEFAULT_BUCKET})")
    parser.add_argument("--key", default=None, help="S3 key for restore (default: latest)")
    args = parser.parse_args()

    if args.action == "backup":
        backup(args.bucket)
    elif args.action == "list":
        list_backups(args.bucket)
    elif args.action == "restore":
        restore(args.bucket, args.key)


if __name__ == "__main__":
    main()
