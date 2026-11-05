# How to run

Two supported modes:

1. **Local / Docker Compose** — see Option A below
2. **Remote VPS without Docker (systemd)** — see [Server deploy](#server-deploy-no-docker)

---

## Prerequisites

- Docker + Docker Compose *(local Option A)*
- Or Python 3.12+ + Tor *(host / server mode)*

Project config: `config.yaml`  
Persistent output: `./data/` (`forum.db`, `mirror/`, logs)

---

## Server deploy (no Docker)

Target example: `root@104.194.142.112`  
App path: `/opt/dark-crawler`  
Tor SOCKS (localhost only): **`127.0.0.1:19050`** (not the default `9050`, to avoid clashes)

### Sync + install

From your laptop:

```bash
rsync -az --delete \
  --exclude '.venv' --exclude 'data/' --exclude '__pycache__' --exclude '.git' \
  ./ root@104.194.142.112:/opt/dark-crawler/

ssh root@104.194.142.112 'bash /opt/dark-crawler/deploy/install.sh'
```

This installs:

| Unit | Role |
|------|------|
| `dark-crawler-tor.service` | Isolated Tor on `127.0.0.1:19050` |
| `dark-crawler.service` | Crawler (low concurrency for small VPS) |

This stack uses **only** `127.0.0.1:19050`. It must **not** stop or touch the host Tor on `9050` if another service already uses it.

### Manage on the server

```bash
systemctl status dark-crawler-tor dark-crawler
journalctl -u dark-crawler-tor -f
journalctl -u dark-crawler -f

systemctl restart dark-crawler-tor
systemctl restart dark-crawler

# one-shot crawl / stats / export
sudo -u darkcrawler /opt/dark-crawler/.venv/bin/python -m crawler \
  -c /opt/dark-crawler/config.yaml stats
```

### Ports used on the VPS

| Port | Bind | Purpose |
|------|------|---------|
| `19050` | `127.0.0.1` | Tor SOCKS for crawler |
| `19051` | `127.0.0.1` | Tor control (local) |
| `19810` | `0.0.0.0` | Web UI (status + mirror) |

Tor ports stay localhost-only. The web UI uses **19810** to avoid clashing with existing 80/443/808x services.

### Web UI

```bash
systemctl status dark-crawler-web
journalctl -u dark-crawler-web -f

# token (required):
cat /opt/dark-crawler/deploy/web.env
```

Open:

```text
http://104.194.142.112:19810/?token=YOUR_TOKEN
```

Pages:

- `/` — crawl status, queue, recent pages, start button
- `/pages` — searchable crawled pages
- `/mirror/` — browse saved HTML mirror
- `/api/stats` — JSON stats

Token is stored in `/opt/dark-crawler/deploy/web.env` as `WEB_UI_TOKEN`.

### Data on server

```text
/opt/dark-crawler/data/forum.db
/opt/dark-crawler/data/mirror/
/opt/dark-crawler/data/crawler.log
/var/log/dark-crawler/tor.log
```

### Note: human verification walls

If the seed page returns a captcha / “Verify Humanity” challenge, the crawler will store that HTML but cannot discover forum links until you provide a valid session cookie via `crawl.cookies_file` in `config.yaml` (Netscape `cookies.txt` from a browser session you already completed).

---

## Option A — Docker (recommended)

### 1. Start the stack

```bash
./run.sh up
# same as:
docker compose up -d --build
```

This starts:

| Service  | Container           | Role                          |
|----------|---------------------|-------------------------------|
| `tor`    | `dark-crawler-tor`  | SOCKS5 proxy on `127.0.0.1:9050` |
| `crawler`| `dark-crawler-app`  | Forum crawler                 |

Crawler waits until Tor is healthy, then connects to `tor:9050` on the Docker network.

### 2. Follow logs

```bash
./run.sh logs
./run.sh logs crawler
./run.sh logs tor

# or:
docker compose logs -f crawler
docker compose logs -f tor
```

Wait until Tor shows:

```text
Bootstrapped 100% (done): Done
```

### 3. Check status

```bash
docker compose ps
docker compose run --rm crawler stats
```

### 4. One-shot crawl / export

```bash
./run.sh crawl
./run.sh stats
./run.sh export -o data/export
```

`./run.sh crawl` brings Tor up if needed, then runs the crawler once and removes the temporary container.

### 5. Stop

```bash
./run.sh down
# same as:
docker compose down
```

Tor state is kept in the Docker volume `tor-data` (faster next bootstrap).  
Crawl data stays in `./data/` on the host.

### Useful env overrides

```bash
CRAWL_MAX_PAGES=0 docker compose up -d crawler   # unlimited pages
CRAWL_MAX_PAGES=50 ./run.sh crawl                # short test run
```

Other overrides (see `crawler/config.py`):

- `TOR_SOCKS_HOST` / `TOR_SOCKS_PORT`
- `CRAWL_CONCURRENCY`
- `CRAWL_REQUEST_DELAY`
- `CRAWL_TIMEOUT`
- `SEED_URL`
- `LOG_LEVEL`

---

## Option B — Host Python + Docker Tor only

Keep Tor in Docker, run the crawler on the host (uses `127.0.0.1:9050` from `config.yaml`):

```bash
docker compose up -d tor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m crawler crawl
.venv/bin/python -m crawler stats
.venv/bin/python -m crawler export -o data/export
```

Or with `./run.sh` when Docker helpers are not used for the subcommand:

```bash
# if you call an unknown first arg, run.sh falls through to local venv:
./run.sh -c config.yaml crawl
```

> Note: `./run.sh crawl` always prefers Docker when Docker is installed.

---

## Option C — Fully local (no Docker)

1. Install and start system Tor (or open Tor Browser).
2. In `config.yaml`:
   - system Tor: `socks_port: 9050`
   - Tor Browser: `use_tor_browser_port: true` (port `9150`)
3. Run:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m crawler crawl
```

---

## CLI commands

```bash
python -m crawler crawl              # start / resume crawl
python -m crawler stats              # DB counters as JSON
python -m crawler export -o DIR      # CSV export
python -m crawler -c config.yaml ... # custom config path
```

Inside Docker the entrypoint already passes `-c /app/config.yaml`.

---

## Output layout

```text
data/
  forum.db      # SQLite: pages, forums, threads, posts, users, links, queue
  mirror/       # local HTML mirror with rewritten links
  crawler.log
  export/       # optional CSV from `export`
```

Browse the mirror locally:

```bash
python3 -m http.server 8080 --directory data/mirror
```

Then open: http://127.0.0.1:8080/

---

## Config tips

Edit `config.yaml` before crawling:

- `seed_url` — target `.onion` forum
- `crawl.max_pages` — `500` by default; `0` = unlimited
- `crawl.request_delay` — keep polite on Tor (default `2.0`s)
- `crawl.concurrency` — keep low (default `2`)
- `storage.enable_mirror` — HTML mirror on/off
- `crawl.cookies_file` — optional Netscape `cookies.txt` for a session you already own

After editing `config.yaml`, restart crawler:

```bash
docker compose up -d --force-recreate crawler
```

---

## Troubleshooting

| Symptom | What to do |
|---------|------------|
| `Tor is not reachable` | `docker compose up -d tor` and wait for healthy / `Bootstrapped 100%` |
| Tor stuck below 100% | Check `docker compose logs tor`; network may be slow — wait or restart `tor` |
| Crawler exits quickly | `docker compose logs crawler`; check `data/crawler.log` |
| Want a clean crawl queue | Stop stack, remove `data/forum.db`, start again |
| Rebuild everything | `docker compose build --no-cache && docker compose up -d` |

Quick Tor check from the host (after Tor container is up):

```bash
curl -sS --max-time 60 --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip
```

Expected JSON includes `"IsTor": true`.
