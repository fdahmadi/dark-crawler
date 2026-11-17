"""Load forum credentials from a local secrets file (never commit real values)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ForumSecrets:
    username: str
    password: str


def default_secrets_path(project_root: Path) -> Path:
    env = (os.environ.get("DARK_CRAWLER_SECRETS") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return project_root / "secrets.yaml"


def _parse_secrets_file(path: Path) -> ForumSecrets | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Secrets file must be a mapping: {path}")
    forum = raw.get("forum") if isinstance(raw.get("forum"), dict) else raw
    if not isinstance(forum, dict):
        return None
    username = str(forum.get("username") or "").strip()
    password = str(forum.get("password") or "")
    if not username or not password:
        return None
    return ForumSecrets(username=username, password=password)


def load_forum_secrets(
    project_root: Path,
    *,
    site_secrets: Path | None = None,
) -> ForumSecrets | None:
    """Prefer per-site secrets.yaml, then project-root secrets.yaml."""
    if site_secrets is not None:
        loaded = _parse_secrets_file(site_secrets)
        if loaded is not None:
            return loaded
    return _parse_secrets_file(default_secrets_path(project_root))


def save_forum_secrets(
    path: Path,
    *,
    username: str,
    password: str,
) -> ForumSecrets:
    """Write credentials to a secrets.yaml path (mode 600 when possible)."""
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise ValueError("Both username and password are required")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "forum": {
            "username": username,
            "password": password,
        }
    }
    path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return ForumSecrets(username=username, password=password)


def secrets_status(
    project_root: Path,
    *,
    site_secrets: Path | None = None,
) -> dict[str, Any]:
    path = site_secrets if site_secrets is not None else default_secrets_path(project_root)
    loaded = None
    error = ""
    try:
        loaded = load_forum_secrets(project_root, site_secrets=site_secrets)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    site_file_loaded = (
        _parse_secrets_file(site_secrets) if site_secrets is not None else None
    )
    effective = path
    if loaded is None and site_secrets is not None:
        effective = default_secrets_path(project_root)
    elif site_file_loaded is not None and site_secrets is not None:
        effective = site_secrets
    return {
        "path": str(effective),
        "exists": Path(effective).exists(),
        "configured": loaded is not None,
        "username": loaded.username if loaded else "",
        "error": error,
        "source": (
            "site"
            if site_file_loaded is not None
            else ("project" if loaded is not None else "none")
        ),
    }
