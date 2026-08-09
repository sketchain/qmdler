"""数据库 schema 与迁移."""

from __future__ import annotations

import aiosqlite

SCHEMA_VERSION = 2

_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    source_id       TEXT NOT NULL DEFAULT '',
    source_ref      TEXT NOT NULL DEFAULT '',
    save_root       TEXT NOT NULL,
    quality_chain   TEXT NOT NULL,
    options         TEXT NOT NULL,
    status          TEXT NOT NULL,
    pause_reason    TEXT NOT NULL DEFAULT '',
    total_items     INTEGER NOT NULL DEFAULT 0,
    est_total_bytes INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS task_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    position           INTEGER NOT NULL,
    songmid            TEXT NOT NULL,
    song_json          TEXT NOT NULL,
    selected           INTEGER NOT NULL DEFAULT 1,
    status             TEXT NOT NULL DEFAULT 'pending',
    requested_quality  TEXT NOT NULL DEFAULT '',
    actual_quality     TEXT NOT NULL DEFAULT '',
    degraded           INTEGER NOT NULL DEFAULT 0,
    trial_reason       TEXT NOT NULL DEFAULT '',
    target_path        TEXT NOT NULL DEFAULT '',
    bytes_expected     INTEGER NOT NULL DEFAULT 0,
    bytes_downloaded   INTEGER NOT NULL DEFAULT 0,
    api_result_code    INTEGER,
    error_type         TEXT NOT NULL DEFAULT '',
    error_message      TEXT NOT NULL DEFAULT '',
    retry_count        INTEGER NOT NULL DEFAULT 0,
    lyric_status       TEXT NOT NULL DEFAULT 'skipped',
    cover_status       TEXT NOT NULL DEFAULT 'skipped',
    tag_status         TEXT NOT NULL DEFAULT 'skipped',
    -- 四层校验里有层没跑成 (如容器读不出时长). 记下来, 汇总报告要把这些歌
    -- 从「已完成四层校验」里摘出去 —— 否则报告会给出全部通过的假象.
    verify_incomplete  INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    started_at         INTEGER,
    finished_at        INTEGER,
    UNIQUE (task_id, songmid, requested_quality)
);
CREATE INDEX IF NOT EXISTS idx_items_task_status ON task_items(task_id, status);
CREATE INDEX IF NOT EXISTS idx_items_songmid ON task_items(songmid);

CREATE TABLE IF NOT EXISTS downloads (
    songmid      TEXT NOT NULL,
    quality      TEXT NOT NULL,
    path         TEXT NOT NULL,
    file_size    INTEGER NOT NULL,
    task_id      TEXT,
    completed_at INTEGER NOT NULL,
    PRIMARY KEY (songmid, quality)
);

CREATE TABLE IF NOT EXISTS task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    item_id INTEGER,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);
"""


async def migrate(conn: aiosqlite.Connection) -> None:
    """建表并把 schema 升到最新版本."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")

    await conn.executescript(_V1)

    async with conn.execute("SELECT version FROM schema_meta LIMIT 1") as cursor:
        row = await cursor.fetchone()
    if row is None:
        await conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] < SCHEMA_VERSION:
        # `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列, 所以老库要走 ALTER.
        await _upgrade(conn, row[0])
        await conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
    await conn.commit()


async def _upgrade(conn: aiosqlite.Connection, from_version: int) -> None:
    """按版本逐级升级已存在的库."""
    if from_version < 2:
        await _add_column_if_missing(
            conn, "task_items", "verify_incomplete", "INTEGER NOT NULL DEFAULT 0",
        )


async def _add_column_if_missing(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """幂等加列 —— SQLite 没有 ``ADD COLUMN IF NOT EXISTS``."""
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if column not in columns:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
