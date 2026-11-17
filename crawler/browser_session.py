"""Start/stop the on-demand remote browser and export its cookies."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import AppConfig
from .cookies_io import StoredCookie, merge_stored_cookies, write_netscape

logger = logging.getLogger(__name__)

BROWSER_UNIT = "dark-crawler-browser.service"
DEFAULT_WS_PORT = 6080


def browser_target_path(project_root: Path) -> Path:
    return project_root / "data" / "browser-target.json"


def write_browser_target(project_root: Path, *, site_id: str, seed_url: str) -> Path:
    import json

    path = browser_target_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"site_id": site_id, "seed_url": seed_url}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def browser_profile_dir(project_root: Path, site_id: str | None = None) -> Path:
    env = (os.environ.get("BROWSER_PROFILE_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if site_id:
        return project_root / "data" / "sites" / site_id / "browser-profile"
    return project_root / "data" / "browser-profile"


def browser_ws_port() -> int:
    raw = (os.environ.get("BROWSER_WS_PORT") or "").strip()
    if raw:
        return int(raw)
    return DEFAULT_WS_PORT


def browser_unit_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", BROWSER_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _systemctl(action: str) -> bool:
    if action not in {"start", "stop", "restart"}:
        return False
    commands = [
        ["sudo", "-n", "systemctl", action, BROWSER_UNIT],
        ["systemctl", action, BROWSER_UNIT],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True
            logger.warning(
                "systemctl %s %s failed: %s",
                action,
                BROWSER_UNIT,
                (result.stderr or result.stdout or "").strip(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("systemctl %s error: %s", action, exc)
    return False


def start_browser_session() -> tuple[bool, str]:
    if browser_unit_active():
        return True, "Browser session already running."
    if _systemctl("start"):
        # Give Xvfb/Firefox a moment to come up.
        for _ in range(20):
            if browser_unit_active() and _ws_port_open():
                return True, "Browser session started."
            time.sleep(0.5)
        if browser_unit_active():
            return True, "Browser session started (noVNC still warming up)."
        return False, "Browser unit started but did not stay active. Check journalctl."
    return (
        False,
        "Could not start dark-crawler-browser.service. "
        "Install browser deps and allow sudoers for this unit.",
    )


def stop_browser_session() -> tuple[bool, str]:
    if not browser_unit_active():
        return True, "Browser session is not running."
    if _systemctl("stop"):
        return True, "Browser session stopped."
    return False, "Could not stop dark-crawler-browser.service."


def _ws_port_open() -> bool:
    import socket

    port = browser_ws_port()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def session_status() -> dict[str, object]:
    return {
        "active": browser_unit_active(),
        "ws_ready": _ws_port_open(),
        "ws_port": browser_ws_port(),
        "unit": BROWSER_UNIT,
    }


def export_browser_cookies(
    config: AppConfig,
    *,
    site_id: str | None = None,
    cookies_path: Path | None = None,
) -> tuple[bool, str, int]:
    """Read Firefox moz_cookies (or Chromium Cookies) and write Netscape file."""
    profile = browser_profile_dir(config.project_root, site_id=site_id)
    host = (urlparse(config.seed_url).hostname or "").lower()
    if not host:
        return False, "Invalid seed_url host.", 0

    cookies = _read_firefox_cookies(profile, host)
    if not cookies:
        cookies = _read_chromium_cookies(profile, host)
    if not cookies:
        return (
            False,
            "No cookies found in the browser profile yet. "
            "Solve captcha/login in the session, then Save again.",
            0,
        )

    if cookies_path is None:
        raw = (config.crawl.cookies_file or "").strip()
        cookies_path = Path(raw) if raw else config.project_root / "data" / "cookies.txt"
        if not cookies_path.is_absolute():
            cookies_path = config.project_root / cookies_path
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    if cookies_path.exists():
        changed = merge_stored_cookies(cookies_path, cookies)
        count = len(changed) or len(cookies)
    else:
        write_netscape(cookies_path, cookies)
        count = len(cookies)
    return True, f"Saved {count} cookie(s) from the remote browser.", count


def _read_firefox_cookies(profile: Path, host: str) -> list[StoredCookie]:
    db_path = profile / "cookies.sqlite"
    if not db_path.exists():
        return []
    copy_path = _copy_db(db_path)
    try:
        conn = sqlite3.connect(str(copy_path))
        try:
            rows = conn.execute(
                """
                SELECT host, path, isSecure, expiry, name, value
                FROM moz_cookies
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Firefox cookies read failed: %s", exc)
        return []
    finally:
        copy_path.unlink(missing_ok=True)

    out: list[StoredCookie] = []
    for row in rows:
        domain = str(row[0] or "").lstrip(".").lower()
        if not _domain_matches(domain, host):
            continue
        name = str(row[4] or "")
        if not name:
            continue
        out.append(
            StoredCookie(
                domain=domain or host,
                path=str(row[1] or "/"),
                name=name,
                value=str(row[5] or ""),
                secure=bool(row[2]),
                expires=int(row[3] or 0),
            )
        )
    return out


def _read_chromium_cookies(profile: Path, host: str) -> list[StoredCookie]:
    # Chromium encrypts cookie values on Linux; only attempt plaintext/dev builds.
    candidates = [
        profile / "Default" / "Cookies",
        profile / "Cookies",
    ]
    db_path = next((p for p in candidates if p.exists()), None)
    if db_path is None:
        return []
    copy_path = _copy_db(db_path)
    try:
        conn = sqlite3.connect(str(copy_path))
        try:
            rows = conn.execute(
                """
                SELECT host_key, path, is_secure, expires_utc, name, value
                FROM cookies
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Chromium cookies read failed: %s", exc)
        return []
    finally:
        copy_path.unlink(missing_ok=True)

    out: list[StoredCookie] = []
    for row in rows:
        domain = str(row[0] or "").lstrip(".").lower()
        if not _domain_matches(domain, host):
            continue
        value = str(row[5] or "")
        if not value:
            # Encrypted value — skip; Firefox path is preferred.
            continue
        name = str(row[4] or "")
        if not name:
            continue
        # Chromium expires_utc is microseconds since 1601; treat unknown as session.
        out.append(
            StoredCookie(
                domain=domain or host,
                path=str(row[1] or "/"),
                name=name,
                value=value,
                secure=bool(row[2]),
                expires=0,
            )
        )
    return out


def _copy_db(src: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix="dc-cookies-", suffix=".sqlite")
    os.close(fd)
    dest = Path(name)
    shutil.copy2(src, dest)
    # Also copy WAL/SHM if present so the snapshot is consistent.
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + suffix))
    return dest


def _domain_matches(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.lstrip(".").lower()
    host = host.lower()
    if not domain:
        return False
    return host == domain or host.endswith("." + domain) or domain.endswith(host)
