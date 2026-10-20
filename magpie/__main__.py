"""magpie: collect, build, enrich and publish one daily edition.

    python -m magpie run              # full run (local collectors and the editor when available)
    python -m magpie run --no-llm     # deterministic edition only
    python -m magpie run --cloud      # skip collectors that need the owner's machine
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import traceback

from . import build as B
from .collectors import feeds, github, local, web
from .enrich import enrich
from .util import ROOT, edition_date, iso, load_json, now_utc, write_json

DATA = ROOT / "data/v1"
STATE = ROOT / "state"
FORBIDDEN = [re.compile(p, re.I) for p in (r"/Users/", r"/home/[a-z]", r"sk-ant-[A-Za-z0-9]", r"ghp_[A-Za-z0-9]{20}",
                                            r"github_pat_[A-Za-z0-9_]{20}", r"xai-[A-Za-z0-9]{20}")]
# Machine names, internal hosts and folder names never ship in the repo either: list them, one regex
# per line, in the untracked file .redact.local on the machine that runs the home tier.
_local = ROOT / ".redact.local"
if _local.exists():
    FORBIDDEN += [re.compile(l.strip(), re.I) for l in _local.read_text().splitlines() if l.strip() and not l.startswith("#")]


def collect(cloud: bool) -> tuple[list, dict, list]:
    reg = load_json(ROOT / "config/sources.json", {})
    jobs = {
        "github-trending": github.trending,
        "github-new-rising": github.new_and_rising,
        "hacker-news": web.hacker_news,
        "hf-trending-models": web.hf_models,
        "hf-trending-spaces": web.hf_spaces,
        "hf-daily-papers": web.hf_papers,
    }
    for f in reg.get("feeds", []):
        jobs[f["id"]] = (lambda src=f: feeds.collect_feed(src))
    if not cloud:
        jobs["youtube-watchlist"] = local.youtube
    names = {s["id"]: s["name"] for s in reg.get("feeds", []) + reg.get("apis", [])}
    kinds = {s["id"]: s["kind"] for s in reg.get("feeds", []) + reg.get("apis", [])}

    items, rising, health = [], {}, []

    def run(sid, fn):
        t = time.monotonic()
        try:
            res = fn()
            return sid, res, None, time.monotonic() - t
        except Exception as ex:
            return sid, None, f"{type(ex).__name__}: {str(ex)[:160]}", time.monotonic() - t

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(run, sid, fn) for sid, fn in jobs.items()]
        # X takes minutes; run it alongside everything else.
        xfut = None if cloud else pool.submit(run, "x-grok-pulse", local.x_pulse)
        for fu in cf.as_completed(futs + ([xfut] if xfut else [])):
            sid, res, err, secs = fu.result()
            if sid == "github-trending" and res:
                res, rising = res
            got = [i.to_dict() for i in (res or [])]
            items += got
            health.append({"id": sid, "name": names.get(sid, sid), "kind": kinds.get(sid, "other"),
                           "ok": err is None, "count": len(got), "seconds": round(secs, 1),
                           **({"error": err} if err else {})})
    health.sort(key=lambda h: (not h["ok"], h["id"]))
    return items, rising, health


def scrub(obj):
    text = json.dumps(obj, ensure_ascii=False)
    hits = [p.pattern for p in FORBIDDEN if p.search(text)]
    if hits:
        raise SystemExit(f"redaction gate blocked publish: {hits}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="magpie")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--cloud", action="store_true", help="skip collectors that only exist on the owner's machine")
    r.add_argument("--force", action="store_true", help="overwrite an enriched edition with a plain one")
    r.add_argument("--items", help="reuse a saved items file instead of collecting")
    r.add_argument("--reuse-editor", action="store_true", help="apply today's saved editor output instead of calling a model")
    a = ap.parse_args()

    t0 = time.monotonic()
    date = edition_date()
    run_dir = STATE / "runs" / date
    run_dir.mkdir(parents=True, exist_ok=True)

    if a.items:
        saved = load_json(ROOT / a.items, {})
        items, rising, health = saved["items"], saved["rising"], saved["health"]
    else:
        items, rising, health = collect(a.cloud)
        write_json(run_dir / "items.json", {"items": items, "rising": rising, "health": health}, compact=True)
    print(f"collected {len(items)} items from {sum(h['ok'] for h in health)}/{len(health)} sources", file=sys.stderr)

    history = load_json(STATE / "history.json", {"keys": {}})
    built = B.build(items, history, date)
    events = built["events"]

    shortlist = [e["key"].split(":", 1)[1] for e in events[:40] if e["key"].startswith("gh:")][:25]
    meta = github.repo_details(shortlist)
    if meta:
        built = B.build(items, history, date, meta)
        events = built["events"]

    edition, receipt = {}, {"lane": None, "enriched": 0, "errors": ["skipped"]}
    cache = run_dir / "editor.json"
    if a.reuse_editor and cache.exists():
        saved = load_json(cache, {})
        briefs, dropped = saved.get("briefs", {}), set(saved.get("dropped", []))
        for e in events:
            if e["key"] in dropped:
                e["dropped"] = True
            elif e["key"] in briefs:
                e["brief"] = briefs[e["key"]]["brief"]
                e["topics"] = briefs[e["key"]].get("topics") or e["topics"]
                e["vendors"] = briefs[e["key"]].get("vendors") or e["vendors"]
                e["maker"] = briefs[e["key"]].get("maker") or e["maker"]
        edition, receipt = saved.get("edition", {}), saved.get("receipt", receipt)
    elif not a.no_llm:
        edition, receipt = enrich(events, date)
        write_json(cache, {"edition": edition, "receipt": receipt,
                           "dropped": [e["key"] for e in events if e.get("dropped")],
                           "briefs": {e["key"]: {"brief": e["brief"], "topics": e["topics"], "vendors": e["vendors"], "maker": e.get("maker")}
                                      for e in events if e.get("brief")}})
    events = [e for e in events if not e.get("dropped")]
    print(f"editor: {receipt}", file=sys.stderr)

    top = B.rank_and_select(events)
    topics_cfg = load_json(ROOT / "config/topics.json", {}).get("topics", {})
    by_id = {e["id"]: e for e in events}
    top_events = [by_id[i] for i in top]
    if not edition:
        heads = [e["title"].split("/")[-1] for e in top_events[:3]]
        edition = {"headline": (", ".join(heads) + (", and more" if len(top_events) > 3 else "")) if heads else "A quiet day",
                   "dek": f"{len(events)} capability events from {sum(h['ok'] for h in health)} sources. "
                          "Ranked by code; the editor did not run this edition, so copy is the sources' own words.",
                   "quiet": len(top_events) < 3}

    words = len(" ".join([edition.get("headline", ""), edition.get("dek", "")] + [
        " ".join(str((e.get("brief") or {}).get(k, "")) for k in ("headline", "newly_possible", "why", "try")) for e in top_events]).split())
    edition["read_minutes"] = max(1, round(words / 200 + 1.5))

    for e in events:
        e.pop("_context", None)
    facets = {
        "vendors": B.Counter(e.get("maker") or "Open source" for e in events).most_common(),
        "ecosystem": B.Counter(v for e in events for v in e["vendors"][1:]).most_common(),
        "topics": B.Counter(t for e in events for t in e["topics"]).most_common(),
        "kinds": B.Counter(e["kind"] for e in events).most_common(),
        "evidence": B.Counter(e["evidence"] for e in events).most_common(),
    }
    day = {
        "schema": "magpie.day/1",
        "date": date,
        "generated_at": iso(now_utc()),
        "run": {"where": "cloud" if a.cloud else "home", "editor": receipt["lane"], "enriched": receipt["enriched"],
                "seconds": round(time.monotonic() - t0, 1)},
        "edition": edition,
        "coverage": {"sources": health, "sources_ok": sum(h["ok"] for h in health), "sources_total": len(health),
                     "items": len(items), "events": len(events)},
        "top": top,
        "events": events[:260],
        "rising": rising | {"new": [
            {"repo": i["title"], "description": i["summary"], "language": i["extra"].get("language"),
             "stars_total": i["metrics"].get("stars_total"), "created_at": i["extra"].get("created_at"),
             "ai": i["extra"].get("ai")} for i in items if i["source_id"] == "github-new-rising"]},
        "updates": B.updates_calendar(items),
        "facets": facets,
        "graph": B.ripple_graph([by_id[i] for i in top] + [e for e in events if e["id"] not in top and e.get("brief")], topics_cfg),
        "topics": {k: v["label"] for k, v in topics_cfg.items()},
        "hats": load_json(ROOT / "config/hats.json", {}).get("hats", []),
    }
    scrub(day)

    day_path = DATA / "days" / f"{date}.json"
    prior = load_json(day_path, None)
    if prior and prior.get("run", {}).get("enriched") and not receipt["enriched"] and not a.force:
        print("kept the enriched edition already published for today", file=sys.stderr)
        return
    write_json(day_path, day, compact=True)
    write_json(DATA / "latest.json", day, compact=True)

    # History: first sighting per capability, and the last day it led the page.
    keys = history.setdefault("keys", {})
    for e in events:
        k = keys.setdefault(e["key"], {"first_seen": date, "title": e["title"]})
        k["last_seen"] = date
        if e["id"] in top:
            k["featured"] = date
    history["updated"] = date
    write_json(STATE / "history.json", history, compact=True)

    index = load_json(DATA / "index.json", {"schema": "magpie.index/1", "days": []})
    index["days"] = [d for d in index["days"] if d["date"] != date]
    index["days"].append({
        "date": date, "headline": edition.get("headline"), "events": len(events),
        "enriched": bool(receipt["enriched"]),
        "top": [{"id": e["id"], "title": (e.get("brief") or {}).get("headline") or e["title"], "impact": e["impact"],
                 "vendor": e.get("maker")} for e in top_events],
        "vendors": [v for v, _ in facets["vendors"][:6]],
    })
    index["days"].sort(key=lambda d: d["date"], reverse=True)
    index["latest"] = index["days"][0]["date"]
    write_json(DATA / "index.json", index, compact=False)
    print(f"published {date}: {len(events)} events, top {len(top)}, {day['run']['seconds']}s", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
