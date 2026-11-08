"""Each edition item's own page on the site, stamped into the day as `page_url` so anything reading the feed can link to it.

- A capability (every entry of `events`, the day's five leads among them): /magpie/c/<date>-<event id>. The event id is
  a hash of the capability's canonical key (build.py), so the same capability has the same event id on every day it
  appears, and each day's sighting has its own page. Neither part depends on rank, on later editions or on the editor's
  words, so the link never moves: tomorrow's edition leaves it alone, and a re-edited edition keeps it as long as the
  capability keeps its key.
- A new possibility: /magpie/i/<id>, the id views.assign_ids gave it.
- recent.possibilities: an earlier possibility with an id links its own page.

A pure function of stored fields: no model call, nothing fetched. A field that is not a safe id gets no page_url, and a
stale page_url is removed.
"""
from __future__ import annotations

import re

SITE = "https://www.cameronsutcliff.com/magpie"
EVENT_ID = re.compile(r"[0-9a-f]{12}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
IDEA_ID = re.compile(r"\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,99}")  # views.ID_SHAPE


def item_id(date, event_id) -> str | None:
    """A capability's page id on one edition: the edition's date, a hyphen and its event id."""
    if isinstance(date, str) and DATE.fullmatch(date) and isinstance(event_id, str) and EVENT_ID.fullmatch(event_id):
        return f"{date}-{event_id}"
    return None


def item_url(date, event_id) -> str | None:
    i = item_id(date, event_id)
    return f"{SITE}/c/{i}" if i else None


def idea_url(idea_id, date=None) -> str | None:
    """An idea's page, when its id is a safe page id (and starts with its edition's date, when that is given)."""
    if not isinstance(idea_id, str) or not IDEA_ID.fullmatch(idea_id):
        return None
    if date is not None and not idea_id.startswith(f"{date}-"):
        return None
    return f"{SITE}/i/{idea_id}"


def _set(obj: dict, url: str | None) -> int:
    if url:
        if obj.get("page_url") == url:
            return 0
        obj["page_url"] = url
        return 1
    if "page_url" in obj:
        del obj["page_url"]
        return 1
    return 0


def stamp(day: dict) -> int:
    """Set page_url in place on the day's capabilities, its possibilities and its recent possibilities; returns how many
    changed."""
    if not isinstance(day, dict):
        return 0
    date = day.get("date")
    n = 0
    for e in day.get("events") or []:
        if isinstance(e, dict):
            n += _set(e, item_url(date, e.get("id")))
    poss = day.get("possibilities")
    for p in (poss.get("items") if isinstance(poss, dict) else None) or []:
        if isinstance(p, dict):
            n += _set(p, idea_url(p.get("id"), date))
    recent = day.get("recent")
    for r in (recent.get("possibilities") if isinstance(recent, dict) else None) or []:
        if isinstance(r, dict):
            n += _set(r, idea_url(r.get("id"), r.get("date")))
    return n
