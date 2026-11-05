"""SQLite persistence for crawled pages and forum entities."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    url_hash TEXT NOT NULL UNIQUE,
    status_code INTEGER,
    content_type TEXT,
    title TEXT,
    html TEXT,
    text_content TEXT,
    mirror_path TEXT,
    depth INTEGER DEFAULT 0,
    fetched_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_url TEXT NOT NULL,
    to_url TEXT NOT NULL,
    UNIQUE(from_url, to_url)
);

CREATE TABLE IF NOT EXISTS queue (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    enqueued_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS forums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,
    name TEXT,
    url TEXT UNIQUE,
    description TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,
    forum_url TEXT,
    title TEXT,
    url TEXT UNIQUE,
    author TEXT,
    created_at TEXT,
    reply_count INTEGER,
    view_count INTEGER,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,
    thread_url TEXT,
    author TEXT,
    posted_at TEXT,
    content_html TEXT,
    content_text TEXT,
    post_url TEXT,
    raw_json TEXT,
    UNIQUE(thread_url, external_id, post_url)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    profile_url TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS crawler_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pages_fetched ON pages(fetched_at);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_threads_forum ON threads(forum_url);
CREATE INDEX IF NOT EXISTS idx_posts_thread ON posts(thread_url);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._ensure_column("queue", "not_before", "TEXT")
        # Recover URLs left in_progress after a crash / interrupt.
        await self._conn.execute(
            """
            UPDATE queue
            SET status = 'pending', updated_at = ?
            WHERE status = 'in_progress'
            """,
            (utc_now(),),
        )
        await self._conn.commit()

    async def _ensure_column(self, table: str, column: str, decl: str) -> None:
        async with self.conn.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        names = {row["name"] for row in rows}
        if column not in names:
            await self.conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def enqueue(self, url: str, depth: int = 0) -> bool:
        async with self._lock:
            try:
                await self.conn.execute(
                    """
                    INSERT INTO queue(url, depth, status, enqueued_at)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (url, depth, utc_now()),
                )
                await self.conn.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def claim_next(self) -> tuple[str, int] | None:
        # Serialize claims on the shared connection (no nested BEGIN).
        async with self._lock:
            now = utc_now()
            async with self.conn.execute(
                """
                SELECT url, depth FROM queue
                WHERE status = 'pending'
                  AND (not_before IS NULL OR not_before <= ?)
                ORDER BY CASE WHEN not_before IS NULL THEN 0 ELSE 1 END,
                         depth ASC, enqueued_at ASC
                LIMIT 1
                """,
                (now,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None

            url = row["url"]
            depth = int(row["depth"])
            cursor = await self.conn.execute(
                """
                UPDATE queue
                SET status = 'in_progress', updated_at = ?
                WHERE url = ? AND status = 'pending'
                """,
                (utc_now(), url),
            )
            await self.conn.commit()
            if cursor.rowcount != 1:
                return None
            return url, depth

    async def mark_done(self, url: str) -> None:
        await self.conn.execute(
            """
            UPDATE queue
            SET status = 'done', updated_at = ?
            WHERE url = ?
            """,
            (utc_now(), url),
        )
        await self.conn.commit()

    async def mark_failed(self, url: str) -> None:
        await self.conn.execute(
            """
            UPDATE queue
            SET status = 'failed', updated_at = ?
            WHERE url = ?
            """,
            (utc_now(), url),
        )
        await self.conn.commit()

    async def mark_pending(self, url: str) -> None:
        await self.conn.execute(
            """
            UPDATE queue
            SET status = 'pending', not_before = NULL, updated_at = ?
            WHERE url = ?
            """,
            (utc_now(), url),
        )
        await self.conn.commit()

    async def schedule_retry(self, url: str, delay_seconds: float) -> None:
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        await self.conn.execute(
            """
            UPDATE queue
            SET status = 'pending', not_before = ?, updated_at = ?
            WHERE url = ?
            """,
            (when.isoformat(), utc_now(), url),
        )
        await self.conn.commit()

    async def seconds_until_retry(self) -> float | None:
        from datetime import datetime

        now = utc_now()
        async with self.conn.execute(
            """
            SELECT not_before FROM queue
            WHERE status = 'pending'
              AND not_before IS NOT NULL
              AND not_before > ?
            ORDER BY not_before ASC
            LIMIT 1
            """,
            (now,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or not row["not_before"]:
            return None
        target = datetime.fromisoformat(row["not_before"])
        current = datetime.fromisoformat(now)
        return max(0.0, (target - current).total_seconds())

    async def requeue_forbidden(self) -> int:
        """Put previously finished 403 pages back on the queue once."""
        cursor = await self.conn.execute(
            """
            UPDATE queue
            SET status = 'pending', not_before = NULL, updated_at = ?
            WHERE status = 'done'
              AND url IN (SELECT url FROM pages WHERE status_code = 403)
            """,
            (utc_now(),),
        )
        await self.conn.commit()
        return cursor.rowcount

    async def page_status(self, url_hash: str) -> int | None:
        async with self.conn.execute(
            "SELECT status_code FROM pages WHERE url_hash = ? LIMIT 1",
            (url_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["status_code"] is None:
            return None
        return int(row["status_code"])

    async def set_alert(self, message: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO crawler_state(key, value, updated_at)
            VALUES ('alert', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (message, utc_now()),
        )
        await self.conn.commit()

    async def clear_alert(self) -> None:
        await self.conn.execute(
            "DELETE FROM crawler_state WHERE key = 'alert'"
        )
        await self.conn.commit()

    async def page_exists(self, url_hash: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM pages WHERE url_hash = ? LIMIT 1",
            (url_hash,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def save_page(
        self,
        *,
        url: str,
        url_hash: str,
        status_code: int | None,
        content_type: str | None,
        title: str | None,
        html: str | None,
        text_content: str | None,
        mirror_path: str | None,
        depth: int,
        error: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO pages(
                url, url_hash, status_code, content_type, title, html,
                text_content, mirror_path, depth, fetched_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                url_hash=excluded.url_hash,
                status_code=excluded.status_code,
                content_type=excluded.content_type,
                title=excluded.title,
                html=excluded.html,
                text_content=excluded.text_content,
                mirror_path=excluded.mirror_path,
                depth=excluded.depth,
                fetched_at=excluded.fetched_at,
                error=excluded.error
            """,
            (
                url,
                url_hash,
                status_code,
                content_type,
                title,
                html,
                text_content,
                mirror_path,
                depth,
                utc_now(),
                error,
            ),
        )
        await self.conn.commit()

    async def save_link(self, from_url: str, to_url: str) -> None:
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO links(from_url, to_url)
            VALUES (?, ?)
            """,
            (from_url, to_url),
        )
        await self.conn.commit()

    async def upsert_forum(self, forum: dict[str, Any]) -> None:
        await self.conn.execute(
            """
            INSERT INTO forums(external_id, name, url, description, raw_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                external_id=excluded.external_id,
                name=excluded.name,
                description=excluded.description,
                raw_json=excluded.raw_json
            """,
            (
                forum.get("external_id"),
                forum.get("name"),
                forum.get("url"),
                forum.get("description"),
                json.dumps(forum, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    async def upsert_thread(self, thread: dict[str, Any]) -> None:
        await self.conn.execute(
            """
            INSERT INTO threads(
                external_id, forum_url, title, url, author,
                created_at, reply_count, view_count, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                external_id=excluded.external_id,
                forum_url=excluded.forum_url,
                title=excluded.title,
                author=excluded.author,
                created_at=excluded.created_at,
                reply_count=excluded.reply_count,
                view_count=excluded.view_count,
                raw_json=excluded.raw_json
            """,
            (
                thread.get("external_id"),
                thread.get("forum_url"),
                thread.get("title"),
                thread.get("url"),
                thread.get("author"),
                thread.get("created_at"),
                thread.get("reply_count"),
                thread.get("view_count"),
                json.dumps(thread, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    async def upsert_post(self, post: dict[str, Any]) -> None:
        external_id = post.get("external_id") or ""
        post_url = post.get("post_url") or post.get("thread_url") or ""
        await self.conn.execute(
            """
            INSERT INTO posts(
                external_id, thread_url, author, posted_at,
                content_html, content_text, post_url, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_url, external_id, post_url) DO UPDATE SET
                author=excluded.author,
                posted_at=excluded.posted_at,
                content_html=excluded.content_html,
                content_text=excluded.content_text,
                raw_json=excluded.raw_json
            """,
            (
                external_id,
                post.get("thread_url"),
                post.get("author"),
                post.get("posted_at"),
                post.get("content_html"),
                post.get("content_text"),
                post_url,
                json.dumps(post, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    async def upsert_user(self, user: dict[str, Any]) -> None:
        username = user.get("username")
        if not username:
            return
        await self.conn.execute(
            """
            INSERT INTO users(username, profile_url, raw_json)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                profile_url=excluded.profile_url,
                raw_json=excluded.raw_json
            """,
            (
                username,
                user.get("profile_url"),
                json.dumps(user, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    async def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in ("pages", "links", "forums", "threads", "posts", "users"):
            async with self.conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}"
            ) as cursor:
                row = await cursor.fetchone()
                counts[table] = int(row["c"] if row else 0)

        async with self.conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM queue
            GROUP BY status
            """
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            counts[f"queue_{row['status']}"] = int(row["c"])
        return counts
