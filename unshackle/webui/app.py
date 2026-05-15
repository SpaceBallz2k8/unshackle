"""
webui/app.py

FastAPI application. Serves the web UI and REST API.
Runs inside the same Python process as unshackle — no subprocess needed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncGenerator, Optional

import aiosqlite
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.environ.get("DATABASE_URL", "/data/unshackle.db")
SERVICES_PATH = os.environ.get("UNSHACKLE_SERVICES", "/services")
DOWNLOADS_PATH = os.environ.get("UNSHACKLE_DOWNLOADS", "/downloads")
WVD_PATH = "/config/WVDs"

app = FastAPI(title="Unshackle WebUI", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Static files + SPA
_here = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_here / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_here / "static" / "index.html").read_text()


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await _init_db()


async def _init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                service     TEXT NOT NULL,
                content_id  TEXT NOT NULL,
                title       TEXT,
                status      TEXT NOT NULL DEFAULT 'queued',
                log         TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS service_repos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name    TEXT NOT NULL UNIQUE,
                url          TEXT NOT NULL,
                branch       TEXT NOT NULL DEFAULT 'main',
                services     TEXT NOT NULL DEFAULT '[]',
                last_pulled  TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO settings (key, value) VALUES
                ('download_path', '/downloads'),
                ('concurrent_downloads', '2'),
                ('default_lang', 'en'),
                ('video_quality', 'best');
        """)
        await db.commit()


# ── Models ─────────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    service: str
    content_id: str
    title: Optional[str] = None
    extra_args: Optional[list[str]] = []


class SearchRequest(BaseModel):
    service: str
    query: str


class ListTitlesRequest(BaseModel):
    service: str
    item_id: str


class GitCloneRequest(BaseModel):
    url: str
    branch: str = "main"


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


# ── Jobs ───────────────────────────────────────────────────────────────────────

@app.post("/api/download")
async def start_download(req: DownloadRequest):
    from unshackle.webui import jobs as job_mgr
    job_id = await job_mgr.create_job(
        service=req.service,
        content_id=req.content_id,
        title=req.title,
        extra_args=req.extra_args,
    )
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs")
async def list_jobs():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, service, content_id, title, status, created_at, updated_at "
            "FROM jobs ORDER BY created_at DESC LIMIT 100"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            return dict(row)


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    from unshackle.webui import jobs as job_mgr
    job_mgr.cancel_job(job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('queued','running')",
            (job_id,)
        )
        await db.commit()
    return {"cancelled": job_id}


@app.delete("/api/jobs")
async def clear_jobs(status: str = "all"):
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "all":
            await db.execute("DELETE FROM jobs WHERE status != 'running'")
        else:
            await db.execute("DELETE FROM jobs WHERE status = ?", (status,))
        await db.commit()
    return {"cleared": status}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    """SSE endpoint — streams live WebEvents for a job."""

    async def generate() -> AsyncGenerator[str, None]:
        # First replay the stored log
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
                job = await cur.fetchone()

        if not job:
            yield "data: {}\n\n".format(json.dumps({"type": "error", "message": "Job not found"}))
            return

        # Replay stored log
        for line in (job["log"] or "").splitlines():
            if line.strip():
                yield "data: {}\n\n".format(json.dumps({"type": "log", "message": line}))

        # If already done, just send status
        if job["status"] not in ("queued", "running"):
            yield "data: {}\n\n".format(json.dumps({"type": "status", "message": job["status"]}))
            return

        # Subscribe to live events
        from unshackle.webui import jobs as job_mgr
        from unshackle.webui.console import WebEvent

        q = job_mgr.get_queue(job_id)
        if not q:
            yield "data: {}\n\n".format(json.dumps({"type": "status", "message": job["status"]}))
            return

        sub = q.subscribe()
        try:
            while True:
                try:
                    evt: WebEvent = await asyncio.wait_for(sub.get(), timeout=30.0)
                    payload = {
                        "type": evt.type,
                        "message": evt.message,
                    }
                    if evt.type == "progress":
                        payload.update({
                            "task_id": evt.task_id,
                            "completed": evt.completed,
                            "total": evt.total,
                            "speed": evt.speed,
                        })
                    yield "data: {}\n\n".format(json.dumps(payload))
                    if evt.type == "status" and evt.message not in ("queued", "running"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            q.unsubscribe(sub)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Search ─────────────────────────────────────────────────────────────────────

@app.post("/api/search")
async def search_service(req: SearchRequest):
    from unshackle.webui.search import search_service as _search
    try:
        results = await _search(req.service, req.query)
        return {"service": req.service, "query": req.query, "results": results}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/list-titles")
async def list_titles(req: ListTitlesRequest):
    from unshackle.webui.search import list_titles as _list
    try:
        titles = await _list(req.service, req.item_id)
        return {"service": req.service, "item_id": req.item_id, "titles": titles}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Services ───────────────────────────────────────────────────────────────────

@app.get("/api/services")
async def list_services():
    # Use v4's Services loader which reads from config.directories.services
    from unshackle.core.services import Services
    tags = Services.get_tags()
    # Also check for repo tracking
    try:
        from unshackle.webui import services_manager as svc_mgr
        repos = await svc_mgr.list_repos()
        repo_map = {}
        for r in repos:
            for svc in json.loads(r.get("services", "[]")):
                repo_map[svc] = r
    except Exception:
        repo_map = {}
    services = []
    for tag in sorted(tags):
        entry = {"name": tag, "type": "folder", "files": ["__init__.py"]}
        if tag in repo_map:
            entry["repo"] = repo_map[tag]
        services.append(entry)
    return {"services": services}


@app.post("/api/services/upload-zip")
async def upload_zip(file: UploadFile = File(...)):
    import zipfile
    from unshackle.webui import services_manager as svc_mgr
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Only .zip files accepted")
    data = await file.read()
    try:
        installed = svc_mgr.install_from_zip(data)
    except (ValueError, zipfile.BadZipFile) as e:
        raise HTTPException(400, str(e))
    return {"installed": installed, "count": len(installed)}


@app.post("/api/services/clone")
async def clone_repo(req: GitCloneRequest):
    from unshackle.webui import services_manager as svc_mgr
    try:
        result = await svc_mgr.clone_repo(req.url, req.branch)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return result


@app.post("/api/services/{name}/pull")
async def pull_repo(name: str):
    from unshackle.webui import services_manager as svc_mgr
    try:
        result = await svc_mgr.pull_repo(name)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return result


@app.delete("/api/services/{name}")
async def delete_service(name: str):
    from unshackle.webui import services_manager as svc_mgr
    try:
        await svc_mgr.delete_service_entry(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"deleted": name}


@app.get("/api/services/{name}/source")
async def get_service_source(name: str):
    base = Path(SERVICES_PATH)
    folder = base / name
    single = base / f"{name}.py"
    if folder.exists():
        py_files = sorted(folder.glob("*.py"))
        if not py_files:
            raise HTTPException(404, "No .py files in service folder")
        parts = [f"# {'='*60}\n# {f.name}\n# {'='*60}\n{f.read_text()}" for f in py_files]
        return {"name": name, "source": "\n\n".join(parts), "files": [f.name for f in py_files]}
    elif single.exists():
        return {"name": name, "source": single.read_text(), "files": [single.name]}
    raise HTTPException(404, "Service not found")


# ── WVDs ───────────────────────────────────────────────────────────────────────

@app.get("/api/wvds")
async def list_wvds():
    base = Path(WVD_PATH)
    base.mkdir(parents=True, exist_ok=True)
    files = [
        {"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime}
        for f in sorted(base.iterdir()) if f.suffix.lower() == ".wvd"
    ]
    return {"wvds": files}


@app.post("/api/wvds/upload")
async def upload_wvd(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".wvd"):
        raise HTTPException(400, "Only .wvd files accepted")
    Path(WVD_PATH).mkdir(parents=True, exist_ok=True)
    (Path(WVD_PATH) / file.filename).write_bytes(await file.read())
    return {"filename": file.filename}


@app.delete("/api/wvds/{filename}")
async def delete_wvd(filename: str):
    p = Path(WVD_PATH) / filename
    if not p.exists():
        raise HTTPException(404, "WVD not found")
    p.unlink()
    return {"deleted": filename}


# ── Config ─────────────────────────────────────────────────────────────────────

CONFIG_PATH = "/config/unshackle.yaml"
XDG_CONFIG = "/root/.config/unshackle/unshackle.yaml"


@app.get("/api/config")
async def get_config():
    p = Path(CONFIG_PATH)
    return {"content": p.read_text() if p.exists() else ""}


@app.post("/api/config")
async def save_config(payload: dict):
    content = payload.get("content", "")
    Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(CONFIG_PATH).write_text(content)
    # Also write to XDG location unshackle reads from
    Path(XDG_CONFIG).parent.mkdir(parents=True, exist_ok=True)
    Path(XDG_CONFIG).write_text(content)
    return {"saved": True}


# ── Settings ───────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM settings") as cur:
            return {r["key"]: r["value"] for r in await cur.fetchall()}


@app.post("/api/settings")
async def update_settings(payload: SettingsUpdate):
    async with aiosqlite.connect(DB_PATH) as db:
        for k, v in payload.settings.items():
            await db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v)
            )
        await db.commit()
    return {"saved": True}


# ── Downloads browser ──────────────────────────────────────────────────────────

@app.get("/api/downloads")
async def list_downloads():
    base = Path(DOWNLOADS_PATH)
    files = []
    if base.exists():
        for f in sorted(base.rglob("*")):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "path": str(f.relative_to(base)),
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                })
    return {"files": files}


# ── Credentials ────────────────────────────────────────────────────────────────

@app.get("/api/credentials")
async def list_credentials():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Table may not exist yet in new DB
        try:
            async with db.execute(
                "SELECT id, service, type, label, created_at FROM credentials ORDER BY service"
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception:
            return []


# Credentials table DDL (added to startup)
@app.on_event("startup")
async def _ensure_credentials_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                service    TEXT NOT NULL,
                type       TEXT NOT NULL,
                label      TEXT,
                data       TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
