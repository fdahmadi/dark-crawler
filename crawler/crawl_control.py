"""Start/stop the crawler via systemd (VPS) or a local background process."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CRAWLER_UNIT = "dark-crawler.service"


def _project_root() -> Path:
    env = (os.environ.get("DARK_CRAWLER_ROOT") or "").strip()
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[1]


def _config_path(project_root: Path | None = None) -> Path:
    env = (os.environ.get("DARK_CRAWLER_CONFIG") or "").strip()
    if env:
        return Path(env).resolve()
    root = project_root or _project_root()
    return root / "config.yaml"


def _pid_path(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    return root / "data" / "crawl.pid"


def _log_path(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    return root / "data" / "crawl-run.log"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_local_pid(project_root: Path | None = None) -> int | None:
    path = _pid_path(project_root)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        path.unlink(missing_ok=True)
        return None
    return pid


def local_crawl_active(project_root: Path | None = None) -> bool:
    return _read_local_pid(project_root) is not None


def systemd_unit_exists(unit: str = CRAWLER_UNIT) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "cat", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def systemd_unit_active(unit: str = CRAWLER_UNIT) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def crawler_active(project_root: Path | None = None) -> bool:
    return systemd_unit_active() or local_crawl_active(project_root)


def stop_local_crawl(project_root: Path | None = None) -> None:
    root = project_root or _project_root()
    pid = _read_local_pid(root)
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for _ in range(20):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    _pid_path(root).unlink(missing_ok=True)


def _python_bin(project_root: Path) -> str:
    venv_python = project_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def start_local_crawl(project_root: Path | None = None) -> tuple[bool, str]:
    root = project_root or _project_root()
    stop_local_crawl(root)

    config = _config_path(root)
    if not config.exists():
        return False, f"Config not found: {config}"

    log_path = _log_path(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    python_bin = _python_bin(root)
    cmd = [python_bin, "-m", "crawler", "-c", str(config), "crawl"]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("DARK_CRAWLER_ROOT", str(root))

    with log_path.open("ab") as log_handle:
        log_handle.write(f"\n--- crawl start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        log_handle.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _pid_path(root).write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.8)
    if proc.poll() is not None:
        detail = ""
        if log_path.exists():
            detail = log_path.read_text(encoding="utf-8", errors="ignore")[-500:]
        _pid_path(root).unlink(missing_ok=True)
        return (
            False,
            "Local crawl exited early. "
            + (detail.strip() or f"See {log_path}"),
        )
    return True, f"Local crawl started (pid {proc.pid}). Log: {log_path}"


def _start_systemd(action: str) -> bool:
    """Passwordless systemd only — never call bare systemctl start (triggers polkit)."""
    if not systemd_unit_exists():
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", action, CRAWLER_UNIT],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return True
        logger.info(
            "sudo systemctl %s %s failed: %s",
            action,
            CRAWLER_UNIT,
            (result.stderr or result.stdout or "").strip()[:200],
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("systemd crawl start unavailable: %s", exc)
    return False


def start_crawler(action: str = "start", project_root: Path | None = None) -> tuple[bool, str]:
    if action not in {"start", "restart"}:
        action = "start"
    root = project_root or _project_root()

    if _start_systemd(action):
        return True, f"Started via systemd ({CRAWLER_UNIT})."

    ok, message = start_local_crawl(root)
    if ok:
        return True, message
    return False, message


def stop_crawler(project_root: Path | None = None) -> tuple[bool, str]:
    root = project_root or _project_root()
    stopped = False

    if systemd_unit_exists() and systemd_unit_active():
        if _start_systemd("stop"):
            stopped = True
        else:
            return False, f"Could not stop systemd unit {CRAWLER_UNIT} (need passwordless sudo)."

    if local_crawl_active(root):
        stop_local_crawl(root)
        stopped = True

    if stopped or not crawler_active(root):
        return True, "Crawler stopped."
    return False, "Crawler was not running."
