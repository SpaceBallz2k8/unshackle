"""
webui/search.py

Calls unshackle v4 service internals directly for search and list-titles.
Uses the real Services loader, ContextData, and dl auth helpers.
"""
from __future__ import annotations

import asyncio
import logging
import yaml
from typing import Optional

log = logging.getLogger("webui.search")


def _build_context(service_tag: str, title_id: str):
    """Build a minimal Click context that Service.__init__ expects."""
    import click
    from unshackle.core.config import config
    from unshackle.core.utils.click_types import ContextData
    from unshackle.core.utils.collections import merge_dict
    from unshackle.core.services import Services

    # Load service config (same as search/dl commands do)
    service_config_path = Services.get_path(service_tag) / config.filenames.config
    if service_config_path.exists():
        service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8")) or {}
    else:
        service_config = {}
    merge_dict(config.services.get(service_tag), service_config)

    ctx_data = ContextData(
        config=service_config,
        cdm=None,
        proxy_providers=[],
        profile=None,
    )

    # Build a parent context that mirrors what `dl` provides
    # Service.__init__ reads ctx.parent.params for vcodec, range_, proxy etc.
    parent_cmd = click.Command("dl")
    parent_ctx = click.Context(parent_cmd)
    parent_ctx.params = {
        "proxy": None,
        "no_proxy": False,
        "proxy_query": None,
        "proxy_provider": None,
        "vcodec": [],
        "range_": [],
        "best_available": False,
        "no_cache": False,
        "reset_cache": False,
    }
    parent_ctx.obj = ctx_data

    # Child context for the service subcommand
    child_cmd = click.Command(service_tag)
    child_ctx = click.Context(child_cmd, parent=parent_ctx, info_name=service_tag)
    child_ctx.params = {"title": title_id}
    child_ctx.obj = ctx_data

    return child_ctx


def _instantiate_service(service_tag: str, title_id: str):
    """Load and instantiate a service class, with auth."""
    from unshackle.core.services import Services
    from unshackle.commands.dl import dl

    service_cls = Services.load(service_tag)
    ctx = _build_context(service_tag, title_id)

    # Instantiate — Service.__init__ does geofence check etc.
    instance = service_cls(ctx)

    # Authenticate (load cookies + credentials from config)
    cookies = dl.get_cookie_jar(service_tag, None)
    credential = dl.get_credentials(service_tag, None)
    instance.authenticate(cookies, credential)

    return instance


async def search_service(service_tag: str, query: str) -> list[dict]:
    """
    Instantiate service, call .search(), return list of result dicts.
    Runs in executor to avoid blocking the event loop.
    """
    def _run():
        from unshackle.core.services import Services
        tag = Services.get_tag(service_tag)
        instance = _instantiate_service(tag, query)
        results = []
        for r in instance.search():
            results.append({
                "id": str(r.id),
                "title": r.title,
                "description": r.description or "",
                "label": r.label or "",
                "url": r.url or "",
                "year": "",
            })
        return results

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def list_titles(service_tag: str, content_id: str) -> list[dict]:
    """
    Instantiate service, call .get_titles(), return structured list.
    """
    def _run():
        from unshackle.core.services import Services
        from unshackle.core.titles.episode import Episode, Series
        from unshackle.core.titles.movie import Movie, Movies
        from unshackle.core.titles.song import Song, Album

        tag = Services.get_tag(service_tag)
        instance = _instantiate_service(tag, content_id)
        titles_obj = instance.get_titles()

        results = []

        if isinstance(titles_obj, (Series, Movies, Album)):
            items = list(titles_obj)
        else:
            items = [titles_obj] if titles_obj else []

        for i, t in enumerate(items, 1):
            if isinstance(t, Episode):
                try:
                    season = int(t.season) if t.season else 0
                    number = int(t.number) if t.number else 0
                    ep_id = f"S{season:02d}E{number:02d}"
                except (TypeError, ValueError):
                    ep_id = f"{t.season or 0}x{t.number or 0}"

                results.append({
                    "index": i,
                    "type": "episode",
                    "episode_id": ep_id,
                    "title": t.name or t.title or "",
                    "show_title": t.title or "",
                    "season": t.season,
                    "number": t.number,
                    "id": str(t.id),
                    "service_data": str(t.id),
                })
            elif isinstance(t, Movie):
                results.append({
                    "index": i,
                    "type": "movie",
                    "episode_id": "",
                    "title": t.name or str(t.id),
                    "year": str(t.year) if t.year else "",
                    "id": str(t.id),
                    "service_data": str(t.id),
                })
            elif isinstance(t, Song):
                results.append({
                    "index": i,
                    "type": "song",
                    "episode_id": "",
                    "title": getattr(t, "name", str(t.id)),
                    "id": str(t.id),
                    "service_data": str(t.id),
                })
            else:
                results.append({
                    "index": i,
                    "type": "unknown",
                    "episode_id": "",
                    "title": str(t),
                    "id": str(getattr(t, "id", i)),
                    "service_data": str(getattr(t, "id", i)),
                })

        return results

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)
