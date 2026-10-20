"""X posts delivered by Grok Bot: files the Bot's routine commits to inbox/x/.

Grok Bot runs the X sweeps on its own cloud computer (see docs/grok-bot.md) and pushes one JSON file
per run. Every Magpie run, cloud or home, reads the files from the last 30 hours.
"""
from __future__ import annotations

import datetime as dt
import json

from ..items import Item
from ..util import ROOT, clean, now_utc, parse_time

INBOX = ROOT / "inbox/x"


def x_from_grok_bot(hours: int = 30) -> list[Item]:
    if not INBOX.is_dir():
        return []
    cutoff = now_utc() - dt.timedelta(hours=hours)
    out, seen = [], set()
    for f in sorted(INBOX.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        ran = parse_time(data.get("ran_at"))
        if ran and ran < cutoff:
            continue
        for x in data.get("items", []):
            url = x.get("url") or ""
            if not url.startswith("https://") or url in seen:
                continue
            seen.add(url)
            links = " ".join(x.get("links") or [])
            it = Item(
                source_id="x-grok-bot",
                source_kind="social",
                source_name=f"X: {x.get('handle', '')}".strip(),
                url=url,
                title=clean(x.get("title", ""))[:200],
                summary=clean(x.get("summary", ""))[:400],
                published_at=x.get("posted_at"),
                author=x.get("handle"),
                vendor_hint=x.get("vendor"),
                trust=0.65,
                extra={"links": x.get("links") or [], "x_group": x.get("group")},
            ).finalize(links)
            if it.mentions:
                it.keys = it.mentions[:1] + it.keys
                it.mentions = it.mentions[1:]
            out.append(it)
    return out
