"""Load and normalize crawler configuration."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    return float(value)


# Tor treats this pair as a circuit-isolation key, not as a login.
# The same pair on every request keeps the crawl on one circuit.
SOCKS_ISOLATION_USER = "dark-crawler"
SOCKS_ISOLATION_PASSWORD = "pinned"


@dataclass
class TorConfig:
    socks_host: str = "127.0.0.1"
    socks_port: int = 9050
    use_tor_browser_port: bool = False

    @property
    def port(self) -> int:
        if self.use_tor_browser_port:
            return 9150
        return self.socks_port

    @property
    def proxy_url(self) -> str:
        user = quote(SOCKS_ISOLATION_USER, safe="")
        password = quote(SOCKS_ISOLATION_PASSWORD, safe="")
        return f"socks5h://{user}:{password}@{self.socks_host}:{self.port}"


def _delay_bounds(crawl_raw: dict[str, Any]) -> tuple[float, float]:
    """Random pause between HTTP requests. Min/max override a single delay."""
    fixed = _env_float(
        "CRAWL_REQUEST_DELAY",
        float(crawl_raw.get("request_delay", 20.0)),
    )
    low = _env_float(
        "CRAWL_DELAY_MIN",
        float(crawl_raw.get("request_delay_min", fixed)),
    )
    high = _env_float(
        "CRAWL_DELAY_MAX",
        float(crawl_raw.get("request_delay_max", fixed)),
    )
    if low > high:
        low, high = high, low
    return max(0.0, low), max(0.0, high)


@dataclass
class CrawlConfig:
    max_pages: int = 500
    concurrency: int = 2
    request_delay: float = 20.0
    request_delay_min: float = 20.0
    request_delay_max: float = 40.0
    timeout: float = 60.0
    same_host_only: bool = True
    allowed_content_types: list[str] = field(
        default_factory=lambda: ["text/html", "application/xhtml+xml"]
    )
    skip_patterns: list[str] = field(default_factory=list)
    user_agent: str = "DarkCrawler/1.0"
    cookies_file: str = ""
    retry_403_seconds: float = 1800.0

    def next_delay(self) -> float:
        low = min(self.request_delay_min, self.request_delay_max)
        high = max(self.request_delay_min, self.request_delay_max)
        if high <= 0:
            return 0.0
        if high == low:
            return low
        return random.uniform(low, high)


@dataclass
class StorageConfig:
    database: str = "data/forum.db"
    mirror_dir: str = "data/mirror"
    enable_mirror: bool = True
    # Fetch CSS/images/fonts for the HTML mirror in a side queue (not page queue).
    enable_mirror_assets: bool = True
    enable_structured_parse: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "data/crawler.log"


@dataclass
class AppConfig:
    seed_url: str
    tor: TorConfig
    crawl: CrawlConfig
    storage: StorageConfig
    logging: LoggingConfig
    project_root: Path
    config_path: Path | None = None

    @property
    def seed_host(self) -> str:
        return urlparse(self.seed_url).netloc.lower()

    @property
    def database_path(self) -> Path:
        path = Path(self.storage.database)
        if not path.is_absolute():
            path = self.project_root / path
        return path

    @property
    def mirror_path(self) -> Path:
        path = Path(self.storage.mirror_dir)
        if not path.is_absolute():
            path = self.project_root / path
        return path

    @property
    def log_path(self) -> Path:
        path = Path(self.logging.file)
        if not path.is_absolute():
            path = self.project_root / path
        return path

    def with_site(
        self,
        *,
        seed_url: str,
        database: str | Path,
        mirror_dir: str | Path,
        cookies_file: str | Path,
    ) -> "AppConfig":
        """Return a shallow copy scoped to one site's paths."""
        crawl = CrawlConfig(
            max_pages=self.crawl.max_pages,
            concurrency=self.crawl.concurrency,
            request_delay=self.crawl.request_delay,
            request_delay_min=self.crawl.request_delay_min,
            request_delay_max=self.crawl.request_delay_max,
            timeout=self.crawl.timeout,
            same_host_only=self.crawl.same_host_only,
            allowed_content_types=list(self.crawl.allowed_content_types),
            skip_patterns=list(self.crawl.skip_patterns),
            user_agent=self.crawl.user_agent,
            cookies_file=str(cookies_file),
            retry_403_seconds=self.crawl.retry_403_seconds,
        )
        storage = StorageConfig(
            database=str(database),
            mirror_dir=str(mirror_dir),
            enable_mirror=self.storage.enable_mirror,
            enable_mirror_assets=self.storage.enable_mirror_assets,
            enable_structured_parse=self.storage.enable_structured_parse,
        )
        return AppConfig(
            seed_url=seed_url,
            tor=self.tor,
            crawl=crawl,
            storage=storage,
            logging=self.logging,
            project_root=self.project_root,
            config_path=self.config_path,
        )


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Config section '{key}' must be a mapping")
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    # Optional legacy seed used only to migrate into data/sites.json.
    seed_url = (raw.get("seed_url") or "").strip()
    seed_url = _env("SEED_URL", seed_url) or seed_url

    tor_raw = _section(raw, "tor")
    crawl_raw = _section(raw, "crawl")
    storage_raw = _section(raw, "storage")
    logging_raw = _section(raw, "logging")

    # Docker / runtime overrides (compose sets TOR_SOCKS_HOST=tor).
    socks_host = _env(
        "TOR_SOCKS_HOST",
        str(tor_raw.get("socks_host", "127.0.0.1")),
    ) or "127.0.0.1"
    socks_port = _env_int(
        "TOR_SOCKS_PORT",
        int(tor_raw.get("socks_port", 9050)),
    )
    max_pages = _env_int(
        "CRAWL_MAX_PAGES",
        int(crawl_raw.get("max_pages", 500)),
    )
    delay_min, delay_max = _delay_bounds(crawl_raw)

    return AppConfig(
        seed_url=seed_url,
        tor=TorConfig(
            socks_host=socks_host,
            socks_port=socks_port,
            use_tor_browser_port=_env_bool(
                "TOR_USE_BROWSER_PORT",
                bool(tor_raw.get("use_tor_browser_port", False)),
            ),
        ),
        crawl=CrawlConfig(
            max_pages=max_pages,
            concurrency=max(
                1,
                _env_int(
                    "CRAWL_CONCURRENCY",
                    int(crawl_raw.get("concurrency", 2)),
                ),
            ),
            request_delay=delay_min,
            request_delay_min=delay_min,
            request_delay_max=delay_max,
            timeout=_env_float(
                "CRAWL_TIMEOUT",
                float(crawl_raw.get("timeout", 60.0)),
            ),
            same_host_only=bool(crawl_raw.get("same_host_only", True)),
            allowed_content_types=list(
                crawl_raw.get(
                    "allowed_content_types",
                    ["text/html", "application/xhtml+xml"],
                )
            ),
            skip_patterns=list(crawl_raw.get("skip_patterns") or []),
            user_agent=str(
                crawl_raw.get("user_agent", "DarkCrawler/1.0")
            ),
            cookies_file=str(crawl_raw.get("cookies_file") or ""),
            retry_403_seconds=_env_float(
                "CRAWL_RETRY_403_SECONDS",
                float(crawl_raw.get("retry_403_seconds", 1800)),
            ),
        ),
        storage=StorageConfig(
            database=str(storage_raw.get("database", "data/forum.db")),
            mirror_dir=str(storage_raw.get("mirror_dir", "data/mirror")),
            enable_mirror=bool(storage_raw.get("enable_mirror", True)),
            enable_mirror_assets=bool(
                storage_raw.get("enable_mirror_assets", True)
            ),
            enable_structured_parse=bool(
                storage_raw.get("enable_structured_parse", True)
            ),
        ),
        logging=LoggingConfig(
            level=_env("LOG_LEVEL", str(logging_raw.get("level", "INFO")))
            or "INFO",
            file=str(logging_raw.get("file", "data/crawler.log")),
        ),
        project_root=config_path.parent,
        config_path=config_path,
    )
