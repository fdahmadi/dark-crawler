"""Start/stop the on-demand remote browser and export its cookies."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import AppConfig, load_config
from .cookies_io import StoredCookie, merge_stored_cookies, write_netscape

logger = logging.getLogger(__name__)

BROWSER_UNIT = "dark-crawler-browser.service"
DEFAULT_WS_PORT = 6080


def _project_root() -> Path:
    env = (os.environ.get("DARK_CRAWLER_ROOT") or "").strip()
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[1]


def _config_path() -> Path:
    env = (os.environ.get("DARK_CRAWLER_CONFIG") or "").strip()
    if env:
        return Path(env).resolve()
    return _project_root() / "config.yaml"


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


def _pid_path(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    return root / "data" / "browser-session.pid"


def _mode_path(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    return root / "data" / "browser-session.mode"


def _script_path(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    return root / "deploy" / "browser-session.sh"


def _session_mode(project_root: Path | None = None) -> str:
    path = _mode_path(project_root)
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or "unknown"
    if local_browser_active(project_root):
        return "local"
    return "systemd" if Path(f"/etc/systemd/system/{BROWSER_UNIT}").exists() else "none"


def _read_pid(project_root: Path | None = None) -> int | None:
    path = _pid_path(project_root)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def local_browser_active(project_root: Path | None = None) -> bool:
    pid = _read_pid(project_root)
    if pid is None:
        return False
    if _pid_alive(pid):
        return True
    _pid_path(project_root).unlink(missing_ok=True)
    return False


def browser_unit_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", BROWSER_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.stdout.strip() == "active":
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return local_browser_active()


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
            logger.debug(
                "systemctl %s %s failed: %s",
                action,
                BROWSER_UNIT,
                (result.stderr or result.stdout or "").strip(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("systemctl %s error: %s", action, exc)
    return False


def _missing_local_deps() -> list[str]:
    needed = {
        "Xvfb": "xvfb",
        "x11vnc": "x11vnc",
        "websockify": "websockify",
    }
    missing: list[str] = []
    for binary, package in needed.items():
        if shutil.which(binary) is None:
            missing.append(package)
    if not Path("/usr/share/novnc/vnc.html").exists():
        missing.append("novnc")
    has_browser = any(
        shutil.which(name)
        for name in ("firefox-esr", "firefox", "chromium", "chromium-browser")
    )
    if not has_browser:
        missing.append("firefox-esr")
    # Dedupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for item in missing:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _local_env(project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    config_path = _config_path()
    env["DARK_CRAWLER_ROOT"] = str(project_root)
    env["DARK_CRAWLER_CONFIG"] = str(config_path)
    env.setdefault("BROWSER_WS_PORT", str(browser_ws_port()))
    env.setdefault("BROWSER_VNC_PORT", "5901")
    env.setdefault("BROWSER_DISPLAY", "99")
    env.setdefault("BROWSER_IDLE_SECONDS", "1800")
    try:
        config = load_config(config_path)
        env["TOR_SOCKS_HOST"] = config.tor.socks_host
        env["TOR_SOCKS_PORT"] = str(config.tor.port)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load config for browser SOCKS defaults: %s", exc)
        env.setdefault("TOR_SOCKS_HOST", "127.0.0.1")
        env.setdefault("TOR_SOCKS_PORT", "9050")
    # Prefer apt firefox-esr over snap when both exist.
    esr = shutil.which("firefox-esr")
    if esr:
        env["BROWSER_FIREFOX_BIN"] = esr
    return env


def _detect_display() -> str | None:
    for candidate in (
        (os.environ.get("DISPLAY") or "").strip(),
        ":0",
        ":1",
    ):
        if not candidate:
            continue
        # X11 socket names drop the leading colon: :0 -> /tmp/.X11-unix/X0
        name = candidate.split(".")[0]
        if name.startswith(":"):
            sock = Path("/tmp/.X11-unix") / f"X{name[1:]}"
            if sock.exists():
                return candidate
    return None


def _firefox_bin() -> str | None:
    # Prefer apt browsers; snap firefox often cannot use custom profile dirs.
    for name in (
        "firefox-esr",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "firefox",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def _read_browser_target(project_root: Path) -> tuple[str, str]:
    import json

    path = browser_target_path(project_root)
    if not path.exists():
        return "", ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "", ""
    return str(raw.get("seed_url") or "").strip(), str(raw.get("site_id") or "").strip()


def _write_firefox_user_js(profile: Path, *, socks_host: str, socks_port: int, seed_url: str) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "user.js").write_text(
        "\n".join(
            [
                'user_pref("network.proxy.type", 1);',
                f'user_pref("network.proxy.socks", "{socks_host}");',
                f'user_pref("network.proxy.socks_port", {int(socks_port)});',
                'user_pref("network.proxy.socks_remote_dns", true);',
                'user_pref("network.proxy.socks_version", 5);',
                'user_pref("browser.shell.checkDefaultBrowser", false);',
                'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
                'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);',
                f'user_pref("browser.startup.homepage", "{seed_url}");',
                'user_pref("startup.homepage_welcome_url", "");',
                'user_pref("startup.homepage_welcome_url.additional", "");',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _start_headed_desktop_browser(project_root: Path) -> tuple[bool, str]:
    display = _detect_display()
    firefox = _firefox_bin()
    if not display:
        return False, "No local X11 display found for headed browser mode."
    if not firefox:
        return False, "No Firefox/Chromium binary found."

    seed_url, site_id = _read_browser_target(project_root)
    if not seed_url:
        return False, "No browser target. Open Auth for a site first."

    env = _local_env(project_root)
    env["DISPLAY"] = display
    socks_host = env.get("TOR_SOCKS_HOST", "127.0.0.1")
    socks_port = int(env.get("TOR_SOCKS_PORT", "9050"))
    profile = browser_profile_dir(project_root, site_id=site_id or None)
    _write_firefox_user_js(
        profile,
        socks_host=socks_host,
        socks_port=socks_port,
        seed_url=seed_url,
    )

    log_path = project_root / "data" / "browser-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str]
    if Path(firefox).name.startswith("firefox"):
        cmd = [firefox, "--profile", str(profile), "--no-remote", "--new-instance", seed_url]
    else:
        cmd = [
            firefox,
            f"--user-data-dir={profile}",
            f"--proxy-server=socks5://{socks_host}:{socks_port}",
            "--no-first-run",
            "--disable-gpu",
            seed_url,
        ]

    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _pid_path(project_root).write_text(str(proc.pid), encoding="utf-8")
    _mode_path(project_root).write_text("headed", encoding="utf-8")
    time.sleep(1.0)
    if proc.poll() is not None:
        detail = log_path.read_text(encoding="utf-8", errors="ignore")[-400:]
        _pid_path(project_root).unlink(missing_ok=True)
        _mode_path(project_root).unlink(missing_ok=True)
        return (
            False,
            "Desktop browser exited early (snap Firefox often fails with custom profiles). "
            + (detail.strip() or f"See {log_path}"),
        )
    return (
        True,
        "Opened Firefox on your desktop through Tor. Solve captcha/login there, then Save session cookies.",
    )


def _start_novnc_browser(project_root: Path) -> tuple[bool, str]:
    script = _script_path(project_root)
    if not script.exists():
        return False, f"Browser script not found: {script}"

    log_path = project_root / "data" / "browser-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = _local_env(project_root)
    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(
            ["bash", str(script)],
            cwd=str(project_root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _pid_path(project_root).write_text(str(proc.pid), encoding="utf-8")
    _mode_path(project_root).write_text("novnc", encoding="utf-8")

    for _ in range(30):
        if proc.poll() is not None:
            detail = ""
            if log_path.exists():
                detail = log_path.read_text(encoding="utf-8", errors="ignore")[-500:]
            _pid_path(project_root).unlink(missing_ok=True)
            _mode_path(project_root).unlink(missing_ok=True)
            return (
                False,
                "Local browser session exited early. "
                + (detail.strip() or f"See {log_path}"),
            )
        if _ws_port_open():
            return True, "Browser session started (local noVNC)."
        time.sleep(0.5)

    if _pid_alive(proc.pid):
        return True, "Browser session started (noVNC still warming up)."
    _pid_path(project_root).unlink(missing_ok=True)
    _mode_path(project_root).unlink(missing_ok=True)
    return False, f"Local browser session failed to open noVNC. See {log_path}"


def _start_local_browser(project_root: Path) -> tuple[bool, str]:
    if local_browser_active(project_root):
        return True, "Browser session already running."

    missing = _missing_local_deps()
    if not missing:
        return _start_novnc_browser(project_root)

    # Desktop fallback: open Firefox on the local display (no root packages needed).
    ok, message = _start_headed_desktop_browser(project_root)
    if ok:
        return ok, message

    return (
        False,
        message
        + " Or install noVNC stack with: sudo apt install "
        + " ".join(missing),
    )


def _stop_local_browser(project_root: Path) -> tuple[bool, str]:
    pid = _read_pid(project_root)
    if pid is None:
        return True, "Browser session is not running."
    if not _pid_alive(pid):
        _pid_path(project_root).unlink(missing_ok=True)
        return True, "Browser session is not running."
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.25)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    _pid_path(project_root).unlink(missing_ok=True)
    _mode_path(project_root).unlink(missing_ok=True)
    return True, "Browser session stopped."


def start_browser_session() -> tuple[bool, str]:
    if browser_unit_active():
        return True, "Browser session already running."

    # Prefer systemd when the unit exists (VPS).
    unit_exists = Path(f"/etc/systemd/system/{BROWSER_UNIT}").exists()
    if unit_exists and _systemctl("start"):
        for _ in range(20):
            if browser_unit_active() and _ws_port_open():
                return True, "Browser session started."
            time.sleep(0.5)
        if browser_unit_active():
            return True, "Browser session started (noVNC still warming up)."
        return False, "Browser unit started but did not stay active. Check journalctl."

    # Local / no-systemd fallback.
    return _start_local_browser(_project_root())


def stop_browser_session() -> tuple[bool, str]:
    root = _project_root()
    if local_browser_active(root):
        return _stop_local_browser(root)
    if not browser_unit_active():
        return True, "Browser session is not running."
    if _systemctl("stop"):
        return True, "Browser session stopped."
    # Last resort: try local stop anyway.
    return _stop_local_browser(root)


def _ws_port_open() -> bool:
    import socket

    port = browser_ws_port()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def session_status() -> dict[str, object]:
    root = _project_root()
    local = local_browser_active(root)
    mode = _session_mode(root)
    return {
        "active": browser_unit_active(),
        "ws_ready": _ws_port_open() if mode != "headed" else False,
        "ws_port": browser_ws_port(),
        "unit": "local-process" if local else BROWSER_UNIT,
        "mode": mode,
        "headed": mode == "headed",
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
            continue
        name = str(row[4] or "")
        if not name:
            continue
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
