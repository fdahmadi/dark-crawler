# Crawl timings

These are the waits the crawler uses. The pause between requests is not the download time. Tor and the forum still need their own time to answer.

Values below are the ones set on the server (`/opt/dark-crawler/config.yaml` and `dark-crawler.service`).

## Pause between requests

| Setting | Value | What it does |
|---|---|---|
| `request_delay_min` | 20 seconds | Shortest pause before the next HTTP request |
| `request_delay_max` | 40 seconds | Longest pause before the next HTTP request |
| `concurrency` | 1 | Only one request is in flight |

Before every request the crawler picks a random number between 20 and 40 seconds and waits that long. This includes each redirect hop. A page that redirects twice can therefore wait two or three times.

The first request of a run is not delayed. The pause starts after a response comes back.

Environment overrides, if set, replace the yaml values:

- `CRAWL_DELAY_MIN`
- `CRAWL_DELAY_MAX`

On the server both are set to `20` and `40`.

## Other crawl times

| Setting | Value | What it does |
|---|---|---|
| `timeout` | 90 seconds | How long to wait for one response before giving up |
| `retry_403_seconds` | 1800 seconds (30 minutes) | How long before a page that returned 403 is tried again |
| `max_pages` | 500 | Page budget for one run. `0` means no limit. 403 retries do not spend this budget |

`retry_403_seconds` is the code default. It is not repeated in the server yaml. A 403 page is saved, then put back on the queue after 30 minutes. Pages that returned 200 are not downloaded again.

If the forum answers `429 Too Many Requests`, the crawl stops and the web UI shows an alert. The same happens when the saved cookie is no longer accepted.

## Process times

These are systemd timings, not crawl settings.

| Setting | Value | What it does |
|---|---|---|
| `ExecStartPre` sleep | 15 seconds | Wait after the service starts, before the first fetch, so Tor is ready |
| `RestartSec` | 30 seconds | If the crawler process crashes, systemd waits this long before starting it again |

A normal finish does not restart the service. A rate-limit or cookie stop is a normal finish. The alert stays on the status page until a new cookie is saved or the crawl is started again.

## Where to change them

Server file:

```text
/opt/dark-crawler/config.yaml
```

Service environment:

```text
/etc/systemd/system/dark-crawler.service
```

After editing the service file:

```bash
systemctl daemon-reload
systemctl restart dark-crawler
```

Editing only `config.yaml` is enough for `timeout`. The delay range on the server is also forced by `CRAWL_DELAY_MIN` and `CRAWL_DELAY_MAX` in the service file, so change those too if you want a new pause range.
