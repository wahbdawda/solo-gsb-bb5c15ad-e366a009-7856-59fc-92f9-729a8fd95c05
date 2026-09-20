"""SQLite 连接与事务管理。

设计要点：
- 单连接 + 可重入锁：SQLite 同一时刻只允许一个写事务，
  用进程内锁把所有读写串行化，避免跨线程复用连接的游标竞争。
- isolation_level=None（自动提交模式），事务边界由代码显式控制：
  所有写路径统一走 BEGIN IMMEDIATE，进入事务即持有写锁，
  保证“检查修订号 -> 写入片段 -> 推进水位线 -> 生成定稿”是原子的。
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel   TEXT PRIMARY KEY,
    watermark INTEGER NOT NULL DEFAULT 0
);

-- 待定稿缓冲区：仅保存尚未进入连续前缀的片段
CREATE TABLE IF NOT EXISTS fragments (
    channel     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    text        TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, seq)
);

-- 定稿表：写入后不可修改，主键防止并发下重复定稿
CREATE TABLE IF NOT EXISTS finalized (
    channel      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    revision     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    finalized_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, seq)
);

-- 永久缺口裁决记录：操作员裁决区间 [start_seq, end_seq] 永久丢失。
-- 按序号落行，并冗余区间端点用于幂等返回及定稿查询的缺口标记
CREATE TABLE IF NOT EXISTS gap_decisions (
    channel        TEXT NOT NULL,
    start_seq      INTEGER NOT NULL,
    end_seq        INTEGER NOT NULL,
    released_until INTEGER NOT NULL,
    decided_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, start_seq, end_seq)
);

-- 被跳过序号：每个序号一行，主键防止并发下重复裁决
CREATE TABLE IF NOT EXISTS gaps (
    channel   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    start_seq INTEGER NOT NULL,
    end_seq   INTEGER NOT NULL,
    PRIMARY KEY (channel, seq),
    FOREIGN KEY (channel, start_seq, end_seq) REFERENCES gap_decisions
);

-- 定稿更正：只追加、不可修改/删除，原定稿记录永不改写。
-- version 对每个 (channel, seq) 从 1 起连续递增，
-- 主键 (channel, seq, version) 防止并发下重复追加同一版本
CREATE TABLE IF NOT EXISTS corrections (
    channel    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (channel, seq, version),
    FOREIGN KEY (channel, seq) REFERENCES finalized (channel, seq)
);
"""

_lock = threading.RLock()
_conn: "sqlite3.Connection | None" = None


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/subtitles.db")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = db_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _conn = conn
    return _conn


def init_db() -> None:
    """建表（幂等），服务启动时调用。"""
    with _lock:
        get_conn().executescript(SCHEMA)


@contextmanager
def write_tx():
    """串行化的 IMMEDIATE 写事务：提交片段与推进水位线必须在同一事务内。"""
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


@contextmanager
def read_tx():
    """读路径同样走锁，避免与进行中的写事务共享连接状态。"""
    with _lock:
        yield get_conn()
