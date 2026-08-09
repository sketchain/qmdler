"""SQLite 仓储层.

引擎与服务层只跟这里打交道, 不直接写 SQL.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

from ..models import (
    ItemRecord,
    ItemStatus,
    SongEntry,
    SubStatus,
    TaskRecord,
    TaskStatus,
    dumps,
    loads,
)
from .migrations import migrate


def _now() -> int:
    return int(time.time())


def _row_to_task(row: aiosqlite.Row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        name=row["name"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        source_ref=row["source_ref"],
        save_root=row["save_root"],
        quality_chain=loads(row["quality_chain"], []) or [],
        options=loads(row["options"], {}) or {},
        status=TaskStatus(row["status"]),
        pause_reason=row["pause_reason"],
        total_items=row["total_items"],
        est_total_bytes=row["est_total_bytes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _row_to_item(row: aiosqlite.Row) -> ItemRecord:
    return ItemRecord(
        id=row["id"],
        task_id=row["task_id"],
        position=row["position"],
        entry=SongEntry.from_dict(loads(row["song_json"], {}) or {}),
        selected=bool(row["selected"]),
        status=ItemStatus(row["status"]),
        requested_quality=row["requested_quality"],
        actual_quality=row["actual_quality"],
        degraded=bool(row["degraded"]),
        trial_reason=row["trial_reason"],
        target_path=row["target_path"],
        bytes_expected=row["bytes_expected"],
        bytes_downloaded=row["bytes_downloaded"],
        api_result_code=row["api_result_code"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        retry_count=row["retry_count"],
        lyric_status=SubStatus(row["lyric_status"]),
        cover_status=SubStatus(row["cover_status"]),
        tag_status=SubStatus(row["tag_status"]),
        verify_incomplete=bool(row["verify_incomplete"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class Repository:
    """任务与条目的持久化."""

    def __init__(self, path: Path) -> None:
        """记录数据库路径 (还没连接)."""
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    # -- 生命周期 ------------------------------------------------------------ #

    async def connect(self) -> None:
        """建立连接并迁移 schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await migrate(self._conn)

    async def close(self) -> None:
        """关闭连接."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """已连接的句柄."""
        if self._conn is None:
            raise RuntimeError("Repository 尚未 connect()")
        return self._conn

    # -- 任务 ---------------------------------------------------------------- #

    async def create_task(self, task: TaskRecord) -> None:
        """插入一个任务."""
        await self.conn.execute(
            """
            INSERT INTO tasks (id, name, source_type, source_id, source_ref, save_root, quality_chain,
                               options, status, pause_reason, total_items, est_total_bytes,
                               created_at, updated_at, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.name,
                task.source_type,
                task.source_id,
                task.source_ref,
                task.save_root,
                dumps(task.quality_chain),
                dumps(task.options),
                task.status.value,
                task.pause_reason,
                task.total_items,
                task.est_total_bytes,
                task.created_at,
                task.updated_at,
                task.started_at,
                task.finished_at,
            ),
        )
        await self.conn.commit()

    async def get_task(self, task_id: str) -> TaskRecord | None:
        """按 ID 取任务."""
        async with self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_task(row) if row else None

    async def list_tasks(self, limit: int = 100) -> list[TaskRecord]:
        """按创建时间倒序列出任务."""
        async with self.conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_task(row) for row in rows]

    async def set_task_status(self, task_id: str, status: TaskStatus, reason: str = "") -> None:
        """更新任务状态."""
        now = _now()
        finished = now if status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED) else None
        await self.conn.execute(
            """
            UPDATE tasks
               SET status = ?, pause_reason = ?, updated_at = ?,
                   started_at = COALESCE(started_at, CASE WHEN ? = 'running' THEN ? END),
                   finished_at = COALESCE(?, finished_at)
             WHERE id = ?
            """,
            (status.value, reason, now, status.value, now, finished, task_id),
        )
        await self.conn.commit()

    async def update_task_totals(self, task_id: str, total_items: int, est_total_bytes: int) -> None:
        """更新任务的条目总数与预估总大小."""
        await self.conn.execute(
            "UPDATE tasks SET total_items = ?, est_total_bytes = ?, updated_at = ? WHERE id = ?",
            (total_items, est_total_bytes, _now(), task_id),
        )
        await self.conn.commit()

    async def delete_task(self, task_id: str) -> None:
        """删除任务及其全部条目."""
        await self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self.conn.commit()

    async def unfinished_tasks(self) -> list[TaskRecord]:
        """重启后需要恢复的任务 (未完成、未取消)."""
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE status NOT IN ('completed', 'cancelled') ORDER BY created_at",
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_task(row) for row in rows]

    # -- 条目 ---------------------------------------------------------------- #

    async def add_items(self, task_id: str, entries: list[tuple[SongEntry, str, bool]]) -> int:
        """批量插入条目.

        Args:
            task_id: 任务 ID.
            entries: ``(歌曲快照, 请求档位, 是否勾选)`` 三元组列表.

        Returns:
            实际插入的条数 (``(task_id, songmid, requested_quality)`` 重复的会被忽略).
        """
        now = _now()
        async with self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM task_items WHERE task_id = ?",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        position = (row[0] if row else -1) + 1

        rows = []
        for entry, quality, selected in entries:
            rows.append(
                (
                    task_id,
                    position,
                    entry.songmid,
                    dumps(entry.as_dict()),
                    int(selected),
                    ItemStatus.PENDING.value,
                    quality,
                    now,
                    now,
                ),
            )
            position += 1

        cursor2 = await self.conn.executemany(
            """
            INSERT OR IGNORE INTO task_items
                (task_id, position, songmid, song_json, selected, status, requested_quality, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self.conn.commit()
        return cursor2.rowcount if cursor2.rowcount and cursor2.rowcount > 0 else 0

    async def list_items(
        self,
        task_id: str,
        *,
        statuses: list[ItemStatus] | None = None,
        selected_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ItemRecord]:
        """列出任务条目."""
        sql = "SELECT * FROM task_items WHERE task_id = ?"
        params: list[Any] = [task_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(status.value for status in statuses)
        if selected_only:
            sql += " AND selected = 1"
        sql += " ORDER BY position"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_item(row) for row in rows]

    async def get_item(self, item_id: int) -> ItemRecord | None:
        """按 ID 取条目."""
        async with self.conn.execute("SELECT * FROM task_items WHERE id = ?", (item_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_item(row) if row else None

    async def next_pending_item(self, task_id: str) -> ItemRecord | None:
        """取下一首待下载的歌 (勾选且未进入终态)."""
        async with self.conn.execute(
            """
            SELECT * FROM task_items
             WHERE task_id = ? AND selected = 1 AND status IN ('pending', 'downloading')
             ORDER BY position LIMIT 1
            """,
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_item(row) if row else None

    async def update_item(self, item_id: int, **fields: Any) -> None:
        """更新条目的任意列. 枚举值会自动取 ``.value``."""
        if not fields:
            return
        payload: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, ItemStatus | SubStatus):
                payload[key] = value.value
            elif isinstance(value, bool):
                payload[key] = int(value)
            else:
                payload[key] = value
        payload["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        await self.conn.execute(
            f"UPDATE task_items SET {assignments} WHERE id = ?",
            [*payload.values(), item_id],
        )
        await self.conn.commit()

    async def set_item_selection(self, task_id: str, item_ids: list[int] | None, selected: bool) -> None:
        """设置勾选状态. ``item_ids`` 为 None 表示整个任务."""
        if item_ids is None:
            await self.conn.execute(
                "UPDATE task_items SET selected = ?, updated_at = ? WHERE task_id = ?",
                (int(selected), _now(), task_id),
            )
        elif item_ids:
            placeholders = ",".join("?" * len(item_ids))
            await self.conn.execute(
                f"UPDATE task_items SET selected = ?, updated_at = ? WHERE task_id = ? AND id IN ({placeholders})",
                [int(selected), _now(), task_id, *item_ids],
            )
        await self.conn.commit()

    async def invert_selection(self, task_id: str) -> None:
        """反选."""
        await self.conn.execute(
            "UPDATE task_items SET selected = 1 - selected, updated_at = ? WHERE task_id = ?",
            (_now(), task_id),
        )
        await self.conn.commit()

    async def reset_failed(self, task_id: str) -> int:
        """把失败/试听条目重置为 pending, 用于「只重试失败项」."""
        cursor = await self.conn.execute(
            """
            UPDATE task_items
               SET status = 'pending', error_type = '', error_message = '', api_result_code = NULL,
                   trial_reason = '', bytes_downloaded = 0, updated_at = ?
             WHERE task_id = ? AND status IN ('failed', 'trial', 'unavailable')
            """,
            (_now(), task_id),
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    async def status_counts(self, task_id: str) -> dict[str, int]:
        """统计各状态条数."""
        async with self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM task_items WHERE task_id = ? GROUP BY status",
            (task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        counts = {status.value: 0 for status in ItemStatus}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    # -- 全局去重索引 --------------------------------------------------------- #

    async def find_download(self, songmid: str, quality: str) -> dict[str, Any] | None:
        """查全局去重索引."""
        async with self.conn.execute(
            "SELECT * FROM downloads WHERE songmid = ? AND quality = ?",
            (songmid, quality),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def record_download(
        self,
        songmid: str,
        quality: str,
        path: str,
        file_size: int,
        task_id: str | None = None,
    ) -> None:
        """写入/更新全局去重索引."""
        await self.conn.execute(
            """
            INSERT INTO downloads (songmid, quality, path, file_size, task_id, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(songmid, quality) DO UPDATE SET
                path = excluded.path, file_size = excluded.file_size,
                task_id = excluded.task_id, completed_at = excluded.completed_at
            """,
            (songmid, quality, path, file_size, task_id, _now()),
        )
        await self.conn.commit()

    async def forget_download(self, songmid: str, quality: str) -> None:
        """从去重索引中删除一条 (文件被判为试听/损坏后调用)."""
        await self.conn.execute("DELETE FROM downloads WHERE songmid = ? AND quality = ?", (songmid, quality))
        await self.conn.commit()

    async def all_download_paths(self) -> list[str]:
        """列出去重索引里的全部路径 (孤儿 ``.part`` 清理时用)."""
        async with self.conn.execute("SELECT path FROM downloads") as cursor:
            rows = await cursor.fetchall()
        return [row["path"] for row in rows]

    # -- 事件 ---------------------------------------------------------------- #

    async def append_event(
        self,
        kind: str,
        payload: str,
        *,
        task_id: str | None = None,
        item_id: int | None = None,
        level: str = "info",
    ) -> None:
        """落一条事件, 供迟到的客户端回放."""
        await self.conn.execute(
            "INSERT INTO task_events (task_id, item_id, ts, level, kind, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, item_id, _now(), level, kind, payload),
        )
        await self.conn.commit()

    async def recent_events(self, task_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """取最近的事件."""
        if task_id:
            sql = "SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?"
            params: tuple[Any, ...] = (task_id, limit)
        else:
            sql = "SELECT * FROM task_events ORDER BY id DESC LIMIT ?"
            params = (limit,)
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in reversed(list(rows))]
