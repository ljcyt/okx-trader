# -*- coding: utf-8 -*-
"""SQLite 连接纪律。

- 一个进程、一个写者（交易循环）；WAL 下读永不阻塞写、写永不阻塞读
- 每线程一个连接（threading.local），绝不用 check_same_thread=False
- journal_mode=WAL 在 init_db 时设一次（写进文件持久生效）
- 写连接 busy_timeout=5000；读连接用 mode=ro 打开——面板的 bug 物理上改不了数据
"""
import os
import sqlite3
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")


def init_db(db_path):
    """建库：执行 schema.sql + 设 WAL。幂等（全部 IF NOT EXISTS）。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()
    return db_path


class Store:
    """写者用 writer()，只读端（面板/CLI 查询）用 reader()。"""

    def __init__(self, db_path):
        self.db_path = db_path
        self._local = threading.local()
        init_db(db_path)

    def writer(self) -> sqlite3.Connection:
        conn = getattr(self._local, "w", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.row_factory = sqlite3.Row
            self._local.w = conn
        return conn

    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "r", None)
        if conn is None:
            uri = f"file:{self.db_path.replace(os.sep, '/')}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.row_factory = sqlite3.Row
            self._local.r = conn
        return conn

    # ── 便捷写入 ────────────────────────────────────────────────

    def execute(self, sql, params=()):
        cur = self.writer().execute(sql, params)
        self.writer().commit()
        return cur.lastrowid

    def executemany(self, sql, seq):
        cur = self.writer().executemany(sql, seq)
        self.writer().commit()
        return cur.rowcount

    def query(self, sql, params=()):
        """读查询（走只读连接）。"""
        return self.reader().execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.reader().execute(sql, params).fetchone()

    # ── run_state（取代 state_*.json）────────────────────────────

    def state_get(self, env, key, default=None):
        import json
        row = self.query_one("SELECT value FROM run_state WHERE env=? AND key=?",
                             (env, key))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def state_set(self, env, key, value):
        import json
        import time
        self.execute(
            "INSERT INTO run_state(env, key, value, updated_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(env, key) DO UPDATE SET "
            "value=excluded.value, updated_ts=excluded.updated_ts",
            (env, key, json.dumps(value), time.time()))
