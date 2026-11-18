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


def infer_host_from_database(database: Path) -> str | None:
    """Return the most common page host in a forum.db, if any."""
    if not database.exists():
        return None
    import sqlite3

    try:
        conn = sqlite3.connect(str(database))
        try:
            rows = conn.execute(
                "SELECT url FROM pages WHERE url IS NOT NULL AND url != '' LIMIT 200"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    counts: dict[str, int] = {}
    for (url,) in rows:
        try:
            host = host_from_seed(str(url))
        except ValueError:
            continue
        counts[host] = counts.get(host, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def _move_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        shutil.move(str(src), str(dest))
    except OSError:
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)


def _ensure_site_record(
    sites: list[SiteRecord],
    *,
    host: str,
    seed_url: str,
    enabled: bool = True,
) -> tuple[list[SiteRecord], SiteRecord]:
    site_id = host_hash(host)
    for existing in sites:
        if existing.id == site_id or existing.host == host:
            return sites, existing
    record = SiteRecord(
        id=site_id,
        seed_url=normalize_seed_url(seed_url),
        host=host,
        enabled=enabled,
        label="",
        created_at=utc_now(),
    )
    sites = [*sites, record]
    return sites, record


def _relocate_crawl_artifacts(
    project_root: Path,
    *,
    from_id: str,
    to_id: str,
    expected_host: str,
) -> None:
    """Move DB + matching mirror host folder; leave auth profile/cookies in place."""
    if from_id == to_id:
        return
    src = site_paths(project_root, from_id)
    dest = site_paths(project_root, to_id)
    dest.ensure()

    if src.database.exists() and not dest.database.exists():
        _move_or_copy(src.database, dest.database)
        for suffix in ("-wal", "-shm"):
            side = Path(str(src.database) + suffix)
            if side.exists():
                _move_or_copy(side, Path(str(dest.database) + suffix))
        logger.info(
            "Moved mismatched database from %s to %s (host %s)",
            from_id,
            to_id,
            expected_host,
        )

    mirror_host = src.mirror / expected_host
    if mirror_host.exists():
        target = dest.mirror / expected_host
        if not target.exists():
            _move_or_copy(mirror_host, target)
            logger.info("Moved mirror host folder %s -> %s", mirror_host, target)


def repair_mismatched_site_data(project_root: Path) -> list[SiteRecord]:
    """If a site folder holds another host's crawl DB, move it to the right id."""
    sites = load_registry(project_root)
    if not sites:
        return sites
    changed = False
    for site in list(sites):
        paths = site_paths(project_root, site.id)
        inferred = infer_host_from_database(paths.database)
        if not inferred or inferred == site.host:
            continue
        correct_id = host_hash(inferred)
        if correct_id == site.id:
            continue
        seed = f"http://{inferred}/"
        sites, _record = _ensure_site_record(
            sites,
            host=inferred,
            seed_url=seed,
            enabled=False,
        )
        _relocate_crawl_artifacts(
            project_root,
            from_id=site.id,
            to_id=correct_id,
            expected_host=inferred,
        )
        changed = True
        logger.warning(
            "Repaired site data: crawl for %s was under %s (%s); moved to %s",
            inferred,
            site.host,
            site.id,
            correct_id,
        )
    if changed:
        save_registry(project_root, sites)
    return sites


def migrate_legacy_layout(project_root: Path, seed_url: str | None) -> list[SiteRecord]:
    """Create registry from config seed_url and move legacy data/forum.db if needed."""
    existing = load_registry(project_root)
    if existing:
        return repair_mismatched_site_data(project_root)

    seed = (seed_url or "").strip()
    legacy_db = project_root / "data" / "forum.db"
    legacy_mirror = project_root / "data" / "mirror"
    legacy_cookies = project_root / "data" / "cookies.txt"

    config_host: str | None = None
    config_seed = ""
    if seed:
        try:
            config_seed = normalize_seed_url(seed)
            config_host = host_from_seed(config_seed)
        except ValueError:
            config_host = None

    data_host = infer_host_from_database(legacy_db) if legacy_db.exists() else None
    if data_host is None and legacy_mirror.exists() and legacy_mirror.is_dir():
        for child in legacy_mirror.iterdir():
            if child.is_dir() and "." in child.name:
                data_host = child.name.lower()
                break

    sites: list[SiteRecord] = []
    if not config_host and not data_host:
        save_registry(project_root, [])
        return []

    # Attach legacy crawl artifacts to the host they actually belong to.
    artifact_host = data_host or config_host
    assert artifact_host is not None
    artifact_seed = (
        f"http://{artifact_host}/"
        if data_host and data_host != config_host
        else (config_seed or f"http://{artifact_host}/")
    )
    sites, artifact_site = _ensure_site_record(
        sites,
        host=artifact_host,
        seed_url=artifact_seed,
        enabled=True,
    )
    artifact_paths = site_paths(project_root, artifact_site.id)
    artifact_paths.ensure()

    if legacy_db.exists() and not artifact_paths.database.exists():
        _move_or_copy(legacy_db, artifact_paths.database)
        logger.info("Migrated legacy database to %s", artifact_paths.database)
    if legacy_mirror.exists() and legacy_mirror.is_dir():
        for child in list(legacy_mirror.iterdir()):
            dest = artifact_paths.mirror / child.name
            if dest.exists():
                continue
            try:
                _move_or_copy(child, dest)
            except OSError as exc:
                logger.warning("Could not migrate mirror entry %s: %s", child, exc)
        try:
            if legacy_mirror.exists() and not any(legacy_mirror.iterdir()):
                legacy_mirror.rmdir()
        except OSError:
            pass
        logger.info("Migrated legacy mirror into %s", artifact_paths.mirror)
    if legacy_cookies.exists() and (
        not artifact_paths.cookies.exists() or artifact_paths.cookies.stat().st_size < 64
    ):
        try:
            shutil.copy2(legacy_cookies, artifact_paths.cookies)
            logger.info("Migrated legacy cookies to %s", artifact_paths.cookies)
        except OSError as exc:
            logger.warning("Could not migrate cookies: %s", exc)

    # Keep config seed as its own site even when legacy data belonged elsewhere.
    if config_host and config_host != artifact_host:
        sites, config_site = _ensure_site_record(
            sites,
            host=config_host,
            seed_url=config_seed,
            enabled=True,
        )
        site_paths(project_root, config_site.id).ensure()

    save_registry(project_root, sites)
    return sites


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
