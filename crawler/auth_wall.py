"""Detect captcha / login / expired-session walls in forum HTML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AuthKind = Literal["captcha", "login", "forbidden_session"]


@dataclass(frozen=True)
class AuthWall:
    kind: AuthKind
    message: str


CAPTCHA_HINTS = (
    "verify humanity",
    "altcha-widget",
    "/_pf_verify/verify",
    "are you human",
    "cf-challenge",
    "hcaptcha",
    "g-recaptcha",
    "solve the captcha",
    "complete the captcha",
    "captcha-widget",
)

LOGIN_HINTS = (
    "please log in",
    "please login",
    "you must be logged in",
    "you need to be logged in",
    "member.php?action=login",
    "do you already have an account",
    "not logged in",
    "login to your account",
    "sign in to continue",
)


def detect_auth_wall(title: str | None, html: str) -> AuthWall | None:
    """Return a typed wall if the page blocks crawling."""
    blob = f"{title or ''}\n{html or ''}".lower()
    if not blob.strip():
        return None

    if any(hint in blob for hint in CAPTCHA_HINTS):
        # Prefer captcha when both markers appear — login cannot succeed yet.
        return AuthWall(
            kind="captcha",
            message=(
                "Captcha wall: the forum is asking for human verification. "
                "Crawl stopped. Open Auth, finish captcha in the browser session, "
                "then Save cookies."
            ),
        )

    if _looks_like_login_wall(blob, html or ""):
        return AuthWall(
            kind="login",
            message=(
                "Login required: the forum is showing a login wall. "
                "Crawl stopped. Open Auth, log in in the browser session, "
                "then Save cookies."
            ),
        )
    return None


def forbidden_session_wall(reason: str = "readable page became forbidden") -> AuthWall:
    return AuthWall(
        kind="forbidden_session",
        message=(
            f"Session expired: {reason}. "
            "Crawl stopped. Open Auth, refresh captcha/login in the browser "
            "session, then Save cookies."
        ),
    )


def _looks_like_login_wall(blob: str, html: str) -> bool:
    if any(hint in blob for hint in LOGIN_HINTS):
        return True
    lower_html = html.lower()
    has_password = 'type="password"' in lower_html or "type='password'" in lower_html
    has_login_action = (
        "action=login" in lower_html
        or 'name="login"' in lower_html
        or "member.php" in lower_html and "login" in lower_html
    )
    if has_password and has_login_action:
        return True
    if has_password and ("username" in lower_html or "user name" in blob):
        # Standalone login page without extra prose.
        if "forum" in blob or "member" in blob or "log in" in blob or "login" in blob:
            return True
    return False
