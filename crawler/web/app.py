"""FastAPI UI: crawl status + browsable HTML mirror."""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from crawler.auth_login import perform_login
from crawler.browser_session import (
    browser_ws_port,
    export_browser_cookies,
    session_status,
    start_browser_session,
    stop_browser_session,
    write_browser_target,
)
from crawler.config import AppConfig, load_config
from crawler.cookies_io import (
    cookie_names,
    domain_from_seed,
    parse_pasted_cookies,
    write_netscape,
)
from crawler.db import utc_now
from crawler.parser import normalize_url
from crawler.secrets import save_forum_secrets, secrets_status
from crawler.sites import (
    SiteRecord,
    add_site,
    delete_site,
    load_registry,
    migrate_legacy_layout,
    resolve_site,
    set_site_enabled,
    site_paths,
    site_summary,
)

WEB_DIR = Path(__file__).resolve().parent


def read_alert(database_path: Path) -> str:
    if not database_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(database_path))
        row = conn.execute(
            "SELECT value FROM crawler_state WHERE key = 'alert'"
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return ""
    if not row or not row[0]:
        return ""
    return str(row[0])


def read_alert_kind(database_path: Path) -> str:
    if not database_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(database_path))
        row = conn.execute(
            "SELECT value FROM crawler_state WHERE key = 'alert_kind'"
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return ""
    if not row or not row[0]:
        return ""
    return str(row[0])


def clear_stored_alert(database_path: Path) -> None:
    if not database_path.exists():
        return
    conn = sqlite3.connect(str(database_path))
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
    conn.commit()
    conn.close()


def set_stored_alert(database_path: Path, message: str, kind: str | None = None) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
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
        now = utc_now()
        conn.execute(
            """
            INSERT INTO crawler_state(key, value, updated_at)
            VALUES ('alert', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (message, now),
        )
        if kind:
            conn.execute(
                """
                INSERT INTO crawler_state(key, value, updated_at)
                VALUES ('alert_kind', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (kind, now),
            )
        else:
            conn.execute("DELETE FROM crawler_state WHERE key = 'alert_kind'")
        conn.commit()
    finally:
        conn.close()


def _template_context(request: Request) -> dict[str, Any]:
    try:
        root = _project_root()
        _ensure_registry()
        sites = load_registry(root)
        site_id = (
            request.query_params.get("site")
            or request.cookies.get("ui_site")
            or ""
        ).strip()
        current = resolve_site(root, site_id or None)
        alert = ""
        alert_kind = ""
        if current is not None:
            paths = site_paths(root, current.id)
            alert = read_alert(paths.database)
            alert_kind = read_alert_kind(paths.database)
        return {
            "alert": alert,
            "alert_kind": alert_kind,
            "nav_sites": sites,
            "current_site": current,
            "site_id": current.id if current else "",
        }
    except Exception:  # noqa: BLE001
        return {
            "alert": "",
            "alert_kind": "",
            "nav_sites": [],
            "current_site": None,
            "site_id": "",
        }


TEMPLATES = Jinja2Templates(
    directory=str(WEB_DIR / "templates"),
    context_processors=[_template_context],
)

# Whitelisted tables only. List view skips bulky HTML columns.
DB_TABLES: dict[str, dict[str, Any]] = {
    "pages": {
        "pk": "id",
        "columns": ["id", "status_code", "title", "url", "depth", "fetched_at", "error"],
        "search": ["title", "url", "error", "text_content"],
        "order": "id DESC",
    },
    "threads": {
        "pk": "id",
        "columns": ["id", "title", "author", "reply_count", "url", "forum_url"],
        "search": ["title", "author", "url"],
        "order": "id DESC",
    },
    "posts": {
        "pk": "id",
        "columns": ["id", "author", "posted_at", "thread_url", "post_url"],
        "search": ["author", "content_text", "thread_url"],
        "order": "id DESC",
    },
    "forums": {
        "pk": "id",
        "columns": ["id", "name", "url", "description"],
        "search": ["name", "description", "url"],
        "order": "id DESC",
    },
    "users": {
        "pk": "id",
        "columns": ["id", "username", "profile_url"],
        "search": ["username", "profile_url"],
        "order": "username COLLATE NOCASE",
    },
    "links": {
        "pk": "id",
        "columns": ["id", "from_url", "to_url"],
        "search": ["from_url", "to_url"],
        "order": "id DESC",
    },
    "queue": {
        "pk": "url",
        "columns": ["status", "depth", "url", "enqueued_at", "updated_at"],
        "search": ["url", "status"],
        "order": "depth ASC, enqueued_at ASC",
    },
}
LONG_FIELDS = {"html", "content_html", "text_content", "content_text", "raw_json"}
PAGE_SIZE = 40


def _project_root() -> Path:
    env = os.environ.get("DARK_CRAWLER_ROOT")
    if env:
        return Path(env).resolve()
    return WEB_DIR.parents[1]


def _config_path() -> Path:
    env = os.environ.get("DARK_CRAWLER_CONFIG")
    if env:
        return Path(env).resolve()
    return _project_root() / "config.yaml"


def _ensure_registry() -> AppConfig:
    config = load_config(_config_path())
    migrate_legacy_layout(config.project_root, config.seed_url)
    return config


def _site_from_request(
    request: Request,
    site: str | None = None,
) -> SiteRecord | None:
    _ensure_registry()
    site_id = (
        (site or "").strip()
        or request.query_params.get("site")
        or request.cookies.get("ui_site")
        or ""
    ).strip()
    return resolve_site(_project_root(), site_id or None)


def _scoped_config(site: SiteRecord) -> tuple[AppConfig, "SitePaths"]:
    from crawler.sites import SitePaths

    config = _ensure_registry()
    paths: SitePaths = site_paths(config.project_root, site.id)
    paths.ensure()
    scoped = config.with_site(
        seed_url=site.seed_url,
        database=paths.database,
        mirror_dir=paths.mirror,
        cookies_file=paths.cookies,
    )
    return scoped, paths


def get_paths(
    request: Request | None = None,
    site: str | None = None,
) -> tuple[Path, Path, Path, SiteRecord | None]:
    config = _ensure_registry()
    current = None
    if request is not None or site:
        if request is None:
            current = resolve_site(config.project_root, site)
        else:
            current = _site_from_request(request, site)
    else:
        current = resolve_site(config.project_root, None)
    if current is None:
        return config.project_root, Path("/nonexistent"), Path("/nonexistent"), None
    paths = site_paths(config.project_root, current.id)
    paths.ensure()
    return config.project_root, paths.database, paths.mirror, current


def _with_site(url: str, site_id: str) -> str:
    if not site_id:
        return url
    sep = "&" if "?" in url else "?"
    if "site=" in url:
        return url
    return f"{url}{sep}site={quote(site_id)}"


def _set_site_cookie(response: RedirectResponse, site_id: str) -> RedirectResponse:
    if site_id:
        response.set_cookie(
            "ui_site",
            site_id,
            httponly=False,
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )
    return response


def db_connect(database_path: Path) -> sqlite3.Connection:
    if not database_path.exists():
        raise HTTPException(status_code=404, detail=f"Database not found: {database_path}")
    conn = sqlite3.connect(str(database_path))
    conn.row_factory = sqlite3.Row
    return conn


def unit_active(unit: str) -> bool:
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


def start_crawler_unit(action: str = "start") -> bool:
    if action not in {"start", "restart"}:
        action = "start"
    commands = [
        ["sudo", "-n", "systemctl", action, "dark-crawler.service"],
        ["systemctl", action, "dark-crawler.service"],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def fetch_stats(conn: sqlite3.Connection) -> dict[str, int]:
    stats: dict[str, int] = {}
    for table in ("pages", "links", "forums", "threads", "posts", "users"):
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        stats[table] = int(row["c"] if row else 0)
    forbidden = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE status_code = 403"
    ).fetchone()
    stats["pages_403"] = int(forbidden["c"] if forbidden else 0)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM queue GROUP BY status"
    ).fetchall()
    for row in rows:
        stats[f"queue_{row['status']}"] = int(row["c"])
    return stats


def fetch_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT status, COUNT(*) AS count FROM queue GROUP BY status ORDER BY status"
    ).fetchall()


def fetch_recent_pages(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT url, title, status_code, mirror_path, fetched_at, error
        FROM pages
        ORDER BY fetched_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def clear_verification_and_reseed(database_path: Path, seed_url: str) -> None:
    """Drop the verification page so the next crawl fetches the real forum."""
    if not database_path.exists():
        return
    seed = normalize_url(seed_url, seed_url) or seed_url
    conn = sqlite3.connect(str(database_path))
    try:
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


def looks_like_captcha(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT title, html FROM pages
        ORDER BY fetched_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return False
    blob = f"{row['title'] or ''}\n{row['html'] or ''}".lower()
    hints = ("verify humanity", "altcha", "captcha", "are you human", "cf-challenge")
    return any(h in blob for h in hints)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Optional shared-secret gate via ?token= or X-UI-Token header."""

    async def dispatch(self, request: Request, call_next):
        expected = os.environ.get("WEB_UI_TOKEN", "").strip()
        if not expected:
            return await call_next(request)
        if request.url.path in {"/healthz"}:
            return await call_next(request)
        provided = request.headers.get("x-ui-token") or request.query_params.get("token")
        cookie = request.cookies.get("ui_token")
        if provided == expected or cookie == expected:
            response = await call_next(request)
            if provided == expected and cookie != expected:
                response.set_cookie(
                    "ui_token",
                    expected,
                    httponly=True,
                    samesite="lax",
                    max_age=60 * 60 * 24 * 30,
                )
            return response
        return HTMLResponse(
            "<h1>Unauthorized</h1><p>Pass ?token=... once to unlock this UI.</p>",
            status_code=401,
        )


def create_app() -> FastAPI:
    app = FastAPI(title="Dark Crawler UI", docs_url=None, redoc_url=None)
    app.add_middleware(TokenAuthMiddleware)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        root, database_path, _mirror, site = get_paths(request)
        alert_kind = ""
        if database_path.exists():
            conn = db_connect(database_path)
            try:
                stats = fetch_stats(conn)
                queue_rows = fetch_queue(conn)
                recent_pages = fetch_recent_pages(conn)
                captcha_hint = looks_like_captcha(conn)
            finally:
                conn.close()
            alert_kind = read_alert_kind(database_path)
        else:
            stats = {}
            queue_rows = []
            recent_pages = []
            captcha_hint = False

        preferred = [
            "pages",
            "pages_403",
            "links",
            "threads",
            "posts",
            "users",
            "forums",
            "queue_pending",
            "queue_done",
            "queue_failed",
            "queue_in_progress",
        ]
        cards = [(key, stats.get(key, 0)) for key in preferred]
        for key, value in sorted(stats.items()):
            if key not in preferred:
                cards.append((key, value))

        return TEMPLATES.TemplateResponse(
            request,
            "status.html",
            {
                "active": "status",
                "seed_url": site.seed_url if site else "",
                "database_path": str(database_path) if site else "",
                "stats_cards": cards,
                "queue_rows": queue_rows,
                "recent_pages": recent_pages,
                "crawler_active": unit_active("dark-crawler.service"),
                "tor_active": unit_active("dark-crawler-tor.service"),
                "captcha_hint": captcha_hint,
                "alert_kind": alert_kind,
                "root": str(root),
            },
        )

    @app.get("/sites", response_class=HTMLResponse)
    def sites_page(
        request: Request,
        flash: str = "",
        ok: int = 0,
        error: str = "",
    ) -> HTMLResponse:
        root = _ensure_registry().project_root
        rows = [site_summary(root, site) for site in load_registry(root)]
        return TEMPLATES.TemplateResponse(
            request,
            "sites.html",
            {
                "active": "sites",
                "sites": rows,
                "flash": flash,
                "flash_ok": bool(ok),
                "error": error,
            },
        )

    @app.post("/sites/add")
    def sites_add(
        seed_url: str = Form(default=""),
        label: str = Form(default=""),
        enabled: str = Form(default="1"),
    ) -> RedirectResponse:
        try:
            record = add_site(
                _project_root(),
                seed_url,
                label=label,
                enabled=enabled == "1",
            )
        except ValueError as exc:
            return RedirectResponse(
                url=f"/sites?error={quote(str(exc))}",
                status_code=303,
            )
        response = RedirectResponse(
            url=f"/sites?ok=1&flash={quote('Added ' + record.host)}",
            status_code=303,
        )
        return _set_site_cookie(response, record.id)

    @app.post("/sites/toggle")
    def sites_toggle(
        site_id: str = Form(default=""),
        enabled: str = Form(default="0"),
    ) -> RedirectResponse:
        try:
            record = set_site_enabled(_project_root(), site_id, enabled == "1")
        except KeyError as exc:
            return RedirectResponse(
                url=f"/sites?error={quote(str(exc))}",
                status_code=303,
            )
        state = "enabled" if record.enabled else "disabled"
        return RedirectResponse(
            url=f"/sites?ok=1&flash={quote(record.host + ' ' + state)}",
            status_code=303,
        )

    @app.post("/sites/delete")
    def sites_delete(site_id: str = Form(default="")) -> RedirectResponse:
        try:
            delete_site(_project_root(), site_id, remove_data=True)
        except KeyError as exc:
            return RedirectResponse(
                url=f"/sites?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/sites?ok=1&flash={quote('Deleted site ' + site_id)}",
            status_code=303,
        )

    @app.post("/sites/select")
    def sites_select(site_id: str = Form(default="")) -> RedirectResponse:
        site = resolve_site(_project_root(), site_id)
        if site is None:
            return RedirectResponse(
                url=f"/sites?error={quote('Unknown site')}",
                status_code=303,
            )
        response = RedirectResponse(url=f"/?site={quote(site.id)}", status_code=303)
        return _set_site_cookie(response, site.id)

    @app.get("/pages", response_class=HTMLResponse)
    def pages_list(
        request: Request,
        q: str = Query(default=""),
        site: str = Query(default=""),
    ) -> HTMLResponse:
        _root, database_path, _mirror, _current = get_paths(request, site)
        pages: list[Any] = []
        if _current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        if database_path.exists():
            conn = db_connect(database_path)
            try:
                if q.strip():
                    like = f"%{q.strip()}%"
                    pages = conn.execute(
                        """
                        SELECT url, title, status_code, mirror_path, fetched_at
                        FROM pages
                        WHERE COALESCE(title,'') LIKE ? OR url LIKE ?
                        ORDER BY fetched_at DESC
                        LIMIT 200
                        """,
                        (like, like),
                    ).fetchall()
                else:
                    pages = fetch_recent_pages(conn, limit=200)
            finally:
                conn.close()
        return TEMPLATES.TemplateResponse(
            request,
            "pages.html",
            {"active": "pages", "pages": pages, "q": q},
        )

    @app.get("/api/stats")
    def api_stats(request: Request, site: str = Query(default="")) -> dict[str, Any]:
        _root, database_path, mirror_path, current = get_paths(request, site)
        payload: dict[str, Any] = {
            "crawler_active": unit_active("dark-crawler.service"),
            "tor_active": unit_active("dark-crawler-tor.service"),
            "database": str(database_path) if current else "",
            "mirror": str(mirror_path) if current else "",
            "site_id": current.id if current else "",
            "sites": [
                site_summary(_project_root(), item)
                for item in load_registry(_project_root())
            ],
            "stats": {},
        }
        if current and database_path.exists():
            conn = db_connect(database_path)
            try:
                payload["stats"] = fetch_stats(conn)
                payload["captcha_hint"] = looks_like_captcha(conn)
                payload["alert"] = read_alert(database_path)
                payload["alert_kind"] = read_alert_kind(database_path)
            finally:
                conn.close()
        payload["browser_session"] = session_status()
        return payload

    def _safe_mirror_target(request: Request, rel: str) -> Path:
        _root, _db, mirror_path, current = get_paths(request)
        if current is None:
            raise HTTPException(status_code=404, detail="No site selected")
        mirror_path = mirror_path.resolve()
        candidate = (mirror_path / rel).resolve()
        if mirror_path not in candidate.parents and candidate != mirror_path:
            raise HTTPException(status_code=400, detail="Invalid mirror path")
        return candidate

    @app.get("/mirror/", response_class=HTMLResponse)
    @app.get("/mirror/{rel_path:path}", response_class=HTMLResponse)
    def mirror_browser(request: Request, rel_path: str = "") -> HTMLResponse:
        target = _safe_mirror_target(request, rel_path)
        crumbs = []
        parts = [p for p in rel_path.split("/") if p]
        acc: list[str] = []
        for part in parts:
            acc.append(part)
            crumbs.append(
                {
                    "name": part,
                    "href": "/mirror/" + quote("/".join(acc)) if part != parts[-1] or target.is_dir() else "",
                }
            )
        if target.exists() and target.is_file():
            return TEMPLATES.TemplateResponse(
                request,
                "mirror_browser.html",
                {
                    "active": "mirror",
                    "is_file": True,
                    "rel_path": rel_path,
                    "crumbs": crumbs,
                    "entries": [],
                },
            )

        if not target.exists():
            if rel_path:
                raise HTTPException(status_code=404, detail="Path not found")
            target.mkdir(parents=True, exist_ok=True)

        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            child_rel = f"{rel_path.rstrip('/')}/{child.name}".lstrip("/")
            detail = "dir" if child.is_dir() else f"{child.stat().st_size} B"
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "href": "/mirror/" + quote(child_rel),
                    "detail": detail,
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "mirror_browser.html",
            {
                "active": "mirror",
                "is_file": False,
                "rel_path": rel_path,
                "crumbs": crumbs,
                "entries": entries,
            },
        )

    @app.get("/mirror-raw/{rel_path:path}")
    def mirror_raw(request: Request, rel_path: str) -> FileResponse:
        target = _safe_mirror_target(request, rel_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        media = "text/html" if target.suffix.lower() in {".html", ".htm"} else None
        return FileResponse(target, media_type=media)

    def _db_counts_from(self_conn: sqlite3.Connection) -> list[tuple[str, int]]:
        counts: list[tuple[str, int]] = []
        for name in DB_TABLES:
            row = self_conn.execute(f"SELECT COUNT(*) AS c FROM {name}").fetchone()
            counts.append((name, int(row["c"] if row else 0)))
        return counts

    @app.get("/db", response_class=HTMLResponse)
    def database_page(
        request: Request,
        table: str = Query(default="pages"),
        q: str = Query(default=""),
        page: int = Query(default=1, ge=1),
        pk: str = Query(default=""),
    ) -> HTMLResponse:
        if table not in DB_TABLES:
            raise HTTPException(status_code=404, detail="Unknown table")
        spec = DB_TABLES[table]
        _root, database_path, _mirror, _current = get_paths(request)
        if not database_path.exists():
            return TEMPLATES.TemplateResponse(
                request,
                "db.html",
                {
                    "active": "db",
                    "mode": "list",
                    "tables": [(name, 0) for name in DB_TABLES],
                    "table": table,
                    "columns": spec["columns"],
                    "pk": spec["pk"],
                    "rows": [],
                    "total": 0,
                    "page": 1,
                    "pages": 1,
                    "q": q,
                },
            )

        conn = db_connect(database_path)
        try:
            tables = _db_counts_from(conn)
            if pk:
                row = conn.execute(
                    f"SELECT * FROM {table} WHERE {spec['pk']} = ?",
                    (pk,),
                ).fetchone()
                detail = None
                if row is not None:
                    detail = {}
                    for key in row.keys():
                        value = row[key]
                        if isinstance(value, str) and key in LONG_FIELDS and len(value) > 12000:
                            value = value[:12000] + "\n… truncated …"
                        detail[key] = value
                return TEMPLATES.TemplateResponse(
                    request,
                    "db.html",
                    {
                        "active": "db",
                        "mode": "detail",
                        "tables": tables,
                        "table": table,
                        "pk_value": pk,
                        "row": detail,
                        "q": q,
                        "page": page,
                    },
                )

            where = ""
            params: list[Any] = []
            query = q.strip()
            if query:
                clauses = [f"CAST({col} AS TEXT) LIKE ?" for col in spec["search"]]
                where = "WHERE " + " OR ".join(clauses)
                params = [f"%{query}%"] * len(spec["search"])
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} {where}",
                    params,
                ).fetchone()["c"]
            )
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages)
            offset = (page - 1) * PAGE_SIZE
            columns = spec["columns"]
            selected = ", ".join(columns)
            fetched = conn.execute(
                f"SELECT {selected} FROM {table} {where} ORDER BY {spec['order']} LIMIT ? OFFSET ?",
                [*params, PAGE_SIZE, offset],
            ).fetchall()
            rows = [{col: item[col] for col in columns} for item in fetched]
        finally:
            conn.close()

        return TEMPLATES.TemplateResponse(
            request,
            "db.html",
            {
                "active": "db",
                "mode": "list",
                "tables": tables,
                "table": table,
                "columns": spec["columns"],
                "pk": spec["pk"],
                "rows": rows,
                "total": total,
                "page": page,
                "pages": pages,
                "q": query,
            },
        )

    @app.get("/auth", response_class=HTMLResponse)
    def auth_page(
        request: Request,
        flash: str = "",
        ok: int = 0,
        error: str = "",
        site: str = Query(default=""),
    ) -> HTMLResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        scoped, paths = _scoped_config(current)
        return TEMPLATES.TemplateResponse(
            request,
            "auth.html",
            {
                "active": "auth",
                "session": session_status(),
                "secrets": secrets_status(
                    scoped.project_root,
                    site_secrets=paths.secrets,
                ),
                "cookie_path": str(paths.cookies),
                "names": cookie_names(paths.cookies),
                "flash": flash,
                "flash_ok": bool(ok),
                "error": error,
            },
        )

    @app.post("/auth/session/start")
    def auth_session_start(
        request: Request,
        site: str = Form(default=""),
    ) -> RedirectResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        write_browser_target(
            _project_root(),
            site_id=current.id,
            seed_url=current.seed_url,
        )
        ok, message = start_browser_session()
        target = _with_site(
            f"/auth?ok=1&flash={quote(message)}" if ok else f"/auth?error={quote(message)}",
            current.id,
        )
        return _set_site_cookie(RedirectResponse(url=target, status_code=303), current.id)

    @app.post("/auth/session/stop")
    def auth_session_stop(
        request: Request,
        site: str = Form(default=""),
    ) -> RedirectResponse:
        current = _site_from_request(request, site)
        site_id = current.id if current else ""
        ok, message = stop_browser_session()
        target = _with_site(
            f"/auth?ok=1&flash={quote(message)}" if ok else f"/auth?error={quote(message)}",
            site_id,
        )
        return RedirectResponse(url=target, status_code=303)

    @app.post("/auth/session/save")
    def auth_session_save(
        request: Request,
        site: str = Form(default=""),
    ) -> RedirectResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        scoped, paths = _scoped_config(current)
        ok, message, count = export_browser_cookies(
            scoped,
            site_id=current.id,
            cookies_path=paths.cookies,
        )
        if not ok:
            return RedirectResponse(
                url=_with_site(f"/auth?error={quote(message)}", current.id),
                status_code=303,
            )
        clear_stored_alert(paths.database)
        clear_verification_and_reseed(paths.database, current.seed_url)
        restarted = start_crawler_unit("restart")
        detail = message
        if restarted:
            detail += " Crawl restarted."
        elif count:
            detail += " Start crawl manually if needed."
        return RedirectResponse(
            url=_with_site(f"/auth?ok=1&flash={quote(detail)}", current.id),
            status_code=303,
        )

    @app.post("/auth/credentials")
    def auth_credentials_save(
        request: Request,
        site: str = Form(default=""),
        username: str = Form(default=""),
        password: str = Form(default=""),
    ) -> RedirectResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        scoped, paths = _scoped_config(current)
        username = username.strip()
        if not password:
            # Keep previous password when the field is left blank.
            from crawler.secrets import load_forum_secrets

            loaded = load_forum_secrets(scoped.project_root, site_secrets=paths.secrets)
            if loaded is None:
                return RedirectResponse(
                    url=_with_site(
                        "/auth?error=Password+is+required+the+first+time",
                        current.id,
                    ),
                    status_code=303,
                )
            password = loaded.password
            if not username:
                username = loaded.username
        try:
            saved = save_forum_secrets(
                paths.secrets,
                username=username,
                password=password,
            )
        except ValueError as exc:
            return RedirectResponse(
                url=_with_site(f"/auth?error={quote(str(exc))}", current.id),
                status_code=303,
            )
        return _set_site_cookie(
            RedirectResponse(
                url=_with_site(
                    f"/auth?ok=1&flash={quote('Saved credentials for ' + saved.username)}",
                    current.id,
                ),
                status_code=303,
            ),
            current.id,
        )

    @app.post("/auth/login")
    async def auth_login(
        request: Request,
        site: str = Form(default=""),
    ) -> RedirectResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        scoped, paths = _scoped_config(current)
        result = await perform_login(scoped, site_secrets=paths.secrets)
        if not result.ok:
            if result.kind:
                set_stored_alert(paths.database, result.message, kind=result.kind)
            return RedirectResponse(
                url=_with_site(f"/auth?error={quote(result.message)}", current.id),
                status_code=303,
            )
        clear_stored_alert(paths.database)
        clear_verification_and_reseed(paths.database, current.seed_url)
        restarted = start_crawler_unit("restart")
        message = result.message
        if restarted:
            message += " Crawl restarted."
        return RedirectResponse(
            url=_with_site(f"/auth?ok=1&flash={quote(message)}", current.id),
            status_code=303,
        )

    def _novnc_upstream() -> str:
        return f"http://127.0.0.1:{browser_ws_port()}"

    @app.api_route(
        "/auth/session",
        methods=["GET", "HEAD"],
    )
    @app.api_route(
        "/auth/session/{path:path}",
        methods=["GET", "HEAD", "POST", "OPTIONS"],
    )
    async def auth_session_proxy(request: Request, path: str = "") -> Response:
        if path == "websockify":
            raise HTTPException(
                status_code=400,
                detail="Use the WebSocket endpoint for websockify",
            )
        upstream = _novnc_upstream().rstrip("/") + "/"
        target = urljoin(upstream, path)
        if request.url.query:
            target = f"{target}?{request.url.query}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                proxied = await client.request(
                    request.method,
                    target,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() not in {"host", "cookie", "content-length"}
                    },
                    content=await request.body(),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Captcha browser session is not reachable. "
                    "Press Open captcha session first."
                ),
            ) from exc
        excluded = {"content-encoding", "transfer-encoding", "content-length", "connection"}
        headers = {
            key: value
            for key, value in proxied.headers.items()
            if key.lower() not in excluded
        }
        return Response(
            content=proxied.content,
            status_code=proxied.status_code,
            headers=headers,
            media_type=proxied.headers.get("content-type"),
        )

    @app.websocket("/auth/session/websockify")
    async def auth_session_ws(websocket: WebSocket) -> None:
        expected = os.environ.get("WEB_UI_TOKEN", "").strip()
        if expected:
            provided = (
                websocket.headers.get("x-ui-token")
                or websocket.query_params.get("token")
                or websocket.cookies.get("ui_token")
            )
            if provided != expected:
                await websocket.close(code=1008)
                return
        await websocket.accept()
        upstream_url = f"ws://127.0.0.1:{browser_ws_port()}/"
        try:
            import websockets
        except ImportError:
            await websocket.close(code=1011)
            return

        try:
            async with websockets.connect(
                upstream_url,
                open_timeout=10,
                max_size=8 * 1024 * 1024,
            ) as upstream:

                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        data = message.get("bytes")
                        if data is not None:
                            await upstream.send(data)
                            continue
                        text = message.get("text")
                        if text is not None:
                            await upstream.send(text)

                async def upstream_to_client() -> None:
                    while True:
                        data = await upstream.recv()
                        if isinstance(data, bytes):
                            await websocket.send_bytes(data)
                        else:
                            await websocket.send_text(data)

                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(client_to_upstream()),
                        asyncio.create_task(upstream_to_client()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    if exc and not isinstance(
                        exc, (WebSocketDisconnect, asyncio.CancelledError)
                    ):
                        raise exc
        except Exception:  # noqa: BLE001
            try:
                await websocket.close(code=1011)
            except Exception:  # noqa: BLE001
                pass

    @app.get("/cookies", response_class=HTMLResponse)
    def cookies_page(
        request: Request,
        saved: int = 0,
        count: int = 0,
        restarted: int = 0,
        error: str = "",
        site: str = Query(default=""),
    ) -> HTMLResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        _scoped, paths = _scoped_config(current)
        return TEMPLATES.TemplateResponse(
            request,
            "cookies.html",
            {
                "active": "cookies",
                "cookie_path": str(paths.cookies),
                "names": cookie_names(paths.cookies),
                "saved": bool(saved),
                "saved_count": count,
                "restarted": bool(restarted),
                "error": error,
            },
        )

    @app.post("/cookies")
    def cookies_save(
        request: Request,
        cookies: str = Form(default=""),
        restart: str = Form(default=""),
        site: str = Form(default=""),
    ) -> RedirectResponse:
        current = _site_from_request(request, site)
        if current is None:
            return RedirectResponse(url="/sites?error=Add+a+site+first", status_code=303)
        _scoped, paths = _scoped_config(current)
        domain = domain_from_seed(current.seed_url)
        parsed = parse_pasted_cookies(cookies, domain)
        if not parsed:
            return RedirectResponse(
                url=_with_site(
                    "/cookies?error=No+cookies+found.+Paste+a+Cookie+header,+Netscape+file,+or+JSON.",
                    current.id,
                ),
                status_code=303,
            )

        write_netscape(paths.cookies, parsed)
        clear_stored_alert(paths.database)

        restarted = False
        if restart == "1":
            clear_verification_and_reseed(paths.database, current.seed_url)
            restarted = start_crawler_unit("restart")

        return RedirectResponse(
            url=_with_site(
                f"/cookies?saved=1&count={len(parsed)}&restarted={1 if restarted else 0}",
                current.id,
            ),
            status_code=303,
        )

    @app.post("/actions/start-crawl")
    def start_crawl(
        request: Request,
        site: str = Form(default=""),
    ) -> RedirectResponse:
        start_crawler_unit()
        current = _site_from_request(request, site)
        url = _with_site("/", current.id if current else "")
        return RedirectResponse(url=url, status_code=303)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("WEB_UI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_UI_PORT", "19810"))
    if not os.environ.get("WEB_UI_TOKEN"):
        # Generate once per process if unset; prefer setting via systemd.
        os.environ["WEB_UI_TOKEN"] = secrets.token_urlsafe(18)
        print(f"WEB_UI_TOKEN={os.environ['WEB_UI_TOKEN']}")
    uvicorn.run(
        "crawler.web.app:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
