"""Multi-site registry: hashed per-host data dirs + live enable/disable."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_from_seed(seed_url: str) -> str:
    host = (urlparse(seed_url).hostname or "").lower().strip()
    if not host:
        raise ValueError(f"Invalid seed URL (no host): {seed_url}")
    return host


def host_hash(host: str) -> str:
    return hashlib.sha256(host.lower().encode("utf-8")).hexdigest()[:12]


def normalize_seed_url(seed_url: str) -> str:
    raw = (seed_url or "").strip()
    if not raw:
        raise ValueError("seed_url is required")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid seed URL: {seed_url}")
    # Keep trailing slash for forum roots.
    if not parsed.path:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return raw


@dataclass
class SiteRecord:
    id: str
    seed_url: str
    host: str
    enabled: bool = True
    label: str = ""
    created_at: str = field(default_factory=utc_now)

    @property
    def short_host(self) -> str:
        host = self.host
        if len(host) <= 48:
            return host
        return host[:20] + "…" + host[-12:]


@dataclass
class SitePaths:
    root: Path
    database: Path
    mirror: Path
    cookies: Path
    secrets: Path
    log: Path

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.mirror.mkdir(parents=True, exist_ok=True)
        if not self.cookies.exists():
            self.cookies.write_text(
                "# Netscape HTTP Cookie File\n",
                encoding="utf-8",
            )


def registry_path(project_root: Path) -> Path:
    return project_root / "data" / "sites.json"


def sites_root(project_root: Path) -> Path:
    return project_root / "data" / "sites"


def site_paths(project_root: Path, site_id: str) -> SitePaths:
    root = sites_root(project_root) / site_id
    return SitePaths(
        root=root,
        database=root / "forum.db",
        mirror=root / "mirror",
        cookies=root / "cookies.txt",
        secrets=root / "secrets.yaml",
        log=root / "crawler.log",
    )


def load_registry(project_root: Path) -> list[SiteRecord]:
    path = registry_path(project_root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid sites registry: {path}") from exc
    items = raw.get("sites") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    sites: list[SiteRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        site_id = str(item.get("id") or "").strip()
        seed_url = str(item.get("seed_url") or "").strip()
        host = str(item.get("host") or "").strip().lower()
        if not site_id or not seed_url:
            continue
        if not host:
            try:
                host = host_from_seed(seed_url)
            except ValueError:
                continue
        sites.append(
            SiteRecord(
                id=site_id,
                seed_url=seed_url,
                host=host,
                enabled=bool(item.get("enabled", True)),
                label=str(item.get("label") or ""),
                created_at=str(item.get("created_at") or utc_now()),
            )
        )
    return sites


def save_registry(project_root: Path, sites: list[SiteRecord]) -> None:
    path = registry_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": utc_now(),
        "sites": [asdict(site) for site in sites],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def get_site(project_root: Path, site_id: str) -> SiteRecord | None:
    site_id = (site_id or "").strip().lower()
    for site in load_registry(project_root):
        if site.id == site_id:
            return site
    return None


def enabled_sites(project_root: Path) -> list[SiteRecord]:
    return [site for site in load_registry(project_root) if site.enabled]


def add_site(
    project_root: Path,
    seed_url: str,
    *,
    label: str = "",
    enabled: bool = True,
) -> SiteRecord:
    seed = normalize_seed_url(seed_url)
    host = host_from_seed(seed)
    site_id = host_hash(host)
    sites = load_registry(project_root)
    for existing in sites:
        if existing.id == site_id or existing.host == host:
            raise ValueError(f"Site already registered: {host}")
    record = SiteRecord(
        id=site_id,
        seed_url=seed,
        host=host,
        enabled=enabled,
        label=(label or "").strip(),
        created_at=utc_now(),
    )
    paths = site_paths(project_root, site_id)
    paths.ensure()
    sites.append(record)
    save_registry(project_root, sites)
    logger.info("Added site %s (%s)", host, site_id)
    return record


def set_site_enabled(project_root: Path, site_id: str, enabled: bool) -> SiteRecord:
    sites = load_registry(project_root)
    for site in sites:
        if site.id == site_id:
            site.enabled = enabled
            save_registry(project_root, sites)
            return site
    raise KeyError(f"Unknown site: {site_id}")


def delete_site(project_root: Path, site_id: str, *, remove_data: bool = True) -> None:
    sites = load_registry(project_root)
    kept = [site for site in sites if site.id != site_id]
    if len(kept) == len(sites):
        raise KeyError(f"Unknown site: {site_id}")
    save_registry(project_root, kept)
    if remove_data:
        root = site_paths(project_root, site_id).root
        if root.exists():
            shutil.rmtree(root)
            logger.info("Removed site data %s", root)


def resolve_site(
    project_root: Path,
    site_id: str | None = None,
    *,
    prefer_enabled: bool = True,
) -> SiteRecord | None:
    sites = load_registry(project_root)
    if not sites:
        return None
    if site_id:
        for site in sites:
            if site.id == site_id:
                return site
        return None
    if prefer_enabled:
        enabled = [site for site in sites if site.enabled]
        if enabled:
            return enabled[0]
    return sites[0]


def migrate_legacy_layout(project_root: Path, seed_url: str | None) -> list[SiteRecord]:
    """Create registry from config seed_url and move legacy data/forum.db if needed."""
    existing = load_registry(project_root)
    if existing:
        return existing

    seed = (seed_url or "").strip()
    if not seed:
        save_registry(project_root, [])
        return []

    try:
        seed = normalize_seed_url(seed)
        host = host_from_seed(seed)
    except ValueError:
        save_registry(project_root, [])
        return []

    site_id = host_hash(host)
    record = SiteRecord(
        id=site_id,
        seed_url=seed,
        host=host,
        enabled=True,
        label="",
        created_at=utc_now(),
    )
    paths = site_paths(project_root, site_id)
    paths.ensure()

    legacy_db = project_root / "data" / "forum.db"
    legacy_mirror = project_root / "data" / "mirror"
    legacy_cookies = project_root / "data" / "cookies.txt"

    if legacy_db.exists() and not paths.database.exists():
        try:
            shutil.move(str(legacy_db), str(paths.database))
        except OSError:
            shutil.copy2(legacy_db, paths.database)
        logger.info("Migrated legacy database to %s", paths.database)
    if legacy_mirror.exists() and legacy_mirror.is_dir():
        if not any(paths.mirror.iterdir()):
            for child in list(legacy_mirror.iterdir()):
                dest = paths.mirror / child.name
                if dest.exists():
                    continue
                try:
                    shutil.move(str(child), str(dest))
                except OSError:
                    try:
                        if child.is_dir():
                            shutil.copytree(child, dest, dirs_exist_ok=True)
                        else:
                            shutil.copy2(child, dest)
                    except OSError as exc:
                        logger.warning("Could not migrate mirror entry %s: %s", child, exc)
            try:
                legacy_mirror.rmdir()
            except OSError:
                pass
            logger.info("Migrated legacy mirror into %s", paths.mirror)
    if legacy_cookies.exists() and (
        not paths.cookies.exists() or paths.cookies.stat().st_size < 64
    ):
        try:
            shutil.copy2(legacy_cookies, paths.cookies)
            logger.info("Migrated legacy cookies to %s", paths.cookies)
        except OSError as exc:
            logger.warning("Could not migrate cookies: %s", exc)

    save_registry(project_root, [record])
    return [record]


def site_summary(project_root: Path, site: SiteRecord) -> dict[str, Any]:
    paths = site_paths(project_root, site.id)
    pages = 0
    alert = ""
    alert_kind = ""
    if paths.database.exists():
        import sqlite3

        try:
            conn = sqlite3.connect(str(paths.database))
            try:
                row = conn.execute("SELECT COUNT(*) FROM pages").fetchone()
                pages = int(row[0] if row else 0)
                alert_row = conn.execute(
                    "SELECT value FROM crawler_state WHERE key = 'alert'"
                ).fetchone()
                kind_row = conn.execute(
                    "SELECT value FROM crawler_state WHERE key = 'alert_kind'"
                ).fetchone()
                alert = str(alert_row[0]) if alert_row and alert_row[0] else ""
                alert_kind = str(kind_row[0]) if kind_row and kind_row[0] else ""
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return {
        "id": site.id,
        "seed_url": site.seed_url,
        "host": site.host,
        "short_host": site.short_host,
        "enabled": site.enabled,
        "label": site.label,
        "created_at": site.created_at,
        "data_dir": str(paths.root),
        "pages": pages,
        "alert": alert,
        "alert_kind": alert_kind,
        "has_cookies": paths.cookies.exists() and paths.cookies.stat().st_size > 40,
        "has_secrets": paths.secrets.exists(),
    }
