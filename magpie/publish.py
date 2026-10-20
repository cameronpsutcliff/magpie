"""Compose a day's edition from built events, then publish it: day file, latest, index, history.

Shared by the daily run and the backfill, so both produce the same schema.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

from . import build as B
from .util import ROOT, iso, load_json, now_utc, write_json

DATA = ROOT / "data/v1"
STATE = ROOT / "state"
FORBIDDEN = [re.compile(p, re.I) for p in (r"/Users/", r"/home/[a-z]", r"sk-ant-[A-Za-z0-9]", r"ghp_[A-Za-z0-9]{20}",
                                            r"github_pat_[A-Za-z0-9_]{20}", r"xai-[A-Za-z0-9]{20}")]
# Machine names, internal hosts and folder names never ship in the repo either: list them, one regex
# per line, in the untracked file .redact.local on the machine that runs the home tier.
_local = ROOT / ".redact.local"
if _local.exists():
    FORBIDDEN += [re.compile(l.strip(), re.I) for l in _local.read_text().splitlines() if l.strip() and not l.startswith("#")]


def scrub(obj) -> None:
    text = json.dumps(obj, ensure_ascii=False)
    hits = [p.pattern for p in FORBIDDEN if p.search(text)]
    if hits:
        raise SystemExit(f"redaction gate blocked publish: {hits}")


def watch_counts() -> dict:
    """What Magpie watches, as a reader would count it: feeds, APIs, channels and accounts."""
    reg = load_json(ROOT / "config/sources.json", {})
    feeds = len(reg.get("feeds", []))
    apis = len([a for a in reg.get("apis", []) if str(a.get("url", "")).startswith("http")])
    from .ytintel import roster as yt_roster
    yt = len(yt_roster()) or len(load_json(ROOT / "config/youtube.json", {}).get("channels", []))
    from .collectors.inbox import roster
    x = len(roster())
    return {"feeds": feeds, "apis": apis, "youtube_channels": yt, "x_accounts": x, "total": feeds + apis + yt + x}


def compose_day(date: str, items: list[dict], rising: dict, health: list[dict], events: list[dict], edition: dict,
                receipt: dict, *, where: str, seconds: float, note: str | None = None) -> tuple[dict, list[dict]]:
    top = B.rank_and_select(events)
    topics_cfg = load_json(ROOT / "config/topics.json", {}).get("topics", {})
    by_id = {e["id"]: e for e in events}
    top_events = [by_id[i] for i in top]
    ok = sum(h["ok"] for h in health)
    if not edition:
        heads = [e["title"].split("/")[-1] for e in top_events[:3]]
        edition = {"headline": (", ".join(heads) + (", and more" if len(top_events) > 3 else "")) if heads else "A quiet day",
                   "dek": f"{len(events)} capability events from {ok} sources. "
                          "Ranked by code; the editor did not run this edition, so copy is the sources' own words.",
                   "quiet": len(top_events) < 3}
    words = len(" ".join([edition.get("headline", ""), edition.get("dek", "")] + [
        " ".join(str((e.get("brief") or {}).get(k, "")) for k in ("headline", "newly_possible", "why", "try")) for e in top_events]).split())
    edition["read_minutes"] = max(1, round(words / 200 + 1.5))
    if note:
        edition["note"] = note
    if where == "backfill":
        edition["backfilled"] = True

    for e in events:
        e.pop("_context", None)
    facets = {
        "vendors": Counter(e.get("maker") or "Open source" for e in events).most_common(),
        "ecosystem": Counter(v for e in events for v in e["vendors"][1:]).most_common(),
        "topics": Counter(t for e in events for t in e["topics"]).most_common(),
        "kinds": Counter(e["kind"] for e in events).most_common(),
        "evidence": Counter(e["evidence"] for e in events).most_common(),
    }
    day = {
        "schema": "magpie.day/1",
        "date": date,
        "generated_at": iso(now_utc()),
        "run": {"where": where, "editor": receipt.get("lane"), "enriched": receipt.get("enriched", 0), "seconds": round(seconds, 1)},
        "edition": edition,
        "coverage": {"sources": health, "sources_ok": ok, "sources_total": len(health), "items": len(items), "events": len(events),
                     "watch": watch_counts()},
        "top": top,
        "events": events[:260],
        "rising": rising | {"new": [
            {"repo": i["title"], "description": i["summary"], "language": i["extra"].get("language"),
             "stars_total": i["metrics"].get("stars_total"), "created_at": i["extra"].get("created_at"),
             "ai": i["extra"].get("ai")} for i in items if i["source_id"] == "github-new-rising"]},
        "updates": B.updates_calendar(items),
        "facets": facets,
        "graph": B.ripple_graph(top_events + [e for e in events if e["id"] not in top and e.get("brief")], topics_cfg),
        "topics": {k: v["label"] for k, v in topics_cfg.items()},
        "hats": load_json(ROOT / "config/hats.json", {}).get("hats", []),
    }
    from .rising import enrich as enrich_rising
    day["rising"] = enrich_rising(day["rising"], events, STATE / "readme", fetch=where != "backfill")
    return day, top_events


def publish_day(day: dict, events: list[dict], top_events: list[dict], *, force: bool = False) -> bool:
    """Write the day; move `latest` only forward; record first sightings. Returns False if it kept a richer edition."""
    scrub(day)
    date = day["date"]
    day_path = DATA / "days" / f"{date}.json"
    prior = load_json(day_path, None)
    if prior and prior.get("run", {}).get("enriched") and not day["run"]["enriched"] and not force:
        print(f"kept the enriched edition already published for {date}", file=sys.stderr)
        return False
    write_json(day_path, day, compact=True)

    index = load_json(DATA / "index.json", {"schema": "magpie.index/1", "days": []})
    if date >= index.get("latest", ""):
        write_json(DATA / "latest.json", day, compact=True)

    # History: earliest sighting per capability, latest sighting, and the last day it led the page.
    history = load_json(STATE / "history.json", {"keys": {}})
    keys = history.setdefault("keys", {})
    top_ids = {e["id"] for e in top_events}
    for e in events:
        k = keys.setdefault(e["key"], {"first_seen": date, "title": e["title"]})
        k["first_seen"] = min(k.get("first_seen", date), date)
        k["last_seen"] = max(k.get("last_seen", date), date)
        if e["id"] in top_ids:
            k["featured"] = max(k.get("featured", date), date)
    history["updated"] = max(history.get("updated", date), date)
    write_json(STATE / "history.json", history, compact=True)

    index["days"] = [d for d in index["days"] if d["date"] != date]
    index["days"].append(index_entry(day))
    index["days"].sort(key=lambda d: d["date"], reverse=True)
    index["latest"] = index["days"][0]["date"]
    write_json(DATA / "index.json", index, compact=False)
    print(f"published {date}: {len(events)} events, top {len(top_events)}, {day['run']['seconds']}s", file=sys.stderr)
    return True


def index_entry(day: dict) -> dict:
    by_id = {e["id"]: e for e in day["events"]}
    top_events = [by_id[i] for i in day["top"] if i in by_id]
    edition = day["edition"]
    return {
        "date": day["date"], "headline": edition.get("headline"), "events": day["coverage"]["events"],
        "sources": (day["coverage"].get("watch") or {}).get("total") or day["coverage"]["sources_total"],
        "enriched": bool(day["run"]["enriched"]),
        **({"backfilled": True} if edition.get("backfilled") else {}),
        "top": [{"id": e["id"], "title": (e.get("brief") or {}).get("headline") or e["title"], "impact": e["impact"],
                 "vendor": e.get("maker")} for e in top_events],
        "vendors": [v for v, _ in day["facets"]["vendors"][:6]],
        # Per-topic and per-source counts give every later day a "typical day" to compare against.
        "topics": dict(day["facets"].get("topics", [])),
        "kinds": dict(day["facets"].get("kinds", [])),
    }


def reindex() -> None:
    """Rebuild index.json and latest.json from the day files (used after merging branches)."""
    days = sorted((load_json(p, None) for p in (DATA / "days").glob("*.json")), key=lambda d: d["date"], reverse=True)
    days = [d for d in days if d]
    index = {"schema": "magpie.index/1", "days": [index_entry(d) for d in days], "latest": days[0]["date"] if days else None}
    write_json(DATA / "index.json", index, compact=False)
    if days:
        write_json(DATA / "latest.json", days[0], compact=True)
    print(f"reindexed {len(days)} editions; latest {index['latest']}", file=sys.stderr)
