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

    tor = config.tor
    if not check_tor_port(tor.socks_host, tor.port):
        print(
            f"Tor is not reachable at {tor.socks_host}:{tor.port}.\n"
            "  - System Tor: sudo apt install tor && sudo systemctl start tor\n"
            "  - Tor Browser: open it and set tor.use_tor_browser_port: true",
            file=sys.stderr,
        )
        return 2

    db = Database(config.database_path)
    await db.connect()
    try:
        spider = Spider(config, db)
        stats = await spider.run()
        print("Crawl finished.")
        for key, value in sorted(stats.items()):
            print(f"  {key}: {value}")
        if config.storage.enable_mirror:
            print(f"Mirror directory: {config.mirror_path}")
        print(f"Database: {config.database_path}")
        return 0
    except TorNotAvailableError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        await db.close()


async def cmd_stats(config_path: Path) -> int:
    config = load_config(config_path)
    db = Database(config.database_path)
    await db.connect()
    try:
        stats = await db.stats()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    finally:
        await db.close()


async def cmd_export(config_path: Path, out_dir: Path) -> int:
    config = load_config(config_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.database_path)
    await db.connect()
    try:
        tables = ("pages", "forums", "threads", "posts", "users", "links")
        for table in tables:
            async with db.conn.execute(f"SELECT * FROM {table}") as cursor:
                rows = await cursor.fetchall()
                if not rows:
                    continue
                columns = [item[0] for item in cursor.description]
            csv_path = out_dir / f"{table}.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    writer.writerow({col: row[col] for col in columns})
            print(f"Wrote {csv_path} ({len(rows)} rows)")
        return 0
    finally:
        await db.close()


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

    sub.add_parser("crawl", help="Start / resume crawling")
    sub.add_parser("stats", help="Print database statistics")

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
