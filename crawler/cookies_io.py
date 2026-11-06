"""Parse pasted browser cookies and store them as a Netscape cookies file."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class StoredCookie:
    domain: str
    path: str
    name: str
    value: str
    secure: bool = False
    # Unix timestamp. 0 means a session cookie with no expiry.
    expires: int = 0


def default_cookie_path(project_root: Path) -> Path:
    return project_root / "data" / "cookies.txt"


def parse_pasted_cookies(text: str, default_domain: str) -> list[StoredCookie]:
    raw = text.strip()
    if not raw:
        return []

    parsed = _parse_json(raw, default_domain)
    if parsed:
        return _dedupe(parsed)

    cookies: list[StoredCookie] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        if "\t" in line:
            item = _parse_netscape_line(line, default_domain)
            if item:
                cookies.append(item)
            continue
        cookies.extend(_parse_cookie_header(line, default_domain))
    return _dedupe(cookies)


def write_netscape(path: Path, cookies: list[StoredCookie]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Netscape HTTP Cookie File",
        "# Saved by Dark Crawler. Do not commit this file.",
    ]
    for cookie in cookies:
        domain = cookie.domain.lstrip(".").lower()
        flag = "TRUE"
        secure = "TRUE" if cookie.secure else "FALSE"
        expires = str(int(cookie.expires or 0))
        lines.append(
            "\t".join(
                [
                    domain,
                    flag,
                    cookie.path or "/",
                    secure,
                    expires,
                    cookie.name,
                    cookie.value,
                ]
            )
        )
    text = "\n".join(lines) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def read_netscape(path: Path) -> list[StoredCookie]:
    if not path.exists():
        return []
    cookies: list[StoredCookie] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        item = _parse_netscape_line(line, "")
        if item and item.name:
            cookies.append(item)
    return cookies


def merge_stored_cookies(path: Path, updates: list[StoredCookie]) -> list[str]:
    """Upsert cookies into the Netscape file. Returns names whose value changed."""
    if not updates:
        return []
    index: dict[tuple[str, str, str], StoredCookie] = {}
    for cookie in read_netscape(path):
        index[_cookie_key(cookie)] = _normalize_cookie(cookie)
    changed: list[str] = []
    now = int(time.time())
    for cookie in updates:
        normalized = _normalize_cookie(cookie)
        if not normalized.name:
            continue
        key = _cookie_key(normalized)
        if normalized.expires and normalized.expires <= now:
            if key in index:
                del index[key]
                changed.append(normalized.name)
            continue
        previous = index.get(key)
        if (
            previous is None
            or previous.value != normalized.value
            or previous.expires != normalized.expires
            or previous.secure != normalized.secure
        ):
            index[key] = normalized
            changed.append(normalized.name)
    if changed:
        write_netscape(path, list(index.values()))
    return changed


def cookie_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) >= 6 and parts[5]:
            names.append(parts[5])
    return names


def point_config_at_cookies(config_path: Path, cookies_file: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    replacement = f'  cookies_file: "{cookies_file}"'
    updated, count = re.subn(
        r"(?m)^  cookies_file:.*$",
        replacement,
        text,
        count=1,
    )
    if count == 0:
        updated = text.rstrip() + "\n" + replacement + "\n"
    config_path.write_text(updated, encoding="utf-8")


def domain_from_seed(seed_url: str) -> str:
    host = urlparse(seed_url).hostname or ""
    return host.lower()


def _parse_json(raw: str, default_domain: str) -> list[StoredCookie]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = payload if isinstance(payload, list) else [payload]
    cookies: list[StoredCookie] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("Name") or "").strip()
        if not name or "value" not in item and "Value" not in item:
            continue
        value = str(item.get("value") if "value" in item else item.get("Value"))
        domain = str(item.get("domain") or item.get("Domain") or default_domain).lstrip(".")
        path = str(item.get("path") or item.get("Path") or "/")
        secure = bool(item.get("secure") or item.get("Secure") or False)
        cookies.append(
            StoredCookie(domain=domain or default_domain, path=path, name=name, value=value, secure=secure)
        )
    return cookies


def _parse_netscape_line(line: str, default_domain: str) -> StoredCookie | None:
    parts = line.split("\t")
    if len(parts) < 7:
        return None
    domain, _flag, path, secure, expires_raw, name, value = parts[:7]
    if not name:
        return None
    try:
        expires = int(expires_raw)
    except ValueError:
        expires = 0
    return StoredCookie(
        domain=(domain or default_domain).lstrip(".").lower(),
        path=path or "/",
        name=name,
        value=value,
        secure=secure.upper() == "TRUE",
        expires=max(0, expires),
    )


def _normalize_cookie(cookie: StoredCookie) -> StoredCookie:
    return StoredCookie(
        domain=cookie.domain.lstrip(".").lower(),
        path=cookie.path or "/",
        name=cookie.name,
        value=cookie.value,
        secure=cookie.secure,
        expires=max(0, int(cookie.expires or 0)),
    )


def _cookie_key(cookie: StoredCookie) -> tuple[str, str, str]:
    return (cookie.domain.lstrip(".").lower(), cookie.path or "/", cookie.name)


def _parse_cookie_header(line: str, default_domain: str) -> list[StoredCookie]:
    cookies: list[StoredCookie] = []
    for part in line.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name or name.lower() in {"path", "domain", "expires", "max-age", "samesite"}:
            continue
        cookies.append(
            StoredCookie(
                domain=default_domain,
                path="/",
                name=name,
                value=value.strip(),
                secure=False,
            )
        )
    return cookies


def _dedupe(cookies: list[StoredCookie]) -> list[StoredCookie]:
    seen: dict[tuple[str, str, str], StoredCookie] = {}
    for cookie in cookies:
        if not cookie.name:
            continue
        seen[(cookie.domain, cookie.path, cookie.name)] = cookie
    return list(seen.values())
