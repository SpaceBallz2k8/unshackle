# WebUI Integration Guide

This directory contains everything needed to integrate the Web UI into your
unshackle fork at https://github.com/SpaceBallz2k8/unshackle

## What gets added to the fork

```
unshackle/
├── commands/
│   └── webui.py          ← NEW: `unshackle webui` command
└── webui/
    ├── __init__.py        ← NEW
    ├── app.py             ← NEW: FastAPI application
    ├── console.py         ← NEW: Rich console shim (real-time events)
    ├── jobs.py            ← NEW: Job runner using unshackle internals
    ├── search.py          ← NEW: Direct service search/list-titles
    ├── services_manager.py← NEW: Service zip/git management
    └── static/
        └── index.html     ← NEW: Single-page web UI

Dockerfile                 ← NEW: builds from your fork
docker-compose.yml         ← NEW: volume mounts
docker-entrypoint.sh       ← NEW: config setup + launch
strip_vaults.py            ← NEW: removes broken vault entries from config
```

## Step 1 — Copy files into your fork

From this zip, copy:

```bash
# In your fork root:
cp -r unshackle/commands/webui.py   unshackle/commands/
cp -r unshackle/webui/              unshackle/webui/
cp    Dockerfile                    .
cp    docker-compose.yml            .
cp    docker-entrypoint.sh          .
cp    strip_vaults.py               .
```

## Step 2 — Register the webui command

Edit `unshackle/__main__.py` and add the webui command to the CLI group.

Find where the other commands are added (looks like this):
```python
from unshackle.commands.dl import cli as dl
from unshackle.commands.search import cli as search
# ... etc

main.add_command(dl)
main.add_command(search)
```

Add:
```python
from unshackle.commands.webui import cli as webui
main.add_command(webui)
```

## Step 3 — Add dependencies to pyproject.toml

Add these to `[project.dependencies]`:
```toml
"fastapi>=0.115.0",
"uvicorn[standard]>=0.30.0",
"aiosqlite>=0.20.0",
"aiofiles>=23.2.0",
"python-multipart>=0.0.9",
```

Then run `uv sync` to install them.

## Step 4 — Test locally

```bash
uv run unshackle webui
# Open http://localhost:8080
```

## Step 5 — Docker

```bash
docker compose up -d --build
```

## How it works

Instead of spawning a subprocess and scraping terminal output, the WebUI:

1. **Imports unshackle directly** — `from unshackle.commands.dl import ...`
2. **Replaces the Rich Console** with `WebConsole` — a subclass that emits
   every `print()`/`log()` call as a JSON event to an asyncio queue
3. **Replaces rich.Progress** with `WebProgress` — tracks download progress
   and emits percentage/speed events in real-time
4. **Streams events to the browser** via SSE (Server-Sent Events) as structured
   JSON: `{"type": "log"|"progress"|"status", "message": "...", ...}`

For search and list-titles, the service is instantiated directly and
`.search()` / `.get_titles()` are called — returning typed Python objects
instead of parsed terminal strings.

## Keeping up with upstream

```bash
git remote add upstream https://github.com/unshackle-dl/unshackle.git
git fetch upstream
git merge upstream/main
# Resolve any conflicts in __main__.py or pyproject.toml
```

The webui/ directory is entirely additive — it doesn't touch any existing
unshackle files except `__main__.py` and `pyproject.toml`.
