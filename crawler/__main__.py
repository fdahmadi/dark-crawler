"""CLI entry: python -m crawler [--config config.yaml] [crawl|stats|export]."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .sites import enabled_sites, load_registry, migrate_legacy_layout, site_paths
from .spider import Spider
from .tor_client import TorNotAvailableError, check_tor_port


def setup_logging(level: str, log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(stream)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


async def cmd_crawl(config_path: Path) -> int:
    config = load_config(config_path)
    setup_logging(config.logging.level, config.log_path)
    migrate_legacy_layout(config.project_root, config.seed_url)

    tor = config.tor
    if not check_tor_port(tor.socks_host, tor.port):
        print(
            f"Tor is not reachable at {tor.socks_host}:{tor.port}.\n"
            "  - System Tor: sudo apt install tor && sudo systemctl start tor\n"
            "  - Tor Browser: open it and set tor.use_tor_browser_port: true",
            file=sys.stderr,
        )
        return 2

    if not enabled_sites(config.project_root):
        print(
            "No enabled sites. Open the Web UI Sites page and add/enable a forum.",
            file=sys.stderr,
        )
        return 1

    try:
        spider = Spider(config)
        stats = await spider.run()
        print("Crawl finished.")
        for key, value in sorted(stats.items()):
            print(f"  {key}: {value}")
        print(f"Sites registry: {config.project_root / 'data' / 'sites.json'}")
        return 0
    except TorNotAvailableError as exc:
        print(str(exc), file=sys.stderr)
        return 2


async def cmd_stats(config_path: Path) -> int:
    config = load_config(config_path)
    migrate_legacy_layout(config.project_root, config.seed_url)
    payload: dict[str, object] = {"sites": []}
    for site in load_registry(config.project_root):
        paths = site_paths(config.project_root, site.id)
        entry: dict[str, object] = {
            "id": site.id,
            "host": site.host,
            "enabled": site.enabled,
            "seed_url": site.seed_url,
            "stats": {},
        }
        if paths.database.exists():
            db = Database(paths.database)
            await db.connect()
            try:
                entry["stats"] = await db.stats()
            finally:
                await db.close()
        payload["sites"].append(entry)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


async def cmd_export(config_path: Path, out_dir: Path) -> int:
    config = load_config(config_path)
    migrate_legacy_layout(config.project_root, config.seed_url)
    out_dir.mkdir(parents=True, exist_ok=True)
    for site in load_registry(config.project_root):
        paths = site_paths(config.project_root, site.id)
        if not paths.database.exists():
            continue
        site_out = out_dir / site.id
        site_out.mkdir(parents=True, exist_ok=True)
        db = Database(paths.database)
        await db.connect()
        try:
            tables = ("pages", "forums", "threads", "posts", "users", "links")
            for table in tables:
                async with db.conn.execute(f"SELECT * FROM {table}") as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        continue
                    columns = [item[0] for item in cursor.description]
                csv_path = site_out / f"{table}.csv"
                with csv_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({col: row[col] for col in columns})
                print(f"Wrote {csv_path} ({len(rows)} rows)")
        finally:
            await db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tor forum crawler with SQLite DB + local HTML mirror"
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("crawl", help="Start / resume crawling enabled sites")
    sub.add_parser("stats", help="Print per-site database statistics")

    export_parser = sub.add_parser("export", help="Export tables to CSV")
    export_parser.add_argument(
        "-o",
        "--out",
        default="data/export",
        help="Output directory for CSV files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        return 1

    if args.command == "crawl":
        return asyncio.run(cmd_crawl(config_path))
    if args.command == "stats":
        return asyncio.run(cmd_stats(config_path))
    if args.command == "export":
        return asyncio.run(cmd_export(config_path, Path(args.out)))
    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
