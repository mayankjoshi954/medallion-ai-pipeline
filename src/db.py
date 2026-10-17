"""Connection helper, transaction scope and idempotent DDL application."""
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

log = logging.getLogger(__name__)

DEFAULT_URL = "postgresql://pipeline:pipeline@localhost:5433/tickets"


def connect():
    return psycopg2.connect(os.getenv("DATABASE_URL", DEFAULT_URL))


@contextmanager
def transaction():
    """Commit on success, roll back on any error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_ddl() -> None:
    """Run sql/*.sql in filename order. Every statement is IF NOT EXISTS / OR REPLACE, so re-running is safe."""
    files = sorted((ROOT / "sql").glob("[0-9][0-9]_*.sql"))
    with transaction() as conn, conn.cursor() as cur:
        for f in files:
            cur.execute(f.read_text())
    log.info("DDL applied (%d files)", len(files))
