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
    """
    from unshackle.core.services import Services
    tag = Services.get_tag(service_name)
    try:
        return Services.load(tag)
    except KeyError:
        raise ValueError(f"Service '{service_name}' not found")


def _make_context(service_name: str, content_id: str):
    """
    Create a minimal Click context that service __init__ expects.
    Mirrors what unshackle's dl command builds before instantiating a service.
    """
    import click
    from unshackle.core.config import config as cfg
    from unshackle.core.utils.click_types import ContextData

    ctx = click.Context(click.Command(service_name))
    ctx.obj = ContextData(
        config=cfg,
        cdm=None,
        proxy_providers=[],
        profile=None
    )
    return ctx


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
