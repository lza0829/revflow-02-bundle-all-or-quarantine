from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
FIXTURES_DIR = Path(
    os.environ.get("FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures")
)
DB_PATH = DATA_DIR / "app.db"


def connect() -> sqlite3.Connection:
    # 连接时动态解析，保证测试可通过 DATA_DIR 切换独立库
    data_dir = Path(
        os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data")
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "app.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
