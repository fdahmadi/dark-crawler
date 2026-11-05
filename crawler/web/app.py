"""FastAPI UI: crawl status + browsable HTML mirror."""

from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from crawler.config import load_config
from crawler.cookies_io import (
    cookie_names,
    default_cookie_path,
    domain_from_seed,
    parse_pasted_cookies,
    point_config_at_cookies,
    write_netscape,
)
from crawler.db import utc_now
from crawler.parser import normalize_url

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
    conn.execute("DELETE FROM crawler_state WHERE key = 'alert'")
    conn.commit()
    conn.close()


def _template_context(request: Request) -> dict[str, Any]:
    try:
        _root, database_path, _mirror = get_paths()
    except Exception:  # noqa: BLE001
        return {"alert": ""}
    return {"alert": read_alert(database_path)}


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


def get_paths() -> tuple[Path, Path, Path]:
    config = load_config(_config_path())
    return config.project_root, config.database_path, config.mirror_path


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
        root, database_path, _mirror = get_paths()
        config = load_config(_config_path())
        if database_path.exists():
            conn = db_connect(database_path)
            try:
                stats = fetch_stats(conn)
                queue_rows = fetch_queue(conn)
                recent_pages = fetch_recent_pages(conn)
                captcha_hint = looks_like_captcha(conn)
            finally:
                conn.close()
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
                "seed_url": config.seed_url,
                "database_path": str(database_path),
                "stats_cards": cards,
                "queue_rows": queue_rows,
                "recent_pages": recent_pages,
                "crawler_active": unit_active("dark-crawler.service"),
                "tor_active": unit_active("dark-crawler-tor.service"),
                "captcha_hint": captcha_hint,
                "root": str(root),
            },
        )

    @app.get("/pages", response_class=HTMLResponse)
    def pages_list(
        request: Request,
        q: str = Query(default=""),
    ) -> HTMLResponse:
        _root, database_path, _mirror = get_paths()
        pages: list[Any] = []
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
    def api_stats() -> dict[str, Any]:
        _root, database_path, mirror_path = get_paths()
        payload: dict[str, Any] = {
            "crawler_active": unit_active("dark-crawler.service"),
            "tor_active": unit_active("dark-crawler-tor.service"),
            "database": str(database_path),
            "mirror": str(mirror_path),
            "stats": {},
        }
        if database_path.exists():
            conn = db_connect(database_path)
            try:
                payload["stats"] = fetch_stats(conn)
                payload["captcha_hint"] = looks_like_captcha(conn)
            finally:
                conn.close()
        return payload

    def _safe_mirror_target(rel: str) -> Path:
        _root, _db, mirror_path = get_paths()
        mirror_path = mirror_path.resolve()
        candidate = (mirror_path / rel).resolve()
        if mirror_path not in candidate.parents and candidate != mirror_path:
            raise HTTPException(status_code=400, detail="Invalid mirror path")
        return candidate

    @app.get("/mirror/", response_class=HTMLResponse)
    @app.get("/mirror/{rel_path:path}", response_class=HTMLResponse)
    def mirror_browser(request: Request, rel_path: str = "") -> HTMLResponse:
        target = _safe_mirror_target(rel_path)
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
    def mirror_raw(rel_path: str) -> FileResponse:
        target = _safe_mirror_target(rel_path)
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
        _root, database_path, _mirror = get_paths()
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

    @app.get("/cookies", response_class=HTMLResponse)
    def cookies_page(
        request: Request,
        saved: int = 0,
        count: int = 0,
        restarted: int = 0,
        error: str = "",
    ) -> HTMLResponse:
        config = load_config(_config_path())
        path = default_cookie_path(config.project_root)
        return TEMPLATES.TemplateResponse(
            request,
            "cookies.html",
            {
                "active": "cookies",
                "cookie_path": str(path),
                "names": cookie_names(path),
                "saved": bool(saved),
                "saved_count": count,
                "restarted": bool(restarted),
                "error": error,
            },
        )

    @app.post("/cookies")
    def cookies_save(
        cookies: str = Form(default=""),
        restart: str = Form(default=""),
    ) -> RedirectResponse:
        config = load_config(_config_path())
        domain = domain_from_seed(config.seed_url)
        parsed = parse_pasted_cookies(cookies, domain)
        if not parsed:
            return RedirectResponse(
                url="/cookies?error=No+cookies+found.+Paste+a+Cookie+header,+Netscape+file,+or+JSON.",
                status_code=303,
            )

        path = default_cookie_path(config.project_root)
        write_netscape(path, parsed)
        point_config_at_cookies(_config_path(), "data/cookies.txt")
        clear_stored_alert(config.database_path)

        restarted = False
        if restart == "1":
            clear_verification_and_reseed(config.database_path, config.seed_url)
            restarted = start_crawler_unit("restart")

        return RedirectResponse(
            url=f"/cookies?saved=1&count={len(parsed)}&restarted={1 if restarted else 0}",
            status_code=303,
        )

    @app.post("/actions/start-crawl")
    def start_crawl() -> RedirectResponse:
        start_crawler_unit()
        return RedirectResponse(url="/", status_code=303)

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
