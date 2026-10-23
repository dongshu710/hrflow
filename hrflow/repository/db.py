"""数据库连接与初始化。

只暴露两个能力：拿到连接、初始化表结构。业务规则一律不放在这一层。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "hrflow.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建表（幂等）。"""
    conn = connect(db_path)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务：请假审批涉及"改状态 + 扣额度 + 写流水"，必须同成功同失败。"""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reset_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """清空重建，用于跑演示数据。"""
    if os.path.exists(db_path):
        os.remove(db_path)
    return init_db(db_path)
