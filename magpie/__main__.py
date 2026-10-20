"""magpie: collect, build, enrich and publish one daily edition.

    python -m magpie run              # full run (local collectors and the editor when available)
    python -m magpie run --no-llm     # deterministic edition only
    python -m magpie run --cloud      # skip collectors that need the owner's machine
    python -m magpie discover         # weekly: grow and prune the watch list (see discover.py)
    python -m magpie review --week    # weekly: quality report on the week's editions (see review.py)
    python -m magpie constraints      # add the constraints lane and trackers to a published edition
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import sys
import time
import traceback

from . import build as B
from . import discover, review
from .collectors import feeds, github, inbox, local, policy, web
from .enrich import enrich
from .util import ROOT, edition_date, iso, load_json, now_utc, write_json

from .publish import DATA, STATE, compose_day, publish_day, scrub  # noqa: F401  (scrub re-exported for tests)


def collect(cloud: bool) -> tuple[list, dict, list]:
    reg = load_json(ROOT / "config/sources.json", {})
    jobs = {
        "github-trending": github.trending,
        "github-new-rising": github.new_and_rising,
        "hacker-news": web.hacker_news,
        "hf-trending-models": web.hf_models,
        "hf-trending-spaces": web.hf_spaces,
        "hf-daily-papers": web.hf_papers,
        "x-grok-bot": inbox.x_from_grok_bot,
        "federal-register": policy.federal_register,
    }
    for f in reg.get("feeds", []):
        jobs[f["id"]] = (lambda src=f: feeds.collect_feed(src))
    for f in reg.get("policy_feeds", []):
        jobs[f["id"]] = (lambda src=f: policy.policy_feed(src))
    orgs = [o["org"] for o in reg.get("github_orgs", [])]
    if orgs:
        jobs["github-watched-orgs"] = lambda: github.watched_orgs(orgs)
    if not cloud:
        jobs["youtube-watchlist"] = local.youtube  # the brief written by `magpie youtube`, the one YouTube job
        jobs["x-saved"] = inbox.x_saved
    listed = reg.get("feeds", []) + reg.get("apis", []) + reg.get("policy_feeds", [])
    names = {s["id"]: s["name"] for s in listed}
    kinds = {s["id"]: s["kind"] for s in listed}

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


def main() -> None:
    ap = argparse.ArgumentParser(prog="magpie")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--cloud", action="store_true", help="skip collectors that only exist on the owner's machine")
    r.add_argument("--force", action="store_true", help="overwrite an enriched edition with a plain one")
    r.add_argument("--items", help="reuse a saved items file instead of collecting")
    r.add_argument("--reuse-editor", action="store_true", help="apply today's saved editor output instead of calling a model")
    sub.add_parser("reindex", help="rebuild index.json and latest.json from the day files")
    pz = sub.add_parser("possibilities", help="combine the day's capabilities into new possibilities")
    pz.add_argument("--date", help="edition date, default latest")
    pr = sub.add_parser("rising", help="attach README openings and buzz to the rising repos")
    pr.add_argument("--date", help="edition date, default latest")
    py = sub.add_parser("youtube", help="find, transcribe and brief the day's videos (the one YouTube job)")
    py.add_argument("--hours", type=int, help="lookback window, default 36 (MAGPIE_YT_LOOKBACK_HOURS)")
    pv = sub.add_parser("viewing", help="pick the videos worth watching from their transcripts")
    pv.add_argument("--date", help="edition date, default latest")
    pk = sub.add_parser("constraints", help="add official actions that constrain AI, and the trackers, to an edition")
    pk.add_argument("--date", help="edition date, default latest")
    bf = sub.add_parser("backfill", help="rebuild past editions from archives")
    bf.add_argument("--from", dest="start", required=True, help="first edition date, YYYY-MM-DD")
    bf.add_argument("--to", dest="end", required=True, help="last edition date, YYYY-MM-DD")
    bf.add_argument("--no-llm", action="store_true")
    bf.add_argument("--recollect", action="store_true", help="ignore cached archive pulls")
    d = sub.add_parser("discover", help="weekly: promote and demote watch-list sources by explicit rules")
    d.add_argument("--dry-run", action="store_true", help="print the change log without writing anything")
    d.add_argument("--offline", action="store_true", help="do not fetch candidate newsletter feeds (holds them)")
    d.add_argument("--veto", action="append", metavar="ID", help="undo a change and remember it, e.g. x:@handle")
    d.add_argument("--date", help="week ending on this date (default: today's edition date)")
    v = sub.add_parser("review", help="weekly: quality report on a week of editions")
    v.add_argument("--week", nargs="?", const="latest", default="latest",
                   help="YYYY-Www or a date inside the week (default: the week of the latest edition)")
    a = ap.parse_args()
    if a.cmd == "possibilities":
        from .possibilities import run_for
        run_for(a.date)
        return
    if a.cmd == "rising":
        from .rising import run_for as rising_for
        rising_for(a.date)
        return
    if a.cmd == "youtube":
        from .ytintel import run as youtube_run
        youtube_run(a.hours)
        return
    if a.cmd == "viewing":
        from .viewing import run_for as viewing_for
        viewing_for(a.date)
        return
    if a.cmd == "constraints":
        from .constraints import run_for as constraints_for
        constraints_for(a.date)
        return
    if a.cmd == "reindex":
        from .publish import reindex
        reindex()
        return
    if a.cmd == "backfill":
        from .backfill import run as backfill_run
        backfill_run(a.start, a.end, llm=not a.no_llm, recollect=a.recollect)
        return
    if a.cmd == "discover":
        return discover.main(a)
    if a.cmd == "review":
        return review.main(a)

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

    # Constraints: official actions that make AI harder to train, publish, host, reach or buy.
    from . import constraints as K, trackers as T
    try:
        c_events, c_section = K.generate(items, date, DATA, STATE, llm=not a.no_llm)
        used = {s["url"] for r in c_section["items"] for s in r.get("sources", [])}
        # A post that only reports the same action leaves the capability list; the constraint carries it.
        events = [e for e in events if not (e["sources"] and all(s["url"] in used for s in e["sources"]))] + c_events
    except Exception as ex:  # the lane must never block the edition
        c_section = {"items": [], "errors": [f"{type(ex).__name__}: {str(ex)[:160]}"]}
    print(f"constraints: {len(c_section['items'])} ({c_section.get('lane') or c_section.get('errors')})", file=sys.stderr)
    trackers = T.build(date, DATA, STATE)

    day, top_events = compose_day(date, items, rising, health, events, edition, receipt,
                                  where="cloud" if a.cloud else "home", seconds=time.monotonic() - t0,
                                  constraints=c_section, trackers=trackers)
    if receipt.get("enriched"):
        from .possibilities import generate, prior_days
        pcache = run_dir / "possibilities.json"
        if a.reuse_editor and pcache.exists():
            day["possibilities"] = load_json(pcache, {})
        else:
            try:
                day["possibilities"] = generate(day, prior_days(date, DATA))
                write_json(pcache, day["possibilities"])
            except Exception as ex:  # an idea pass must never block the edition
                day["possibilities"] = {"items": [], "errors": [f"{type(ex).__name__}: {str(ex)[:160]}"]}
        print(f"possibilities: {len(day['possibilities'].get('items', []))}", file=sys.stderr)
        try:
            from .viewing import generate as pick_viewing
            day["viewing"] = pick_viewing(date, items, DATA, STATE)
        except Exception as ex:  # viewing must never block the edition
            day["viewing"] = {"items": [], "errors": [f"{type(ex).__name__}: {str(ex)[:160]}"]}
        print(f"viewing: {len(day['viewing'].get('items', []))}", file=sys.stderr)
    publish_day(day, events, top_events, force=a.force)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
