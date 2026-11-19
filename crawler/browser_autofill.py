"""Install a temporary browser extension that autofills saved forum credentials."""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from .secrets import load_forum_secrets

logger = logging.getLogger(__name__)

EXTENSION_ID = "dc-autofill@dark-crawler"
EXTENSION_DIR_NAME = "dc-autofill-ext"


def install_login_autofill(
    profile: Path,
    *,
    username: str,
    password: str,
) -> Path:
    """Write an unpacked + Firefox XPI autofill extension into the browser profile."""
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise ValueError("username and password are required for autofill")

    ext_dir = profile / EXTENSION_DIR_NAME
    ext_dir.mkdir(parents=True, exist_ok=True)
    # MV3 works with Chromium --load-extension; Firefox XPI accepts it on recent ESR.
    (ext_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Dark Crawler Login Autofill",
                "version": "1.0.1",
                "description": "Autofills saved forum credentials in the auth browser session.",
                "content_scripts": [
                    {
                        "matches": ["http://*/*", "https://*/*"],
                        "js": ["content.js"],
                        "run_at": "document_idle",
                        "all_frames": True,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (ext_dir / "content.js").write_text(
        _content_script(username, password),
        encoding="utf-8",
    )

    extensions_dir = profile / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    xpi_path = extensions_dir / f"{EXTENSION_ID}.xpi"
    with zipfile.ZipFile(xpi_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(ext_dir / "manifest.json", arcname="manifest.json")
        archive.write(ext_dir / "content.js", arcname="content.js")

    logger.info("Installed login autofill extension into %s", profile)
    return ext_dir


def install_autofill_for_site(
    project_root: Path,
    *,
    site_id: str,
    profile: Path,
) -> Path | None:
    """Load per-site secrets and install autofill. Returns extension dir or None."""
    secrets_path = project_root / "data" / "sites" / site_id / "secrets.yaml"
    loaded = load_forum_secrets(project_root, site_secrets=secrets_path)
    if loaded is None:
        _remove_autofill(profile)
        return None
    return install_login_autofill(
        profile,
        username=loaded.username,
        password=loaded.password,
    )


def _remove_autofill(profile: Path) -> None:
    ext_dir = profile / EXTENSION_DIR_NAME
    if ext_dir.exists():
        for child in ext_dir.glob("*"):
            child.unlink(missing_ok=True)
        try:
            ext_dir.rmdir()
        except OSError:
            pass
    xpi = profile / "extensions" / f"{EXTENSION_ID}.xpi"
    xpi.unlink(missing_ok=True)


def _content_script(username: str, password: str) -> str:
    user_js = json.dumps(username)
    pass_js = json.dumps(password)
    # Keep re-applying values: SPA/login navigations often clear inputs after first paint.
    return f"""(() => {{
  const USER = {user_js};
  const PASS = {pass_js};

  function setValue(el, value) {{
    if (!el) return;
    if (el.value === value) return;
    const proto = window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
    el.dispatchEvent(new KeyboardEvent("keyup", {{ bubbles: true }}));
  }}

  function looksLikeUserField(el) {{
    if (!el || el.disabled || el.readOnly) return false;
    const type = (el.type || "text").toLowerCase();
    if (type === "password" || type === "hidden") return false;
    if (type === "submit" || type === "button" || type === "checkbox" || type === "radio") return false;
    const name = (
      (el.name || "") + " " +
      (el.id || "") + " " +
      (el.autocomplete || "") + " " +
      (el.placeholder || "") + " " +
      (el.getAttribute("aria-label") || "")
    ).toLowerCase();
    if (name.includes("search") || name.includes("query") || name.includes("captcha")) return false;
    if (type === "email") return true;
    if (
      name.includes("user") ||
      name.includes("login") ||
      name.includes("email") ||
      name.includes("member") ||
      name.includes("quick_username")
    ) return true;
    if (type === "text" || type === "") return true;
    return false;
  }}

  function fill() {{
    const passwords = document.querySelectorAll('input[type="password"]');
    for (const pass of passwords) {{
      if (pass.disabled || pass.readOnly) continue;
      const form = pass.form || pass.closest("form") || document;
      const candidates = Array.from(form.querySelectorAll("input")).filter(looksLikeUserField);
      const user = candidates.find((el) => el !== pass) || null;
      if (user) setValue(user, USER);
      setValue(pass, PASS);
    }}
  }}

  fill();
  document.addEventListener("DOMContentLoaded", fill);
  window.addEventListener("load", fill);
  try {{
    new MutationObserver(() => fill()).observe(document.documentElement, {{
      childList: true,
      subtree: true,
    }});
  }} catch (_err) {{}}
  setInterval(fill, 800);
}})();
"""
