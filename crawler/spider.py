"""Async crawl loop across one or more registered sites."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .auth_wall import AuthWall, detect_auth_wall, forbidden_session_wall
from .config import AppConfig
from .db import Database
from .mirror import MirrorWriter
from .parser import ForumParser, normalize_url
from .sites import SiteRecord, load_registry, migrate_legacy_layout, site_paths
from .tor_client import TorClient

logger = logging.getLogger(__name__)

REGISTRY_REFRESH_SECONDS = 5.0


def auth_wall_message(title: str | None, html: str) -> str | None:
    wall = detect_auth_wall(title, html)
    return wall.message if wall else None


@dataclass
class SiteRuntime:
    record: SiteRecord
    config: AppConfig
    db: Database
    client: TorClient
    mirror: MirrorWriter | None
    member_cookie: bool = False
    paused: bool = False
    pause_reason: str = ""
    forbidden_streak: int = 0
    known_urls: set[str] = field(default_factory=set)
    queued_assets: set[str] = field(default_factory=set)

    @property
    def site_id(self) -> str:
        return self.record.id

    @property
    def seed_host(self) -> str:
        return self.record.host


class Spider:
    """Multi-site crawler with shared Tor concurrency and live registry reload."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.parser = ForumParser()
        self._skip_re = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in config.crawl.skip_patterns
        ]
        self._fetched = 0
        self._fetched_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._sites: dict[str, SiteRuntime] = {}
        self._claim_order: list[str] = []
        self._claim_index = 0
        self._sites_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request_at = [0.0]
        self._rr_lock = asyncio.Lock()
        self._asset_queue: asyncio.Queue[tuple[SiteRuntime, str] | None] = (
            asyncio.Queue()
        )
        self._assets_saved = 0

    async def run(self) -> dict[str, int]:
        migrate_legacy_layout(self.config.project_root, self.config.seed_url)
        await self._sync_sites_from_registry()
        if not self._sites:
            logger.warning(
                "No sites registered. Add one on the Sites page before crawling."
            )
            return {"sites": 0}

        # One Tor IP check using any site client.
        first = next(iter(self._sites.values()))
        await first.client.check_exit_ip()

        # Queue missing CSS/images for pages already in the DB (side channel).
        if self.config.storage.enable_mirror_assets:
            for runtime in list(self._sites.values()):
                await self._seed_missing_mirror_assets(runtime)

        workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self.config.crawl.concurrency)
        ]
        asset_workers = 1 if self.config.storage.enable_mirror_assets else 0
        assets = [
            asyncio.create_task(self._asset_worker(i))
            for i in range(asset_workers)
        ]
        refresher = asyncio.create_task(self._registry_refresher())
        try:
            await asyncio.gather(*workers)
            # Drain mirror assets that were queued while pages were crawling.
            if assets:
                await self._asset_queue.join()
        finally:
            self._stop.set()
            for _ in assets:
                await self._asset_queue.put(None)
            if assets:
                await asyncio.gather(*assets, return_exceptions=True)
            refresher.cancel()
            try:
                await refresher
            except asyncio.CancelledError:
                pass
            await self._close_all_sites()

        stats = await self._aggregate_stats()
        stats["mirror_assets"] = self._assets_saved
        return stats

    async def _registry_refresher(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=REGISTRY_REFRESH_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._sync_sites_from_registry()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to refresh sites registry")

    async def _sync_sites_from_registry(self) -> None:
        records = {
            site.id: site
            for site in load_registry(self.config.project_root)
            if site.enabled
        }
        async with self._sites_lock:
            # Drop disabled / deleted.
            for site_id in list(self._sites.keys()):
                if site_id not in records:
                    runtime = self._sites.pop(site_id)
                    await self._close_runtime(runtime)
                    logger.info("Site removed from crawl set: %s", site_id)
            # Add newly enabled.
            for site_id, record in records.items():
                if site_id in self._sites:
                    self._sites[site_id].record = record
                    continue
                runtime = await self._open_site(record)
                self._sites[site_id] = runtime
                logger.info("Site added to crawl set: %s", record.host)
            self._claim_order = sorted(self._sites.keys())

    async def _open_site(self, record: SiteRecord) -> SiteRuntime:
        paths = site_paths(self.config.project_root, record.id)
        paths.ensure()
        site_config = self.config.with_site(
            seed_url=record.seed_url,
            database=paths.database,
            mirror_dir=paths.mirror,
            cookies_file=paths.cookies,
        )
        db = Database(paths.database)
        await db.connect()
        seed = normalize_url(record.seed_url, record.seed_url)
        if seed:
            await db.enqueue(seed, depth=0)
        requeued = await db.requeue_forbidden()
        if requeued:
            logger.info(
                "[%s] Requeued %s previously forbidden pages",
                record.id,
                requeued,
            )
        # Do not clear alerts on open — Auth UI needs them until fixed.
        client = TorClient(
            site_config,
            request_lock=self._request_lock,
            last_request_at=self._last_request_at,
        )
        await client.open()
        mirror = (
            MirrorWriter(paths.mirror, record.host)
            if site_config.storage.enable_mirror
            else None
        )
        return SiteRuntime(
            record=record,
            config=site_config,
            db=db,
            client=client,
            mirror=mirror,
            member_cookie=_has_member_cookie(paths.cookies),
        )

    async def _close_runtime(self, runtime: SiteRuntime) -> None:
        try:
            await runtime.client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await runtime.db.close()
        except Exception:  # noqa: BLE001
            pass

    async def _close_all_sites(self) -> None:
        async with self._sites_lock:
            for runtime in list(self._sites.values()):
                await self._close_runtime(runtime)
            self._sites.clear()
            self._claim_order.clear()

    async def _aggregate_stats(self) -> dict[str, int]:
        totals: dict[str, int] = {"sites": 0}
        async with self._sites_lock:
            runtimes = list(self._sites.values())
        # Sites may already be closed; read from registry paths.
        for site in load_registry(self.config.project_root):
            if not site.enabled:
                continue
            paths = site_paths(self.config.project_root, site.id)
            if not paths.database.exists():
                continue
            db = Database(paths.database)
            await db.connect()
            try:
                stats = await db.stats()
            finally:
                await db.close()
            totals["sites"] += 1
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + int(value)
        if not totals["sites"] and runtimes:
            totals["sites"] = len(runtimes)
        return totals

    def should_visit(self, runtime: SiteRuntime, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if runtime.config.crawl.same_host_only:
            if parsed.netloc.lower() != runtime.seed_host:
                return False
        for pattern in self._skip_re:
            if pattern.search(url):
                return False
        return True

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    async def _budget_available(self) -> bool:
        if self.config.crawl.max_pages <= 0:
            return True
        async with self._fetched_lock:
            return self._fetched < self.config.crawl.max_pages

    async def _increment_fetched(self) -> bool:
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

    async def _claim_next_fair(self) -> tuple[SiteRuntime, str, int] | None:
        async with self._rr_lock:
            async with self._sites_lock:
                order = list(self._claim_order)
                sites = dict(self._sites)
            if not order:
                return None
            start = self._claim_index % len(order)
            for offset in range(len(order)):
                idx = (start + offset) % len(order)
                site_id = order[idx]
                runtime = sites.get(site_id)
                if runtime is None or runtime.paused:
                    continue
                claimed = await runtime.db.claim_next()
                if claimed is None:
                    continue
                self._claim_index = idx + 1
                url, depth = claimed
                return runtime, url, depth
            self._claim_index = start + 1
            return None

    async def _seconds_until_any_retry(self) -> float | None:
        waits: list[float] = []
        async with self._sites_lock:
            runtimes = [r for r in self._sites.values() if not r.paused]
        for runtime in runtimes:
            wait = await runtime.db.seconds_until_retry()
            if wait is not None:
                waits.append(wait)
        if not waits:
            return None
        return min(waits)

    async def _active_site_count(self) -> int:
        async with self._sites_lock:
            return sum(1 for runtime in self._sites.values() if not runtime.paused)

    async def _worker(self, worker_id: int) -> None:
        idle_rounds = 0
        while not self._stop.is_set():
            if not await self._budget_available():
                self._stop.set()
                break

            if await self._active_site_count() == 0:
                idle_rounds += 1
                if idle_rounds >= 3:
                    break
                await asyncio.sleep(1.0)
                continue

            claimed = await self._claim_next_fair()
            if claimed is None:
                wait = await self._seconds_until_any_retry()
                if wait is not None and not self._stop.is_set():
                    idle_rounds = 0
                    await asyncio.sleep(min(wait, 20.0))
                    continue
                idle_rounds += 1
                if idle_rounds >= 5:
                    break
                await asyncio.sleep(1.0)
                continue

            idle_rounds = 0
            runtime, url, depth = claimed
            try:
                processed = await self._process(runtime, url, depth)
                if processed:
                    await runtime.db.mark_done(url)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[%s] Failed %s: %s", runtime.site_id, url, exc)
                await runtime.db.save_page(
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
                await runtime.db.mark_failed(url)

    async def _process(self, runtime: SiteRuntime, url: str, depth: int) -> bool:
        if not self.should_visit(runtime, url):
            logger.debug("[%s] Skip URL policy: %s", runtime.site_id, url)
            return True

        digest = self.url_hash(url)
        previous_status = await runtime.db.page_status(digest)
        if previous_status is not None and previous_status < 400:
            logger.debug("[%s] Already fetched: %s", runtime.site_id, url)
            # Backfill missing mirror CSS/images without re-fetching the page.
            await self._backfill_mirror_assets(runtime, url)
            return True

        if not await self._increment_fetched():
            self._stop.set()
            await runtime.db.mark_pending(url)
            return False

        logger.info("[%s depth=%s] GET %s", runtime.site_id, depth, url)
        response = await runtime.client.get(url)

        content_type = response.headers.get("content-type", "")
        allowed = runtime.config.crawl.allowed_content_types
        if allowed and not any(ct in content_type for ct in allowed):
            await runtime.db.save_page(
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
                "Forum returned 429 Too Many Requests. This site is paused. "
                "Wait and use a slower request delay before enabling it again."
            )
            logger.error("[%s] %s (%s)", runtime.site_id, message, url)
            await runtime.db.save_page(
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
            await runtime.db.set_alert(message, kind="rate_limit")
            await runtime.db.mark_pending(url)
            await self._release_budget()
            runtime.paused = True
            runtime.pause_reason = message
            return False

        wall = detect_auth_wall(None, html)
        if wall is None and previous_status == 200 and response.status_code == 403:
            wall = forbidden_session_wall("a page that was readable is now forbidden")
        if wall is not None:
            await self._pause_on_wall(
                runtime, url, digest, depth, response.status_code, content_type, html, wall
            )
            return False

        if response.status_code == 403:
            if runtime.member_cookie and "you do not have permission" in html.lower():
                runtime.forbidden_streak += 1
                if runtime.forbidden_streak >= 5:
                    wall = forbidden_session_wall(
                        "logged-in pages are forbidden again"
                    )
                    await self._pause_on_wall(
                        runtime,
                        url,
                        digest,
                        depth,
                        response.status_code,
                        content_type,
                        html,
                        wall,
                    )
                    return False
            else:
                runtime.forbidden_streak = 0
            logger.info(
                "[%s] 403 %s; retry in %ss",
                runtime.site_id,
                url,
                runtime.config.crawl.retry_403_seconds,
            )
            parsed_title = None
            try:
                parsed_title = self.parser.parse(url, html, structured=False).title
            except Exception:  # noqa: BLE001
                parsed_title = None
            await runtime.db.save_page(
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
            await runtime.db.schedule_retry(
                url, runtime.config.crawl.retry_403_seconds
            )
            await self._release_budget()
            return False

        runtime.forbidden_streak = 0
        parsed = self.parser.parse(
            url,
            html,
            structured=runtime.config.storage.enable_structured_parse,
        )

        mirror_path = None
        if runtime.mirror is not None and response.status_code < 400:
            mirror_path, assets = runtime.mirror.save(
                url, html, runtime.known_urls
            )
            runtime.known_urls.add(url)
            await self._queue_mirror_assets(runtime, assets)

        await runtime.db.save_page(
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
            runtime,
            parsed.forums,
            parsed.threads,
            parsed.posts,
            parsed.users,
        )
        await self._enqueue_links(runtime, url, parsed.links, depth)
        return True

    async def _pause_on_wall(
        self,
        runtime: SiteRuntime,
        url: str,
        digest: str,
        depth: int,
        status_code: int,
        content_type: str,
        html: str,
        wall: AuthWall,
    ) -> None:
        logger.error("[%s] %s (%s)", runtime.site_id, wall.message, url)
        await runtime.db.save_page(
            url=url,
            url_hash=digest,
            status_code=status_code,
            content_type=content_type,
            title=None,
            html=html,
            text_content=None,
            mirror_path=None,
            depth=depth,
            error=wall.message,
        )
        await runtime.db.set_alert(wall.message, kind=wall.kind)
        await runtime.db.mark_pending(url)
        await self._release_budget()
        runtime.paused = True
        runtime.pause_reason = wall.message

    async def _store_entities(
        self,
        runtime: SiteRuntime,
        forums: Iterable[dict],
        threads: Iterable[dict],
        posts: Iterable[dict],
        users: Iterable[dict],
    ) -> None:
        for forum in forums:
            await runtime.db.upsert_forum(forum)
        for thread in threads:
            await runtime.db.upsert_thread(thread)
        for post in posts:
            await runtime.db.upsert_post(post)
        for user in users:
            await runtime.db.upsert_user(user)

    async def _enqueue_links(
        self,
        runtime: SiteRuntime,
        from_url: str,
        links: list[str],
        depth: int,
    ) -> None:
        for link in links:
            await runtime.db.save_link(from_url, link)
            if not self.should_visit(runtime, link):
                continue
            await runtime.db.enqueue(link, depth=depth + 1)

    async def _backfill_mirror_assets(
        self,
        runtime: SiteRuntime,
        url: str,
    ) -> None:
        if (
            not self.config.storage.enable_mirror_assets
            or runtime.mirror is None
        ):
            return
        html = await runtime.db.get_page_html(url)
        if not html:
            return
        assets = runtime.mirror.extract_page_assets(url, html)
        await self._queue_mirror_assets(runtime, assets)

    async def _seed_missing_mirror_assets(self, runtime: SiteRuntime) -> None:
        if runtime.mirror is None:
            return
        pages = await runtime.db.list_page_htmls(limit=200)
        queued = 0
        for url, html in pages:
            assets = runtime.mirror.extract_page_assets(url, html)
            before = len(runtime.queued_assets)
            await self._queue_mirror_assets(runtime, assets)
            queued += max(0, len(runtime.queued_assets) - before)
        if queued:
            logger.info(
                "[%s] Queued %s missing mirror assets from saved pages",
                runtime.site_id,
                queued,
            )

    async def _queue_mirror_assets(
        self,
        runtime: SiteRuntime,
        assets: list[str],
    ) -> None:
        if (
            not self.config.storage.enable_mirror_assets
            or runtime.mirror is None
            or not assets
        ):
            return
        for asset_url in assets:
            if asset_url in runtime.queued_assets:
                continue
            if runtime.mirror.asset_exists(asset_url):
                runtime.queued_assets.add(asset_url)
                continue
            runtime.queued_assets.add(asset_url)
            await self._asset_queue.put((runtime, asset_url))

    async def _asset_worker(self, worker_id: int) -> None:
        while True:
            item = await self._asset_queue.get()
            try:
                if item is None:
                    return
                runtime, asset_url = item
                # Still fetch assets if the page crawl is paused (e.g. login wall).
                if runtime.mirror is None:
                    continue
                if runtime.mirror.asset_exists(asset_url):
                    continue
                logger.info("[%s asset] GET %s", runtime.site_id, asset_url)
                try:
                    response = await runtime.client.get(asset_url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[%s asset] failed %s: %s",
                        runtime.site_id,
                        asset_url,
                        exc,
                    )
                    continue
                if response.status_code >= 400:
                    logger.warning(
                        "[%s asset] HTTP %s for %s",
                        runtime.site_id,
                        response.status_code,
                        asset_url,
                    )
                    continue
                data = response.content
                if not data:
                    continue
                runtime.mirror.save_asset(asset_url, data)
                self._assets_saved += 1
                content_type = response.headers.get("content-type", "")
                if "css" in content_type or asset_url.lower().endswith(".css"):
                    try:
                        nested = runtime.mirror.extract_css_assets(
                            asset_url,
                            data.decode("utf-8", errors="ignore"),
                        )
                        await self._queue_mirror_assets(runtime, nested)
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "[%s asset] could not parse css urls from %s",
                            runtime.site_id,
                            asset_url,
                        )
            finally:
                self._asset_queue.task_done()


def _has_member_cookie(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    return "mybbuser" in text
