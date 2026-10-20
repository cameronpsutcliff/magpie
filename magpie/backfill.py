"""Rebuild past editions from archives, so the calendar has history from day one.

Each past edition D uses only what was knowable on the morning of D:
- the archived YouTube Intel Brief for D-1 (the owner's watch list, when this machine has it)
- the GitHub trending page as archived early on D (a public repo that commits it daily)
- Hacker News stories posted in the 30 hours before 06:00 Central on D
- Hugging Face daily papers for D-1
- feed entries (newsletters, lab news, changelogs) dated in that window, where feeds still list them

Pulls are cached per date in state/backfill/. Editions are marked as backfilled, and the page says so.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
import urllib.parse

from . import build as B
from .collectors import feeds as F
from .collectors.github import AI_HINT
from .collectors.local import ENTRY, YT_HOME
from .enrich import enrich
from .items import Item
from .publish import STATE, compose_day, publish_day
from .util import CENTRAL, ROOT, clean, fetch, fetch_json, github_headers, load_json, parse_time, strip_html, write_json

CACHE = STATE / "backfill"
NOTE = ("Rebuilt from archives: the YouTube Intel Brief, the archived GitHub trending page, Hacker News, "
        "Hugging Face papers and the feeds that still list that day. X, live star counts and trending models "
        "are not recoverable for past days.")


def _window(date: str) -> tuple[dt.datetime, dt.datetime]:
    end = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(6, 0), CENTRAL)
    return end - dt.timedelta(hours=30), end


def _dates(start: str, end: str) -> list[str]:
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [(a + dt.timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


# ---------- sources by date ----------

def trending_shas(dates: list[str]) -> dict[str, str]:
    """Map date -> commit of the public trending archive that recorded it."""
    since = (dt.date.fromisoformat(min(dates)) - dt.timedelta(days=1)).isoformat()
    until = (dt.date.fromisoformat(max(dates)) + dt.timedelta(days=2)).isoformat()
    out, page = {}, 1
    while page < 10:
        rows = fetch_json(f"https://api.github.com/repos/bonfy/github-trending/commits?since={since}T00:00:00Z"
                          f"&until={until}T00:00:00Z&per_page=100&page={page}", headers=github_headers())
        for r in rows:
            msg = r["commit"]["message"].strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", msg):
                out.setdefault(msg, r["sha"])
        if len(rows) < 100:
            break
        page += 1
    return out


LINE = re.compile(r"^\* \[([^\]/]+?) / ([^\]]+?)\]\(https://github\.com/[^)]+\):?(.*)$")


def trending_for(date: str, sha: str | None) -> tuple[list[Item], list[dict]]:
    if not sha:
        return [], []
    text = fetch(f"https://raw.githubusercontent.com/bonfy/github-trending/{sha}/{date}.md").decode("utf-8", "replace")
    items, rows, lang, rank = [], [], None, 0
    for line in text.splitlines():
        if line.startswith("#### "):
            lang, rank = line[5:].strip(), 0
            continue
        m = LINE.match(line.strip())
        if not m:
            continue
        rank += 1
        repo = f"{m.group(1).strip()}/{m.group(2).strip()}"
        desc = strip_html(m.group(3), 300)
        ai = bool(AI_HINT.search(f"{repo} {desc}"))
        rows.append({"repo": repo, "description": desc, "language": (lang or "").title() or None, "stars_total": None,
                     "stars_window": None, "window": "day", "rank": len(rows) + 1, "ai": ai})
        items.append(Item(source_id="github-trending-daily", source_kind="repo", source_name="GitHub trending (archive)",
                          url=f"https://github.com/{repo}", title=repo, summary=desc, trust=0.7,
                          metrics={"trend_rank_day": rank}, extra={"language": lang, "ai": ai}).finalize())
    return items, rows


def hn_for(date: str) -> list[Item]:
    start, end = _window(date)
    nf = urllib.parse.quote(f"created_at_i>{int(start.timestamp())},created_at_i<{int(end.timestamp())},points>60")
    data = fetch_json(f"https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=200&numericFilters={nf}")
    out = []
    for h in data.get("hits", []):
        title = h.get("title") or ""
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        if not (AI_HINT.search(title) or re.search(r"github\.com|huggingface\.co|arxiv\.org", url)):
            continue
        out.append(Item(source_id="hacker-news", source_kind="community", source_name="Hacker News", url=url,
                        title=strip_html(title), published_at=h.get("created_at"), author=h.get("author"), trust=0.65,
                        metrics={"hn_points": h.get("points"), "hn_comments": h.get("num_comments")},
                        extra={"discussion": f"https://news.ycombinator.com/item?id={h['objectID']}"}).finalize())
    return out


def papers_for(date: str) -> list[Item]:
    day = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    data = fetch_json(f"https://huggingface.co/api/daily_papers?date={day}&limit=50")
    out = []
    for p in data:
        paper = p.get("paper") or {}
        pid = paper.get("id")
        if not pid:
            continue
        it = Item(source_id="hf-daily-papers", source_kind="paper", source_name="Hugging Face daily papers",
                  url=f"https://huggingface.co/papers/{pid}", title=strip_html(paper.get("title") or pid),
                  summary=strip_html(paper.get("ai_summary") or paper.get("summary") or "", 420),
                  published_at=p.get("publishedAt"), trust=0.6,
                  metrics={"hf_upvotes": paper.get("upvotes"), "gh_stars": paper.get("githubStars")},
                  extra={"github": paper.get("githubRepo"), "keywords": (paper.get("ai_keywords") or [])[:10],
                         "org": (paper.get("organization") or {}).get("fullname")})
        it.keys = [f"arxiv:{pid}"]
        out.append(it.finalize(f"{paper.get('githubRepo') or ''} {paper.get('projectPage') or ''}"))
    return out


def youtube_for(date: str) -> list[Item]:
    if YT_HOME is None:
        return []
    prev = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    f = YT_HOME / "briefs" / f"{prev}-brief.md"
    if not f.exists():
        return []
    channels = {c["name"]: c.get("priority", "medium") for c in load_json(YT_HOME / "config/channels.json", [])}
    out = []
    for m in ENTRY.finditer(f.read_text()):
        prio = channels.get(m.group("channel").strip(), "medium")
        out.append(Item(source_id="youtube-watchlist", source_kind="video", source_name=f"YouTube: {m.group('channel').strip()}",
                        url=m.group("url").strip(), title=clean(m.group("title").strip()), author=m.group("channel").strip(),
                        published_at=f"{prev}T18:00:00Z", trust=0.7 if prio == "high" else 0.55,
                        metrics={"duration": m.group("dur").strip()},
                        extra={"private_context": strip_html(m.group("text"), 1400), "channel_priority": prio}).finalize(m.group("text")))
    return out


def feed_entries() -> list[tuple[dict, list[dict]]]:
    """Fetch every configured feed once; entries are filtered per date later."""
    out = []
    for src in load_json(ROOT / "config/sources.json", {}).get("feeds", []):
        try:
            out.append((src, F.parse_feed(fetch(src["url"]))))
        except Exception as ex:
            print(f"  feed {src['id']}: {str(ex)[:80]}", file=sys.stderr)
    return out


def feeds_for(date: str, pulled: list[tuple[dict, list[dict]]]) -> list[Item]:
    start, end = _window(date)
    cal_start = end - dt.timedelta(days=14)
    out = []
    for src, entries in pulled:
        lo = cal_start if src["kind"] in ("changelog", "news") else start
        for e in entries:
            t = parse_time(e["published"])
            if not t or not e["url"] or not (lo <= t < end):
                continue
            out.append(Item(source_id=src["id"], source_kind=src["kind"], source_name=src["name"], url=e["url"],
                            title=strip_html(e["title"], 220), summary=strip_html(e["summary"] or e["body"], 420),
                            published_at=t.isoformat(), author=strip_html(e["author"]) or None,
                            vendor_hint=src.get("vendor"), trust=src.get("trust", 0.6),
                            extra={"product": src.get("product")}).finalize(
                e["body"] if src["kind"] in ("newsletter", "blog", "news", "community") else ""))
    return out


# ---------- the run ----------

def collect_date(date: str, sha: str | None, pulled) -> dict:
    path = CACHE / date / "items.json"
    items, rising, health = [], {}, []
    for sid, name, kind, fn in (
        ("github-trending", "GitHub trending (archive)", "repo", lambda: trending_for(date, sha)),
        ("hacker-news", "Hacker News", "community", lambda: hn_for(date)),
        ("hf-daily-papers", "Hugging Face daily papers", "paper", lambda: papers_for(date)),
        ("youtube-watchlist", "YouTube Intel Brief (archive)", "video", lambda: youtube_for(date)),
        ("feeds", "Newsletters, lab news and changelogs", "news", lambda: feeds_for(date, pulled)),
    ):
        t = time.monotonic()
        try:
            res = fn()
            if sid == "github-trending":
                res, rising["day"] = res
            got = [i.to_dict() for i in res]
            items += got
            health.append({"id": sid, "name": name, "kind": kind, "ok": True, "count": len(got), "seconds": round(time.monotonic() - t, 1)})
        except Exception as ex:
            health.append({"id": sid, "name": name, "kind": kind, "ok": False, "count": 0, "seconds": round(time.monotonic() - t, 1),
                           "error": f"{type(ex).__name__}: {str(ex)[:140]}"})
    saved = {"items": items, "rising": rising, "health": health}
    write_json(path, saved, compact=True)
    return saved


def run(start: str, end: str, *, llm: bool = True, recollect: bool = False) -> None:
    from .publish import DATA

    def is_live(d: str) -> bool:
        day = load_json(DATA / "days" / f"{d}.json", None)
        return bool(day) and day.get("run", {}).get("where") != "backfill"

    # A live edition always beats an archive rebuild; never overwrite one.
    skipped = [d for d in _dates(start, end) if is_live(d)]
    dates = [d for d in _dates(start, end) if d not in skipped]
    if skipped:
        print(f"skipping live editions: {', '.join(skipped)}", file=sys.stderr)
    if not dates:
        return
    print(f"backfill {len(dates)} editions: {dates[0]} to {dates[-1]}", file=sys.stderr)
    need = [d for d in dates if recollect or not (CACHE / d / "items.json").exists()]
    shas = trending_shas(need) if need else {}
    pulled = feed_entries() if need else []
    collected = {}
    for d in dates:
        collected[d] = collect_date(d, shas.get(d), pulled) if d in need else load_json(CACHE / d / "items.json", {})
        print(f"  {d}: {len(collected[d]['items'])} items", file=sys.stderr)

    # Novelty must only know what came before each date: first sightings from the archive pulls
    # plus whatever live history already holds.
    live = load_json(STATE / "history.json", {"keys": {}})
    first_seen: dict[str, str] = {k: v.get("first_seen") for k, v in live.get("keys", {}).items() if v.get("first_seen")}
    for d in dates:
        for it in collected[d]["items"]:
            for k in it["keys"][:1]:
                if k not in first_seen or d < first_seen[k]:
                    first_seen[k] = d

    model = os.environ.get("MAGPIE_BACKFILL_MODEL", "sonnet")
    for d in sorted(dates, reverse=True):  # newest first, so the calendar fills backward from today
        t0 = time.monotonic()
        saved = collected[d]
        hist = {"keys": {k: {"first_seen": v} for k, v in first_seen.items() if v < d}}
        events = B.build(saved["items"], hist, d)["events"]
        edition, receipt = {}, {"lane": None, "enriched": 0, "errors": ["skipped"]}
        if llm and events:
            prev = os.environ.get("MAGPIE_MODEL")
            os.environ["MAGPIE_MODEL"] = model
            try:
                edition, receipt = enrich(events, d, limit=22)
            finally:
                if prev is None:
                    os.environ.pop("MAGPIE_MODEL", None)
                else:
                    os.environ["MAGPIE_MODEL"] = prev
            events = [e for e in events if not e.get("dropped")]
        day, top_events = compose_day(d, saved["items"], saved["rising"], saved["health"], events, edition, receipt,
                                      where="backfill", seconds=time.monotonic() - t0, note=NOTE)
        publish_day(day, events, top_events)
