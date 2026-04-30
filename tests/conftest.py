"""Make tests importable without installing the package.

Adds the parent of ``deep_space_db/`` to sys.path so that
``from deep_space_db.ingest.baseline._common import ...`` resolves
when tests are run from inside ``deep_space_db/tests/``.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make `deep_space_db` importable as a package
_PKG_PARENT = Path(__file__).resolve().parents[2]
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

# And make `baseline_queries` (a top-level module inside deep_space_db)
# importable when tests refer to it directly.
_PKG_DIR = Path(__file__).resolve().parents[1]
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))


@pytest.fixture
def temp_db(monkeypatch):
    """Provide a fresh SQLite DB seeded from schema.sql.

    Sets ``DEEP_SPACE_DB_PATH`` so both the ingest helpers and
    ``baseline_queries`` see the temp database.
    """
    import sqlite3

    schema_path = _PKG_DIR / "schema.sql"
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        with open(schema_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
        conn.close()
        monkeypatch.setenv("DEEP_SPACE_DB_PATH", str(db_path))
        # Bust the percentile cache between tests
        try:
            from deep_space_db import baseline_queries  # type: ignore
            baseline_queries.clear_cache()
        except ImportError:
            pass
        yield db_path
