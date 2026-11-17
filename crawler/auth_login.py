"""One-click forum login over Tor using stored secrets + session cookies."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .auth_wall import detect_auth_wall
from .config import AppConfig
from .secrets import ForumSecrets, load_forum_secrets
from .tor_client import TorClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    message: str
    kind: str | None = None


def ensure_cookies_file(config: AppConfig) -> Path:
    raw = (config.crawl.cookies_file or "").strip()
    if not raw:
        raise ValueError("cookies_file is not set for this site")
    path = Path(raw)
    if not path.is_absolute():
        path = config.project_root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Netscape HTTP Cookie File\n# Created by dark-crawler auth login\n",
            encoding="utf-8",
        )
    return path


async def perform_login(
    config: AppConfig,
    *,
    site_secrets: Path | None = None,
) -> LoginResult:
    secrets = load_forum_secrets(config.project_root, site_secrets=site_secrets)
    if secrets is None:
        return LoginResult(
            ok=False,
            message=(
                "No credentials configured. Open Auth and save username/password for this site."
            ),
            kind="login",
        )

    ensure_cookies_file(config)

    async with TorClient(config) as client:
        login_url, form_html = await _find_login_page(client, config.seed_url)
        wall = detect_auth_wall(None, form_html)
        if wall is not None and wall.kind == "captcha":
            return LoginResult(ok=False, message=wall.message, kind="captcha")

        action, payload = _extract_login_form(form_html, login_url, secrets)
        if action is None or payload is None:
            return LoginResult(
                ok=False,
                message=(
                    "Could not find a login form on the forum. "
                    "Solve captcha first, or paste cookies manually."
                ),
                kind="login",
            )

        logger.info("Submitting login form as user %s", secrets.username)
        response = await client.post(
            action,
            data=payload,
            headers={"Referer": login_url, "Origin": _origin(login_url)},
        )
        body = response.text
        wall = detect_auth_wall(None, body)
        if wall is not None and wall.kind == "captcha":
            return LoginResult(ok=False, message=wall.message, kind="captcha")

        if _login_looks_failed(body, secrets.username):
            return LoginResult(
                ok=False,
                message=(
                    "Login failed. Check username/password on the Auth page, "
                    "or solve captcha and try again."
                ),
                kind="login",
            )

        check = await client.get(config.seed_url)
        wall = detect_auth_wall(None, check.text)
        if wall is not None and wall.kind == "captcha":
            return LoginResult(ok=False, message=wall.message, kind="captcha")
        if wall is not None and wall.kind == "login":
            return LoginResult(
                ok=False,
                message="Login did not stick. Session cookies may still be missing.",
                kind="login",
            )

    return LoginResult(
        ok=True,
        message=f"Logged in as {secrets.username}. Cookies saved; crawl can continue.",
    )


async def _find_login_page(client: TorClient, seed_url: str) -> tuple[str, str]:
    seed = seed_url if seed_url.endswith("/") else seed_url + "/"
    candidates = [
        seed_url,
        urljoin(seed, "member.php?action=login"),
        urljoin(seed, "login.php"),
        urljoin(seed, "ucp.php?mode=login"),
        urljoin(seed, "index.php?login"),
    ]
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        response = await client.get(url)
        html = response.text
        if _has_login_form(html):
            return str(response.url), html
        if url == seed_url:
            for href in _login_links(html, seed_url):
                if href not in seen:
                    candidates.append(href)
    response = await client.get(seed_url)
    return str(response.url), response.text


def _has_login_form(html: str) -> bool:
    lower = html.lower()
    return 'type="password"' in lower or "type='password'" in lower


def _login_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for node in soup.select("a[href]"):
        href = (node.get("href") or "").strip()
        text = " ".join(node.stripped_strings).lower()
        hay = f"{href} {text}".lower()
        if "login" in hay or "sign in" in hay or "action=login" in hay:
            links.append(urljoin(base, href))
    return links


def _extract_login_form(
    html: str, page_url: str, secrets: ForumSecrets
) -> tuple[str | None, dict[str, str] | None]:
    soup = BeautifulSoup(html, "lxml")
    forms = soup.select("form")
    chosen = None
    for form in forms:
        if form.select_one('input[type="password"]'):
            chosen = form
            break
    if chosen is None:
        return None, None

    action = (chosen.get("action") or "").strip() or page_url
    action = urljoin(page_url, action)
    payload: dict[str, str] = {}
    for inp in chosen.select("input"):
        name = (inp.get("name") or "").strip()
        if not name:
            continue
        input_type = (inp.get("type") or "text").lower()
        if input_type in {"submit", "image", "button", "file"}:
            if input_type == "submit" and name:
                payload[name] = inp.get("value") or "Login"
            continue
        if input_type == "password":
            payload[name] = secrets.password
            continue
        value = inp.get("value") or ""
        lower_name = name.lower()
        if lower_name in {
            "username",
            "user",
            "email",
            "login",
            "user_name",
            "vb_login_username",
        }:
            payload[name] = secrets.username
        elif "user" in lower_name and "pass" not in lower_name:
            payload[name] = secrets.username
        else:
            payload[name] = value

    if secrets.username not in payload.values():
        for key in ("username", "user", "email", "login"):
            if key in payload:
                payload[key] = secrets.username
                break
        else:
            payload["username"] = secrets.username

    if "action" not in payload and "member.php" in action:
        payload["action"] = "do_login"

    return action, payload


def _login_looks_failed(html: str, username: str) -> bool:
    blob = html.lower()
    failures = (
        "invalid username",
        "invalid password",
        "login failed",
        "incorrect password",
        "wrong password",
        "authentication failed",
        "please correct the following",
        "error logging in",
    )
    if any(item in blob for item in failures):
        return True
    if 'type="password"' in blob and "logout" not in blob and username.lower() not in blob:
        return True
    return False


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"
