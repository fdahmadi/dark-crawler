"""Write a browsable local HTML mirror with rewritten same-host links."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup


class MirrorWriter:
    def __init__(self, root: Path, seed_host: str) -> None:
        self.root = root
        self.seed_host = seed_host.lower()
        self.root.mkdir(parents=True, exist_ok=True)

    def url_to_path(self, url: str) -> Path:
        parsed = urlparse(url)
        host = parsed.netloc.lower() or self.seed_host
        path = parsed.path or "/"

        if path.endswith("/"):
            path = path + "index.html"
        elif not Path(path).suffix:
            path = path.rstrip("/") + "/index.html"

        # Keep query identity without breaking filesystem paths.
        if parsed.query:
            digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
            suffix = Path(path).suffix or ".html"
            path = str(Path(path).with_suffix("")) + f"__q_{digest}{suffix}"

        relative = Path(host) / path.lstrip("/")
        return self.root / relative

    def save(self, url: str, html: str, known_urls: set[str] | None = None) -> str:
        target = self.url_to_path(url)
        target.parent.mkdir(parents=True, exist_ok=True)

        rewritten = self._rewrite_links(url, html, known_urls or set())
        target.write_text(rewritten, encoding="utf-8")
        return str(target.relative_to(self.root))

    def _rewrite_links(
        self, page_url: str, html: str, known_urls: set[str]
    ) -> str:
        soup = BeautifulSoup(html, "lxml")
        page_path = self.url_to_path(page_url)

        for tag in soup.find_all(["a", "link", "img", "script", "source"]):
            attr = "href" if tag.name in {"a", "link"} else "src"
            value = tag.get(attr)
            if not value:
                continue

            from .parser import normalize_url

            absolute = normalize_url(page_url, value)
            if not absolute:
                continue
            parsed = urlparse(absolute)
            if parsed.netloc.lower() != self.seed_host:
                continue

            local_path = self.url_to_path(absolute)
            try:
                relative = Path(
                    __import__("os").path.relpath(local_path, page_path.parent)
                ).as_posix()
            except ValueError:
                relative = local_path.as_posix()
            tag[attr] = relative

            # Mark missing pages so the mirror still navigates after more crawls.
            if known_urls and absolute not in known_urls and tag.name == "a":
                existing = tag.get("class", [])
                if isinstance(existing, str):
                    existing = existing.split()
                tag["class"] = list(existing) + ["mirror-pending"]

        # Inject a tiny banner so local mirror is visually distinct.
        if soup.body:
            banner = soup.new_tag("div")
            banner["style"] = (
                "position:sticky;top:0;z-index:9999;background:#111;color:#eee;"
                "padding:6px 10px;font:12px/1.4 monospace;"
            )
            banner.string = f"LOCAL MIRROR — source: {page_url}"
            soup.body.insert(0, banner)

        return str(soup)


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)[:180]
