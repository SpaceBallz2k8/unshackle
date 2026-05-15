"""
webui/search.py

Calls unshackle's search and list-titles internals directly.
No subprocess. No output parsing.
"""
from __future__ import annotations

from typing import Optional


def _load_service_class(service_name: str):
    """
    Load a service class by name from the configured services directories.
    Mirrors what unshackle's dl command does internally.
    """
    import importlib.util
    import os
    import sys
    from pathlib import Path

    services_dirs = os.environ.get("UNSHACKLE_SERVICES", "/services")
    search_paths = [Path(p) for p in services_dirs.split(":") if p]

    # Also check the XDG services dir
    xdg_svc = Path("/root/.config/unshackle/services")
    if xdg_svc.exists():
        search_paths.append(xdg_svc)

    for base in search_paths:
        if not base.exists():
            continue
        # Service folder: base/SERVICE_NAME/__init__.py
        folder = base / service_name / "__init__.py"
        if folder.exists():
            spec = importlib.util.spec_from_file_location(
                f"services.{service_name}", folder,
                submodule_search_locations=[str(folder.parent)]
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"services.{service_name}"] = mod
            spec.loader.exec_module(mod)
            # The class should have the same name as the service
            cls = getattr(mod, service_name, None)
            if cls is None:
                # Try to find any Service subclass
                from unshackle.core.service import Service
                for attr in dir(mod):
                    obj = getattr(mod, attr)
                    try:
                        if isinstance(obj, type) and issubclass(obj, Service) and obj is not Service:
                            cls = obj
                            break
                    except TypeError:
                        pass
            if cls:
                return cls

    raise ValueError(f"Service '{service_name}' not found in {search_paths}")


def _make_context(service_name: str, content_id: str):
    """
    Create a minimal Click context that service __init__ expects.
    Mirrors what unshackle's dl command builds before instantiating a service.
    """
    import click
    from unshackle.core.config import config as cfg

    ctx = click.Context(click.Command(service_name))
    ctx.obj = {
        "config": cfg,
        "service": service_name,
        "title": content_id,
        # Minimal track_request to avoid AttributeError
        "track_request": _FakeTrackRequest(),
    }
    return ctx


class _FakeTrackRequest:
    codecs = []
    ranges = []
    wanted = None
    lang = ["en"]
    sub_lang = ["en"]


async def search_service(service_name: str, query: str) -> list[dict]:
    """
    Instantiate the service with the query as the title, call .search(),
    return a list of result dicts.
    """
    import asyncio

    def _run():
        try:
            cls = _load_service_class(service_name)
            ctx = _make_context(service_name, query)
            instance = cls.__new__(cls)
            # Minimal init — just enough to call search()
            # We call the parent Service.__init__ manually
            from unshackle.core.service import Service
            Service.__init__(instance, ctx)
            instance.title = query

            results = []
            for r in instance.search():
                results.append({
                    "id": r.id_,
                    "title": r.title,
                    "description": getattr(r, "description", ""),
                    "label": getattr(r, "label", ""),
                    "url": getattr(r, "url", ""),
                    "year": getattr(r, "year", ""),
                })
            return results
        except Exception as e:
            raise RuntimeError(f"Search failed: {e}") from e

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def list_titles(service_name: str, content_id: str) -> list[dict]:
    """
    Instantiate the service with the content_id, call .get_titles(),
    return structured episode/movie list.
    """
    import asyncio

    def _run():
        try:
            cls = _load_service_class(service_name)
            ctx = _make_context(service_name, content_id)
            instance = cls.__new__(cls)
            from unshackle.core.service import Service
            Service.__init__(instance, ctx)
            instance.title = content_id

            from unshackle.core.titles import Episode, Movie, Movies, Series

            titles_obj = instance.get_titles()
            results = []

            if isinstance(titles_obj, Series):
                items = list(titles_obj)
            elif isinstance(titles_obj, Movies):
                items = list(titles_obj)
            else:
                items = [titles_obj] if titles_obj else []

            for i, t in enumerate(items, 1):
                if isinstance(t, Episode):
                    results.append({
                        "index": i,
                        "type": "episode",
                        "episode_id": f"S{t.season:02d}E{t.number:02d}" if t.season and t.number else "",
                        "title": t.title or "",
                        "season": t.season,
                        "number": t.number,
                        "id": t.id,
                        "service_data": str(t.id),
                    })
                elif isinstance(t, Movie):
                    results.append({
                        "index": i,
                        "type": "movie",
                        "episode_id": "",
                        "title": t.title or str(t.id),
                        "year": getattr(t, "year", ""),
                        "id": t.id,
                        "service_data": str(t.id),
                    })
            return results
        except Exception as e:
            raise RuntimeError(f"list_titles failed: {e}") from e

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)
