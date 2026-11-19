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
DEFAULT_CDP_PORT = 9333


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


def login_open_url(seed_url: str) -> str:
    """Default forum login URL to open in the auth browser session."""
    from urllib.parse import urljoin

    seed = (seed_url or "").strip()
    if not seed:
        return seed
    base = seed if seed.endswith("/") else seed + "/"
    # MyBB / common forum login entry points (darkfox and similar).
    return urljoin(base, "member.php?action=login")


def write_browser_target(
    project_root: Path,
    *,
    site_id: str,
    seed_url: str,
    open_url: str | None = None,
) -> Path:
    import json

    path = browser_target_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "site_id": site_id,
        "seed_url": seed_url,
        "open_url": (open_url or seed_url).strip() or seed_url,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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


def browser_cdp_port() -> int:
    raw = (os.environ.get("BROWSER_CDP_PORT") or "").strip()
    if raw:
        return int(raw)
    return DEFAULT_CDP_PORT


def _cdp_port_file(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    return root / "data" / "browser-cdp.port"


def _write_cdp_port(port: int, project_root: Path | None = None) -> None:
    path = _cdp_port_file(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(port)), encoding="utf-8")


def _clear_cdp_port(project_root: Path | None = None) -> None:
    _cdp_port_file(project_root).unlink(missing_ok=True)


def _read_devtools_active_port(profile: Path) -> int | None:
    for candidate in (
        profile / "DevToolsActivePort",
        profile / "Default" / "DevToolsActivePort",
    ):
        if not candidate.exists():
            continue
        try:
            first = candidate.read_text(encoding="utf-8").splitlines()[0].strip()
        except OSError:
            continue
        if first.isdigit():
            port = int(first)
            if port > 0:
                return port
    return None


def _wait_for_cdp_port(profile: Path, preferred: int, timeout: float = 12.0) -> int | None:
    """Wait until Chrome publishes a DevTools port (or preferred port opens)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        discovered = _read_devtools_active_port(profile)
        if discovered is not None and _tcp_port_open(discovered):
            return discovered
        if preferred > 0 and _tcp_port_open(preferred):
            return preferred
        time.sleep(0.25)
    discovered = _read_devtools_active_port(profile)
    if discovered is not None and _tcp_port_open(discovered):
        return discovered
    if preferred > 0 and _tcp_port_open(preferred):
        return preferred
    return None


def _tcp_port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def resolve_cdp_port(profile: Path | None = None) -> int | None:
    """Resolve the live Chrome DevTools port for cookie export."""
    root = _project_root()
    stored = _cdp_port_file(root)
    if stored.exists():
        try:
            port = int(stored.read_text(encoding="utf-8").strip())
        except ValueError:
            port = 0
        if port > 0 and _tcp_port_open(port):
            return port
    if profile is not None:
        discovered = _read_devtools_active_port(profile)
        if discovered is not None and _tcp_port_open(discovered):
            return discovered
    preferred = browser_cdp_port()
    if _tcp_port_open(preferred):
        return preferred
    return None


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


def _read_browser_target(project_root: Path) -> tuple[str, str, str]:
    """Return (open_url, site_id, seed_url). open_url falls back to seed_url."""
    import json

    path = browser_target_path(project_root)
    if not path.exists():
        return "", "", ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "", "", ""
    seed_url = str(raw.get("seed_url") or "").strip()
    open_url = str(raw.get("open_url") or "").strip() or seed_url
    site_id = str(raw.get("site_id") or "").strip()
    return open_url, site_id, seed_url


def _write_firefox_user_js(
    profile: Path,
    *,
    socks_host: str,
    socks_port: int,
    seed_url: str,
    enable_unsigned_addons: bool = False,
) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    lines = [
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
    ]
    if enable_unsigned_addons:
        # Allow the temporary login-autofill XPI sideloaded into this profile.
        lines.extend(
            [
                'user_pref("xpinstall.signatures.required", false);',
                'user_pref("extensions.langpacks.signatures.required", false);',
                'user_pref("extensions.autoDisableScopes", 0);',
                'user_pref("extensions.enabledScopes", 15);',
            ]
        )
    lines.append("")
    (profile / "user.js").write_text("\n".join(lines), encoding="utf-8")


def _prepare_login_autofill(project_root: Path, site_id: str, profile: Path) -> Path | None:
    if not site_id:
        return None
    try:
        from .browser_autofill import install_autofill_for_site

        return install_autofill_for_site(
            project_root,
            site_id=site_id,
            profile=profile,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not install login autofill: %s", exc)
        return None


def _start_headed_desktop_browser(project_root: Path) -> tuple[bool, str]:
    display = _detect_display()
    firefox = _firefox_bin()
    if not display:
        return False, "No local X11 display found for headed browser mode."
    if not firefox:
        return False, "No Firefox/Chromium binary found."

    open_url, site_id, seed_url = _read_browser_target(project_root)
    if not open_url:
        return False, "No browser target. Open Auth for a site first."

    env = _local_env(project_root)
    env["DISPLAY"] = display
    socks_host = env.get("TOR_SOCKS_HOST", "127.0.0.1")
    socks_port = int(env.get("TOR_SOCKS_PORT", "9050"))
    profile = browser_profile_dir(project_root, site_id=site_id or None)
    autofill_dir = _prepare_login_autofill(project_root, site_id, profile)
    _write_firefox_user_js(
        profile,
        socks_host=socks_host,
        socks_port=socks_port,
        seed_url=open_url,
        enable_unsigned_addons=autofill_dir is not None,
    )

    log_path = project_root / "data" / "browser-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str]
    if Path(firefox).name.startswith("firefox"):
        cmd = [firefox, "--profile", str(profile), "--no-remote", "--new-instance", open_url]
    else:
        # Force DNS through SOCKS so .onion hosts resolve (socks5 alone is not enough).
        # --test-type hides Chrome's "unsupported command-line flag" infobar for that rule.
        # Live cookie export uses Chrome DevTools (CDP); port is discovered via DevToolsActivePort.
        cdp_port = browser_cdp_port()
        for stale in (
            profile / "DevToolsActivePort",
            profile / "Default" / "DevToolsActivePort",
            profile / "SingletonLock",
            profile / "SingletonSocket",
            profile / "SingletonCookie",
        ):
            stale.unlink(missing_ok=True)
        _clear_cdp_port(project_root)
        cmd = [
            firefox,
            f"--user-data-dir={profile}",
            f"--proxy-server=socks5://{socks_host}:{socks_port}",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            "--test-type",
            "--password-store=basic",
            f"--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={cdp_port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--disable-gpu",
            "--new-window",
        ]
        if autofill_dir is not None:
            cmd.append(f"--load-extension={autofill_dir}")
            cmd.append(f"--disable-extensions-except={autofill_dir}")
        cmd.append(open_url)

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
        _clear_cdp_port(project_root)
        return (
            False,
            "Desktop browser exited early (snap Firefox often fails with custom profiles). "
            + (detail.strip() or f"See {log_path}"),
        )

    live_cdp = None
    if not Path(firefox).name.startswith("firefox"):
        live_cdp = _wait_for_cdp_port(profile, browser_cdp_port())
        if live_cdp is not None:
            _write_cdp_port(live_cdp, project_root)
        else:
            logger.warning(
                "Chrome started but DevTools port is not reachable; cookie Save may fail"
            )

    if autofill_dir is not None:
        base = (
            "Opened browser through Tor with saved credentials autofill. "
            "Log in, then Save session cookies while this window stays open."
        )
    else:
        base = (
            "Opened browser through Tor. Log in, then Save session cookies "
            "while this window stays open."
        )
    if live_cdp is None and not Path(firefox).name.startswith("firefox"):
        base += " Warning: live cookie export is not ready yet — wait a few seconds before Save."
    return True, base


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


def _clear_auth_block_after_cookie_save(database_path: Path, seed_url: str) -> None:
    """Clear captcha/login alert and re-queue seed after cookies were exported."""
    if not database_path.exists():
        return
    from .db import utc_now
    from .parser import normalize_url

    seed = normalize_url(seed_url, seed_url) or seed_url
    conn = sqlite3.connect(str(database_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawler_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute("DELETE FROM crawler_state WHERE key IN ('alert', 'alert_kind')")
        conn.execute(
            """
            DELETE FROM pages
            WHERE url = ?
               OR COALESCE(title, '') LIKE '%Verify Humanity%'
               OR COALESCE(html, '') LIKE '%altcha-widget%'
            """,
            (seed,),
        )
        conn.execute("DELETE FROM links WHERE from_url = ?", (seed,))
        now = utc_now()
        conn.execute(
            """
            INSERT INTO queue(url, depth, status, enqueued_at)
            VALUES (?, 0, 'pending', ?)
            ON CONFLICT(url) DO UPDATE SET
                status = 'pending',
                depth = 0,
                updated_at = excluded.enqueued_at
            """,
            (seed, now),
        )
        conn.commit()
    finally:
        conn.close()


def export_cookies_for_active_target() -> tuple[bool, str, int]:
    """Export cookies for the site currently opened in the auth browser session."""
    root = _project_root()
    _open_url, site_id, seed_url = _read_browser_target(root)
    if not site_id or not seed_url:
        return False, "No browser target site is selected.", 0
    try:
        config = load_config(_config_path())
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not load config: {exc}", 0
    from dataclasses import replace

    from .sites import site_paths

    paths = site_paths(root, site_id)
    scoped = replace(
        config,
        seed_url=seed_url,
        crawl=replace(config.crawl, cookies_file=str(paths.cookies)),
    )
    return export_browser_cookies(
        scoped,
        site_id=site_id,
        cookies_path=paths.cookies,
    )


def stop_browser_session() -> tuple[bool, str]:
    root = _project_root()
    saved_note = ""
    # Auto-save cookies via CDP before killing Chrome.
    if local_browser_active(root) or browser_unit_active():
        ok, message, count = export_cookies_for_active_target()
        if ok and count:
            saved_note = f" {message}"
            try:
                from .sites import site_paths

                _open_url, site_id, seed_url = _read_browser_target(root)
                if site_id and seed_url:
                    paths = site_paths(root, site_id)
                    _clear_auth_block_after_cookie_save(paths.database, seed_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not clear auth alert after auto-save: %s", exc)
        elif not ok:
            logger.info("Auto-save on stop skipped: %s", message)

    if local_browser_active(root):
        ok, text = _stop_local_browser(root)
        _clear_cdp_port(root)
        return ok, text + saved_note
    if not browser_unit_active():
        _clear_cdp_port(root)
        return True, "Browser session is not running." + saved_note
    if _systemctl("stop"):
        _clear_cdp_port(root)
        return True, "Browser session stopped." + saved_note
    ok, text = _stop_local_browser(root)
    _clear_cdp_port(root)
    return ok, text + saved_note


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
    _, site_id, _ = _read_browser_target(root)
    profile = browser_profile_dir(root, site_id=site_id or None) if site_id else None
    cdp_port = resolve_cdp_port(profile) if local or mode == "headed" else None
    return {
        "active": browser_unit_active(),
        "ws_ready": _ws_port_open() if mode != "headed" else False,
        "ws_port": browser_ws_port(),
        "cdp_ready": cdp_port is not None,
        "cdp_port": cdp_port or 0,
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

    # Prefer live CDP export while Chrome is open (cookie DB values are encrypted).
    cookies = _read_chromium_cookies_cdp(host, profile=profile)
    if not cookies:
        cookies = _read_firefox_cookies(profile, host)
    if not cookies:
        cookies = _read_chromium_cookies(profile, host)
    if not cookies:
        cdp = resolve_cdp_port(profile)
        if cdp is None:
            return (
                False,
                "Browser DevTools is not reachable for live cookie export. "
                "Open browser session, wait until the page loads, log in, "
                "then Save while the window is still open (or just Stop — it auto-saves).",
                0,
            )
        if _chromium_has_encrypted_cookies(profile, host):
            return (
                False,
                "No cookies exported yet from the open browser. "
                "Finish login in the Chrome window first, then Save (or Stop).",
                0,
            )
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


def _read_chromium_cookies_cdp(
    host: str,
    *,
    profile: Path | None = None,
) -> list[StoredCookie]:
    """Read plaintext cookies from a live Chrome session via DevTools protocol."""
    import asyncio
    import json
    import urllib.error
    import urllib.request

    port = resolve_cdp_port(profile)
    if port is None:
        logger.debug("No live Chrome DevTools port for cookie export")
        return []
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=2.0,
        ) as response:
            meta = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("CDP version endpoint unavailable on %s: %s", port, exc)
        return []

    ws_url = str(meta.get("webSocketDebuggerUrl") or "").strip()
    if not ws_url:
        return []

    async def _fetch() -> list[dict]:
        import websockets

        # Chrome 120+ browser targets expose Storage.getCookies; Network.getAllCookies
        # is often missing on the browser websocket endpoint.
        methods = ("Storage.getCookies", "Network.getAllCookies")
        async with websockets.connect(ws_url, max_size=16_000_000) as ws:
            for index, method in enumerate(methods, start=1):
                await ws.send(
                    json.dumps({"id": index, "method": method, "params": {}})
                )
                while True:
                    message = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=5.0)
                    )
                    if message.get("id") != index:
                        continue
                    if message.get("error"):
                        logger.debug(
                            "CDP %s failed: %s",
                            method,
                            message.get("error"),
                        )
                        break
                    result = message.get("result") or {}
                    cookies = result.get("cookies") or []
                    if isinstance(cookies, list) and cookies:
                        return cookies
                    break
        return []

    try:
        raw_cookies = asyncio.run(_fetch())
    except Exception as exc:  # noqa: BLE001
        logger.warning("CDP cookie fetch failed: %s", exc)
        return []

    out: list[StoredCookie] = []
    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if not _domain_matches(domain, host):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if not name or not value:
            continue
        expires_raw = item.get("expires")
        try:
            expires = int(float(expires_raw)) if expires_raw is not None else 0
        except (TypeError, ValueError):
            expires = 0
        if expires < 0:
            expires = 0
        out.append(
            StoredCookie(
                domain=domain or host,
                path=str(item.get("path") or "/"),
                name=name,
                value=value,
                secure=bool(item.get("secure")),
                expires=expires,
            )
        )
    return out


def _chromium_cookie_db(profile: Path) -> Path | None:
    candidates = [
        profile / "Default" / "Cookies",
        profile / "Cookies",
    ]
    return next((path for path in candidates if path.exists()), None)


def _chromium_has_encrypted_cookies(profile: Path, host: str) -> bool:
    db_path = _chromium_cookie_db(profile)
    if db_path is None:
        return False
    copy_path = _copy_db(db_path)
    try:
        conn = sqlite3.connect(str(copy_path))
        try:
            rows = conn.execute(
                """
                SELECT host_key, name, value, encrypted_value
                FROM cookies
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    finally:
        copy_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(copy_path) + suffix).unlink(missing_ok=True)

    for row in rows:
        domain = str(row[0] or "").lstrip(".").lower()
        if not _domain_matches(domain, host):
            continue
        value = str(row[2] or "")
        encrypted = row[3] or b""
        if not value and encrypted:
            return True
    return False


def _read_chromium_cookies(profile: Path, host: str) -> list[StoredCookie]:
    db_path = _chromium_cookie_db(profile)
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
        for suffix in ("-wal", "-shm"):
            Path(str(copy_path) + suffix).unlink(missing_ok=True)

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
