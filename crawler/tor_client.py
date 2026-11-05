"""HTTP client that routes all traffic through Tor SOCKS5."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import AppConfig

logger = logging.getLogger(__name__)


class TorNotAvailableError(RuntimeError):
    """Raised when the local Tor SOCKS port is closed."""


def check_tor_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class TorClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def __aenter__(self) -> "TorClient":
        await self.open()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def open(self) -> None:
        tor = self.config.tor
        if not check_tor_port(tor.socks_host, tor.port):
            raise TorNotAvailableError(
                f"Tor SOCKS proxy not reachable at {tor.socks_host}:{tor.port}. "
                "Start system Tor (`sudo systemctl start tor`) or set "
                "tor.use_tor_browser_port: true while Tor Browser is open."
            )

        cookies = self._load_cookies()
        self._client = httpx.AsyncClient(
            proxy=tor.proxy_url,
            timeout=httpx.Timeout(self.config.crawl.timeout),
            headers={"User-Agent": self.config.crawl.user_agent},
            cookies=cookies,
            follow_redirects=False,
            http2=False,
        )
        logger.info(
            "Tor client ready via %s; delay %.0f-%.0fs",
            tor.proxy_url,
            self.config.crawl.request_delay_min,
            self.config.crawl.request_delay_max,
        )

    def _load_cookies(self) -> httpx.Cookies:
        cookies = httpx.Cookies()
        raw_path = (self.config.crawl.cookies_file or "").strip()
        if not raw_path:
            return cookies

        path = Path(raw_path)
        if not path.is_absolute():
            path = self.config.project_root / path
        if not path.exists():
            logger.warning("cookies_file not found: %s", path)
            return cookies

        # Minimal Netscape cookies.txt reader.
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, cookie_path, _secure, _expires, name, value = parts[:7]
            cookies.set(name, value, domain=domain.lstrip("."), path=cookie_path)
        logger.info("Loaded cookies from %s", path)
        return cookies

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, url: str) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("TorClient is not open")

        # Follow redirects one hop at a time so the delay applies to each request.
        response = await self._send(url)
        for _ in range(4):
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            url = urljoin(str(response.url), location)
            response = await self._send(url)
        return response

    async def _send(self, url: str) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("TorClient is not open")
        delay = self.config.crawl.next_delay()
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait_for = self._last_request_at + delay - now
            if wait_for > 0:
                logger.info("Waiting %.1fs before next request", wait_for)
                await asyncio.sleep(wait_for)
            response = await self._client.get(url)
            self._last_request_at = loop.time()
            return response

    async def check_exit_ip(self) -> str | None:
        """Optional sanity check that traffic exits via Tor."""
        try:
            response = await self.get("https://check.torproject.org/api/ip")
            data = response.json()
            if data.get("IsTor"):
                logger.info("Confirmed Tor exit IP: %s", data.get("IP"))
            else:
                logger.warning("Traffic may not be using Tor: %s", data)
            return str(data.get("IP") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tor IP check failed: %s", exc)
            return None
