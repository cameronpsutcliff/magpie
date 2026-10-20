"""X posts delivered by Grok Bot (Magpie Scout): files the Bot's routines commit under inbox/.

Grok Bot runs the X sweeps on its own cloud computer (see docs/grok-bot.md) and pushes one JSON file
per run to inbox/x/. It also keeps inbox/x-roster.json, the players it has added, promoted or retired,
and inbox/x-status.json. Every Magpie run, cloud or home, reads sweep files from the last 30 hours.
Doubt the Bot records (a Community Note, credible pushback) lowers trust and travels with the item, so
the editor and the score both see it.
"""
from __future__ import annotations

import datetime as dt
import json

from ..items import Item
from ..util import ROOT, clean, load_json, now_utc, parse_time

INBOX = ROOT / "inbox/x"
ROSTER = ROOT / "inbox/x-roster.json"
TRUST = {"artifact": 0.7, "measured": 0.68, "claim": 0.55}


def roster() -> list[dict]:
    """The accounts to sweep: the curated roster, plus the Scout's trial and active additions, minus its retirements."""
    base = load_json(ROOT / "config/x-accounts.json", {}).get("accounts", [])
    scout = load_json(ROSTER, {}).get("accounts", [])
    retired = {a["handle"].lower() for a in scout if a.get("status") == "retired" and a.get("handle")}
    out, seen = [], set()
    for a in base + [a for a in scout if a.get("status") in ("trial", "active")]:
        h = (a.get("handle") or "").lower()
        if h and h not in retired and h not in seen:
            seen.add(h)
            out.append(a)
    return out


def _doubt(x: dict) -> dict | None:
    d = x.get("doubt") or {}
    try:
        level = max(0, min(3, int(d.get("level") or 0)))
    except (TypeError, ValueError):
        level = 0
    note = clean(d.get("community_note") or "")
    push = clean(d.get("pushback") or "")
    if note:
        level = max(level, 2)
    if not level:
        return None
    return {"level": level, "note": (f"Community Note: {note}" if note else push)[:300], "url": d.get("source")}


def x_from_grok_bot(hours: int = 30) -> list[Item]:
    if not INBOX.is_dir():
        return []
    cutoff = now_utc() - dt.timedelta(hours=hours)
    out, seen, newest = [], set(), None
    files = sorted(INBOX.glob("*.json"), reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        ran = parse_time(data.get("ran_at"))
        if ran and (newest is None or ran > newest):
            newest = ran
        if ran and ran < cutoff:
            continue
        for x in data.get("items", []):
            url = x.get("url") or ""
            if not url.startswith("https://") or url in seen:
                continue
            seen.add(url)  # newest file first, so a later re-check of the same post wins
            doubt = _doubt(x)
            trust = TRUST.get(x.get("evidence"), 0.6)
            summary = clean(x.get("summary", ""))
            if doubt:
                trust = min(trust, 0.55 if doubt["level"] == 1 else 0.4)
                if doubt["level"] >= 2 and doubt["note"]:
                    summary = f"{summary} Flagged on X: {doubt['note']}".strip()
            links = " ".join(x.get("links") or [])
            it = Item(
                source_id="x-grok-bot",
                source_kind="social",
                source_name=f"X: {x.get('handle', '')}".strip(),
                url=url,
                title=clean(x.get("title", ""))[:200],
                summary=summary[:500],
                published_at=x.get("posted_at"),
                author=x.get("handle"),
                vendor_hint=x.get("vendor"),
                trust=trust,
                extra={"links": x.get("links") or [], "x_group": x.get("group"), "x_kind": x.get("kind"),
                       "x_metrics": x.get("metrics") or {}, "confirmed_by": x.get("confirmed_by") or [], "doubt": doubt},
            ).finalize(links)
            if it.mentions:
                it.keys = it.mentions[:1] + it.keys
                it.mentions = it.mentions[1:]
            out.append(it)
    if not out and files and newest and newest < now_utc() - dt.timedelta(hours=12):
        # The Bot is set up but has gone quiet: show it as down in source health, not as a quiet day.
        raise RuntimeError(f"Magpie Scout has not delivered since {newest.isoformat(timespec='minutes')}")
    return out
