"""
services_manager.py — handles zip uploads and git repo management for services.

Services live flat in /services/<service_name>/
Git repos are cloned to a temp location, their service folders moved out into
/services/ directly, and the .git dir is kept in /data/repos/<repo_name>/.git
so we can still run git pull later.

DB tracks which service folders came from which repo URL so we can pull/delete them.
"""

import os
import asyncio
import shutil
import zipfile
import tempfile
from pathlib import Path
from typing import Optional
import aiosqlite

SERVICES_PATH = os.environ.get("UNSHACKLE_SERVICES", "/services")
DB_PATH = os.environ.get("DATABASE_URL", "/data/unshackle.db")
REPOS_META_PATH = "/data/repos"   # stores .git dirs for pull support


# ── DB bootstrap ──────────────────────────────────────────────────────────────

REPOS_DDL = """
CREATE TABLE IF NOT EXISTS service_repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_name    TEXT NOT NULL UNIQUE,   -- derived from URL, used as key
    url          TEXT NOT NULL,
    branch       TEXT NOT NULL DEFAULT 'main',
    services     TEXT NOT NULL DEFAULT '[]',  -- JSON list of service folder names
    last_pulled  TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
"""


async def ensure_repos_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(REPOS_DDL)
        await db.commit()


# ── Service discovery ─────────────────────────────────────────────────────────

def scan_services() -> list[dict]:
    """
    Return every service installed in /services.
    A valid service is a subfolder containing at least one .py file,
    or a top-level .py file (single-file style).
    """
    base = Path(SERVICES_PATH)
    base.mkdir(parents=True, exist_ok=True)
    results = []

    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_dir():
            py_files = list(entry.glob("*.py"))
            if py_files:
                results.append({
                    "name": entry.name,
                    "type": "folder",
                    "path": str(entry),
                    "files": [f.name for f in sorted(py_files)],
                })
        elif entry.is_file() and entry.suffix == ".py":
            results.append({
                "name": entry.stem,
                "type": "file",
                "path": str(entry),
                "files": [entry.name],
            })

    return results


# ── Zip upload ────────────────────────────────────────────────────────────────

def install_from_zip(zip_bytes: bytes) -> list[str]:
    """
    Extract a zip into /services, placing each top-level folder directly there.
    Returns list of installed service names.
    """
    base = Path(SERVICES_PATH)
    installed = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "upload.zip"
        zip_path.write_bytes(zip_bytes)

        with zipfile.ZipFile(zip_path, "r") as zf:
            safe_members = [
                m for m in zf.infolist()
                if not m.filename.startswith("/") and ".." not in m.filename
            ]
            zf.extractall(tmp_path / "extracted", members=safe_members)

        extracted = tmp_path / "extracted"
        top_level = [p for p in extracted.iterdir() if not p.name.startswith("__")]

        if not top_level:
            raise ValueError("Zip appears to be empty or contains no valid entries")

        for entry in top_level:
            if entry.is_dir():
                dest = base / entry.name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(entry, dest)
                installed.append(entry.name)
            elif entry.is_file() and entry.suffix == ".py":
                dest = base / entry.name
                shutil.copy2(entry, dest)
                installed.append(entry.stem)

    return installed


# ── Git helpers ───────────────────────────────────────────────────────────────

async def _run_git(args: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _repo_git_dir(repo_name: str) -> Path:
    """Path where we store the .git directory for a repo."""
    return Path(REPOS_META_PATH) / repo_name


# ── Clone ─────────────────────────────────────────────────────────────────────

async def clone_repo(url: str, branch: str = "main") -> dict:
    """
    Clone repo into a temp dir, then move each service folder directly into
    /services/<service_name>. Keep the .git dir in /data/repos/<repo_name>
    so we can git pull later by pointing GIT_DIR + GIT_WORK_TREE per service.
    """
    import json

    base = Path(SERVICES_PATH)
    repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")

    # Check none of this repo's services already exist (re-clone guard)
    existing_repo = await _get_repo(repo_name)
    if existing_repo:
        raise ValueError(
            f"Repo '{repo_name}' is already installed. Use Pull to update it."
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / repo_name

        # Try with branch first, fall back to default
        rc, out, err = await _run_git(
            ["clone", "--depth", "1", "--branch", branch, url, str(tmp_path)]
        )
        if rc != 0:
            rc, out, err = await _run_git(
                ["clone", "--depth", "1", url, str(tmp_path)]
            )
        if rc != 0:
            raise RuntimeError(f"git clone failed:\n{err}")

        # Discover service folders inside the cloned repo
        service_dirs = _find_service_entries(tmp_path)
        if not service_dirs:
            raise ValueError(
                "No service folders found in repo. "
                "Expected subfolders containing .py files."
            )

        # Move each service folder directly into /services/
        installed = []
        for entry in service_dirs:
            dest = base / entry.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(entry), str(dest))
            installed.append(entry.name)

        # Save the .git dir so we can pull later
        git_store = _repo_git_dir(repo_name)
        git_store.parent.mkdir(parents=True, exist_ok=True)
        if git_store.exists():
            shutil.rmtree(git_store)
        shutil.move(str(tmp_path / ".git"), str(git_store))

    # Persist to DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO service_repos (repo_name, url, branch, services, last_pulled)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(repo_name) DO UPDATE SET
                 url=excluded.url, branch=excluded.branch,
                 services=excluded.services, last_pulled=datetime('now')""",
            (repo_name, url, branch, json.dumps(installed)),
        )
        await db.commit()

    return {
        "repo_name": repo_name,
        "url": url,
        "branch": branch,
        "services": installed,
        "message": f"Installed {len(installed)} service(s): {', '.join(installed)}",
    }


# ── Pull ──────────────────────────────────────────────────────────────────────

async def pull_repo(repo_name: str) -> dict:
    """
    Pull updates for a previously cloned repo.
    Uses the stored .git dir + each service folder as the work tree.
    Pulls into a temp dir, then syncs changed files into /services/<service>/.
    """
    import json

    repo = await _get_repo(repo_name)
    if not repo:
        raise ValueError(f"Repo '{repo_name}' not found in database")

    git_dir = _repo_git_dir(repo_name)
    if not git_dir.exists():
        raise ValueError(
            f"Git metadata for '{repo_name}' missing from /data/repos. "
            "You may need to delete and re-clone."
        )

    services = json.loads(repo["services"])
    base = Path(SERVICES_PATH)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / repo_name
        tmp_path.mkdir()

        # Clone fresh from the stored git dir (it acts as a local remote)
        rc, out, err = await _run_git(
            ["clone", str(git_dir), str(tmp_path)]
        )
        if rc != 0:
            # Fall back: fetch into git_dir then checkout
            rc2, out2, err2 = await _run_git(
                ["--git-dir", str(git_dir), "fetch", "--depth", "1", "origin"]
            )
            if rc2 != 0:
                raise RuntimeError(f"git fetch failed:\n{err2}")
            out = out2

        # Also fetch updates into the stored git dir for future pulls
        rc_f, _, err_f = await _run_git(
            ["--git-dir", str(git_dir), "fetch", "--depth", "1", "origin"],
        )

        # Sync each service folder from tmp clone into /services/
        updated = []
        for svc_name in services:
            src = tmp_path / svc_name
            dest = base / svc_name
            if src.exists():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
                updated.append(svc_name)

        # Check for new service folders the repo may have added
        new_services = _find_service_entries(tmp_path)
        new_names = [e.name for e in new_services]
        added = [n for n in new_names if n not in services]
        for svc_name in added:
            src = tmp_path / svc_name
            dest = base / svc_name
            if src.exists():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
                updated.append(svc_name)

        all_services = list(set(services + added))

    # Update DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE service_repos SET last_pulled=datetime('now'), services=? WHERE repo_name=?",
            (json.dumps(all_services), repo_name),
        )
        await db.commit()

    msg = f"Updated: {', '.join(updated)}" if updated else "Already up to date"
    return {"repo_name": repo_name, "services": all_services, "output": msg}


# ── Delete ────────────────────────────────────────────────────────────────────

async def delete_service_entry(name: str):
    """
    Remove a service folder/file from /services.
    If it belongs to a repo, also remove that repo's other services + git metadata.
    """
    import json

    base = Path(SERVICES_PATH)
    folder = base / name
    single = base / f"{name}.py"

    # Check if this service belongs to a repo
    repo = await _get_repo_by_service(name)

    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder)
    elif single.exists():
        single.unlink()
    else:
        raise FileNotFoundError(f"Service '{name}' not found")

    if repo:
        repo_name = repo["repo_name"]
        services = json.loads(repo["services"])
        # Remove all service folders from this repo
        for svc in services:
            p = base / svc
            if p.exists():
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        # Remove stored git dir
        git_dir = _repo_git_dir(repo_name)
        if git_dir.exists():
            shutil.rmtree(git_dir)
        # Remove from DB
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM service_repos WHERE repo_name=?", (repo_name,))
            await db.commit()
    else:
        # Standalone service — just clean up if somehow in DB
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM service_repos WHERE repo_name=?", (name,))
            await db.commit()


async def list_repos() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM service_repos ORDER BY repo_name") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_service_entries(path: Path) -> list[Path]:
    """Return subfolders (or .py files) that look like services."""
    found = []
    for entry in sorted(path.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_dir() and any(entry.glob("*.py")):
            found.append(entry)
        elif entry.is_file() and entry.suffix == ".py":
            found.append(entry)
    return found


async def _get_repo(repo_name: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM service_repos WHERE repo_name=?", (repo_name,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _get_repo_by_service(service_name: str) -> Optional[dict]:
    """Find which repo a service folder belongs to (if any)."""
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM service_repos") as cur:
            rows = await cur.fetchall()
            for row in rows:
                services = json.loads(row["services"])
                if service_name in services:
                    return dict(row)
    return None
