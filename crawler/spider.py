"""Async crawl loop: queue -> fetch -> parse -> store -> mirror."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .config import AppConfig
from .db import Database
from .mirror import MirrorWriter
from .parser import ForumParser, normalize_url
from .tor_client import TorClient

logger = logging.getLogger(__name__)


def auth_wall_message(title: str | None, html: str) -> str | None:
    """Return an alert if this response means the saved cookie is no longer valid."""
    blob = f"{title or ''}\n{html}".lower()
    if (
        "verify humanity" in blob
        or "altcha-widget" in blob
        or "/_pf_verify/verify" in blob
    ):
        return (
            "Cookie expired: the forum is showing the human-verification wall again. "
            "Crawl stopped. Paste a fresh cookie to continue."
        )
    return None


class Spider:
    def __init__(self, config: AppConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.parser = ForumParser()
        self.mirror = (
            MirrorWriter(config.mirror_path, config.seed_host)
            if config.storage.enable_mirror
            else None
        )
        self._skip_re = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in config.crawl.skip_patterns
        ]
        self._fetched = 0
        self._fetched_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._known_urls: set[str] = set()
        self._forbidden_streak = 0
        self._member_cookie = self._has_member_cookie()

    def _has_member_cookie(self) -> bool:
        raw = (self.config.crawl.cookies_file or "").strip()
        if not raw:
            return False
        path = Path(raw)
        if not path.is_absolute():
            path = self.config.project_root / path
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        return "mybbuser" in text

    async def seed(self) -> None:
        seed = normalize_url(self.config.seed_url, self.config.seed_url)
        if not seed:
            raise ValueError(f"Invalid seed_url: {self.config.seed_url}")
        created = await self.db.enqueue(seed, depth=0)
        if created:
            logger.info("Seeded queue with %s", seed)
        else:
            logger.info("Seed already present in queue")

    def should_visit(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if self.config.crawl.same_host_only:
            if parsed.netloc.lower() != self.config.seed_host:
                return False
        for pattern in self._skip_re:
            if pattern.search(url):
                return False
        return True

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    async def run(self) -> dict[str, int]:
        await self.seed()
        requeued = await self.db.requeue_forbidden()
        if requeued:
            logger.info("Requeued %s pages that previously returned 403", requeued)
        await self.db.clear_alert()
        async with TorClient(self.config) as client:
            await client.check_exit_ip()
            workers = [
                asyncio.create_task(self._worker(client, i))
                for i in range(self.config.crawl.concurrency)
            ]
            await asyncio.gather(*workers)
        return await self.db.stats()

    async def _budget_available(self) -> bool:
        if self.config.crawl.max_pages <= 0:
            return True
        async with self._fetched_lock:
            return self._fetched < self.config.crawl.max_pages

    async def _increment_fetched(self) -> bool:
        """Reserve one page slot. Returns False if max_pages reached."""
        if self.config.crawl.max_pages <= 0:
            async with self._fetched_lock:
                self._fetched += 1
            return True
        async with self._fetched_lock:
            if self._fetched >= self.config.crawl.max_pages:
                return False
            self._fetched += 1
            return True

    async def _release_budget(self) -> None:
        async with self._fetched_lock:
            if self._fetched > 0:
                self._fetched -= 1

    async def _worker(self, client: TorClient, worker_id: int) -> None:
        idle_rounds = 0
        while not self._stop.is_set():
            if not await self._budget_available():
                self._stop.set()
                break

            claimed = await self.db.claim_next()
            if claimed is None:
                wait = await self.db.seconds_until_retry()
                if wait is not None and not self._stop.is_set():
                    idle_rounds = 0
                    await asyncio.sleep(min(wait, 20.0))
                    continue
                idle_rounds += 1
                if idle_rounds >= 3:
                    break
                await asyncio.sleep(1.0)
                continue

            idle_rounds = 0
            url, depth = claimed
            try:
                processed = await self._process(client, url, depth)
                if processed:
                    await self.db.mark_done(url)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed %s: %s", url, exc)
                await self.db.save_page(
                    url=url,
                    url_hash=self.url_hash(url),
                    status_code=None,
                    content_type=None,
                    title=None,
                    html=None,
                    text_content=None,
                    mirror_path=None,
                    depth=depth,
                    error=str(exc),
                )
                await self.db.mark_failed(url)

    async def _process(self, client: TorClient, url: str, depth: int) -> bool:
        """Fetch and store one URL. Returns False if deferred (budget)."""
        if not self.should_visit(url):
            logger.debug("Skip URL policy: %s", url)
            return True

        digest = self.url_hash(url)
        previous_status = await self.db.page_status(digest)
        if previous_status is not None and previous_status < 400:
            logger.debug("Already fetched: %s", url)
            return True

        if not await self._increment_fetched():
            self._stop.set()
            # Put back for the next run once max_pages budget resets.
            await self.db.mark_pending(url)
            return False

        logger.info("[depth=%s] GET %s", depth, url)
        response = await client.get(url)

        content_type = response.headers.get("content-type", "")
        allowed = self.config.crawl.allowed_content_types
        if allowed and not any(ct in content_type for ct in allowed):
            await self.db.save_page(
                url=url,
                url_hash=digest,
                status_code=response.status_code,
                content_type=content_type,
                title=None,
                html=None,
                text_content=None,
                mirror_path=None,
                depth=depth,
                error=f"skipped content-type: {content_type}",
            )
            return True

        html = response.text
        if response.status_code == 429:
            message = (
                "Forum returned 429 Too Many Requests. Crawl stopped. "
                "Wait and use a slower request delay before starting again."
            )
            logger.error("%s (%s)", message, url)
            await self.db.save_page(
                url=url,
                url_hash=digest,
                status_code=429,
                content_type=content_type,
                title=None,
                html=html,
                text_content=None,
                mirror_path=None,
                depth=depth,
                error=message,
            )
            await self.db.set_alert(message)
            await self.db.mark_pending(url)
            await self._release_budget()
            self._stop.set()
            return False

        wall = auth_wall_message(None, html)
        if wall or (previous_status == 200 and response.status_code == 403):
            if not wall:
                wall = (
                    "Cookie expired: a page that was readable is now forbidden. "
                    "Crawl stopped. Paste a fresh cookie to continue."
                )
            logger.error("%s (%s)", wall, url)
            await self.db.save_page(
                url=url,
                url_hash=digest,
                status_code=response.status_code,
                content_type=content_type,
                title=None,
                html=html,
                text_content=None,
                mirror_path=None,
                depth=depth,
                error=wall,
            )
            await self.db.set_alert(wall)
            await self.db.mark_pending(url)
            await self._release_budget()
            self._stop.set()
            return False

        if response.status_code == 403:
            if self._member_cookie and "you do not have permission" in html.lower():
                self._forbidden_streak += 1
                if self._forbidden_streak >= 5:
                    message = (
                        "Cookie expired: logged-in pages are forbidden again. "
                        "Crawl stopped. Paste a fresh cookie to continue."
                    )
                    logger.error("%s (%s)", message, url)
                    await self.db.set_alert(message)
                    await self.db.mark_pending(url)
                    await self._release_budget()
                    self._stop.set()
                    return False
            else:
                self._forbidden_streak = 0
            logger.info("403 %s; retry in %ss", url, self.config.crawl.retry_403_seconds)
            parsed_title = None
            try:
                parsed_title = self.parser.parse(url, html, structured=False).title
            except Exception:  # noqa: BLE001
                parsed_title = None
            await self.db.save_page(
                url=url,
                url_hash=digest,
                status_code=403,
                content_type=content_type,
                title=parsed_title,
                html=html,
                text_content=None,
                mirror_path=None,
                depth=depth,
                error="forbidden; scheduled for retry",
            )
            await self.db.schedule_retry(url, self.config.crawl.retry_403_seconds)
            await self._release_budget()
            return False

        self._forbidden_streak = 0
        parsed = self.parser.parse(
            url,
            html,
            structured=self.config.storage.enable_structured_parse,
        )

        mirror_path = None
        if self.mirror is not None and response.status_code < 400:
            mirror_path = self.mirror.save(url, html, self._known_urls)
            self._known_urls.add(url)

        await self.db.save_page(
            url=url,
            url_hash=digest,
            status_code=response.status_code,
            content_type=content_type,
            title=parsed.title,
            html=html,
            text_content=parsed.text_content,
            mirror_path=mirror_path,
            depth=depth,
        )

        await self._store_entities(
            parsed.forums, parsed.threads, parsed.posts, parsed.users
        )
        await self._enqueue_links(url, parsed.links, depth)
        return True

    async def _store_entities(
        self,
        forums: Iterable[dict],
        threads: Iterable[dict],
        posts: Iterable[dict],
        users: Iterable[dict],
    ) -> None:
        for forum in forums:
            await self.db.upsert_forum(forum)
        for thread in threads:
            await self.db.upsert_thread(thread)
        for post in posts:
            await self.db.upsert_post(post)
        for user in users:
            await self.db.upsert_user(user)

    async def _enqueue_links(
        self, from_url: str, links: list[str], depth: int
    ) -> None:
        for link in links:
            await self.db.save_link(from_url, link)
            if not self.should_visit(link):
                continue
            await self.db.enqueue(link, depth=depth + 1)
