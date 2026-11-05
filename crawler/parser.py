"""HTML parsing: link discovery + best-effort forum entity extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag


TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid")


def normalize_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "data:")):
        return None

    absolute = urljoin(base_url, href)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None

    # Drop common tracking params while keeping forum query ids.
    if parsed.query:
        kept = []
        for part in parsed.query.split("&"):
            if not part:
                continue
            key = part.split("=", 1)[0].lower()
            if any(key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
                continue
            kept.append(part)
        query = "&".join(kept)
        parsed = parsed._replace(query=query)

    # Normalize trailing slash for directory-like paths without extension.
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    parsed = parsed._replace(path=path or "/")
    return urlunparse(parsed)


def extract_text(element: Tag | None) -> str:
    if element is None:
        return ""
    return " ".join(element.stripped_strings)


@dataclass
class ParsedPage:
    title: str | None
    text_content: str
    links: list[str] = field(default_factory=list)
    forums: list[dict[str, Any]] = field(default_factory=list)
    threads: list[dict[str, Any]] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    users: list[dict[str, Any]] = field(default_factory=list)


class ForumParser:
    """Heuristic parser for common forum layouts (phpBB-like / generic)."""

    THREAD_HINTS = re.compile(
        r"(viewtopic|showthread|thread|topic|t=\d+|threads/)",
        re.IGNORECASE,
    )
    FORUM_HINTS = re.compile(
        r"(viewforum|forumdisplay|forum|f=\d+|forums/)",
        re.IGNORECASE,
    )

    def parse(self, url: str, html: str, structured: bool = True) -> ParsedPage:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None
        text_content = " ".join(soup.stripped_strings)

        links: list[str] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            normalized = normalize_url(url, anchor.get("href"))
            if normalized and normalized not in seen:
                seen.add(normalized)
                links.append(normalized)

        page = ParsedPage(title=title, text_content=text_content, links=links)
        if structured:
            self._extract_entities(url, soup, page)
        return page

    def _extract_entities(
        self, url: str, soup: BeautifulSoup, page: ParsedPage
    ) -> None:
        # Generic forum/category blocks
        for node in soup.select(
            ".forum, .forums, .category, li.row, tr.forum, .structItem--forum"
        ):
            link = node.find("a", href=True)
            if not link:
                continue
            href = normalize_url(url, link.get("href"))
            name = extract_text(link)
            if not href or not name:
                continue
            if self.FORUM_HINTS.search(href) or "forum" in " ".join(
                node.get("class", [])
            ).lower():
                page.forums.append(
                    {
                        "external_id": self._id_from_url(href),
                        "name": name,
                        "url": href,
                        "description": extract_text(
                            node.select_one(".forum-description, .description")
                        ),
                    }
                )

        # Thread list rows
        for node in soup.select(
            ".topic, .thread, tr.topic, li.structItem--thread, .structItem--thread"
        ):
            link = node.find("a", href=True)
            if not link:
                continue
            href = normalize_url(url, link.get("href"))
            title = extract_text(link)
            if not href or not title:
                continue
            if not (
                self.THREAD_HINTS.search(href)
                or "thread" in " ".join(node.get("class", [])).lower()
                or "topic" in " ".join(node.get("class", [])).lower()
            ):
                continue

            author_node = node.select_one(
                ".author, .username, .structItem-parts a, .topic-author"
            )
            page.threads.append(
                {
                    "external_id": self._id_from_url(href),
                    "forum_url": url,
                    "title": title,
                    "url": href,
                    "author": extract_text(author_node) or None,
                    "created_at": extract_text(
                        node.select_one("time, .time, .posted-date")
                    )
                    or None,
                    "reply_count": self._first_int(
                        extract_text(
                            node.select_one(".replies, .posts, .reply-count")
                        )
                    ),
                    "view_count": self._first_int(
                        extract_text(node.select_one(".views, .view-count"))
                    ),
                }
            )

        # Posts inside a thread page
        post_nodes = soup.select(
            ".post, .message, article.message, .structItem--comment, div[id^=post]"
        )
        if post_nodes and self.THREAD_HINTS.search(url):
            for node in post_nodes:
                author = extract_text(
                    node.select_one(
                        ".author, .username, .message-name, .post-author, h4 a"
                    )
                )
                content_node = node.select_one(
                    ".content, .message-content, .post-content, .bbWrapper, .postbody"
                )
                if content_node is None:
                    content_node = node
                post_id = node.get("id") or node.get("data-content")
                permalink = node.select_one("a[href*='post'], a.permalink")
                post_url = normalize_url(
                    url, permalink.get("href") if permalink else None
                ) or url
                content_html = str(content_node)
                content_text = extract_text(content_node)
                if not content_text:
                    continue
                page.posts.append(
                    {
                        "external_id": str(post_id) if post_id else None,
                        "thread_url": url,
                        "author": author or None,
                        "posted_at": extract_text(
                            node.select_one("time, .time, .post-date")
                        )
                        or None,
                        "content_html": content_html,
                        "content_text": content_text,
                        "post_url": post_url,
                    }
                )
                if author:
                    profile = node.select_one("a.username, .author a, h4 a")
                    page.users.append(
                        {
                            "username": author,
                            "profile_url": normalize_url(
                                url, profile.get("href") if profile else None
                            ),
                        }
                    )

        # Fallback: treat obvious thread/forum links from anchors as entities
        if not page.threads:
            for anchor in soup.find_all("a", href=True):
                href = normalize_url(url, anchor.get("href"))
                text = extract_text(anchor)
                if href and text and self.THREAD_HINTS.search(href):
                    page.threads.append(
                        {
                            "external_id": self._id_from_url(href),
                            "forum_url": url,
                            "title": text,
                            "url": href,
                            "author": None,
                            "created_at": None,
                            "reply_count": None,
                            "view_count": None,
                        }
                    )

        if not page.forums:
            for anchor in soup.find_all("a", href=True):
                href = normalize_url(url, anchor.get("href"))
                text = extract_text(anchor)
                if href and text and self.FORUM_HINTS.search(href):
                    page.forums.append(
                        {
                            "external_id": self._id_from_url(href),
                            "name": text,
                            "url": href,
                            "description": None,
                        }
                    )

    @staticmethod
    def _id_from_url(url: str) -> str | None:
        parsed = urlparse(url)
        match = re.search(r"(?:^|&)(?:t|f|p)=(\d+)", parsed.query)
        if match:
            return match.group(1)
        match = re.search(r"/(\d+)(?:/?$|\.)", parsed.path)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _first_int(value: str) -> int | None:
        match = re.search(r"\d+", value.replace(",", ""))
        return int(match.group()) if match else None
