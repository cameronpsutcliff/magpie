"""The discovery loop: a watch list that grows and prunes itself, once a week.

`python -m magpie discover` mines the last seven days of run items (state/runs/*/items.json) for
sources Magpie does not watch yet: newsletters and blogs, YouTube channels and GitHub owners that
other sources linked or mentioned, plus the `source_leads` Magpie Scout records in inbox/x-roster.json.
Explicit rules then change the watch list:

- Promote a new source when two or more trusted, independent sources cited it within the week.
- Demote a watched source after zero kept items in its last 21 live editions, or three hype flags in
  21 days. Backfilled editions never count toward zero kept: they were rebuilt from archives that cover
  only some sources. Official lab and vendor feeds (a feed with a vendor) are pinned: hype flags still
  mark their items down in the score, but the loop never benches them.

A trusted source is an owner-curated one: a registry source with trust of at least 0.65, an account in
config/x-accounts.json with signal_to_noise 4 or more, or a high-priority YouTube channel. Discovered
sources never vouch for others, so the list cannot snowball from its own picks; that includes the
accounts Magpie Scout adds, and an X post carrying doubt of level 2 or more.

The X roster is not the loop's: Magpie Scout maintains inbox/x-roster.json and config/x-accounts.json
stays owner-curated, so the loop never promotes or benches an X account. X posts still cite sources.

Every change lands in config/ (sources.json, youtube.json) with its evidence, candidates in
config/candidates.json, and a readable change log in reports/discovery/. The owner undoes any change with
`--veto <id>`; config/vetoes.json remembers it so the loop never repeats it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

from .build import source_group
from .collectors.feeds import parse_feed
from .collectors.github import AI_HINT
from .publish import scrub
from .util import ROOT, edition_date, fetch, load_json, write_json

WEEK_DAYS = 7
DEMOTE_DAYS = 21
HYPE_LIMIT = 3
MIN_TRUST = 0.65
X_TRUSTED_SNR = 4
NEW_FEED_TRUST = 0.6
CAPS = {"github": 5, "newsletter": 3, "youtube": 3}
KIND_LABEL = {"youtube": "YouTube channel", "newsletter": "newsletter", "github": "GitHub owner"}
GROUP_NAMES = {"github": "GitHub", "huggingface": "Hugging Face"}
# Both read the X roster through Grok (the home pulse and Grok Bot's inbox), so an account is one citer
# whichever of them carried its post.
X_SWEEPS = ("x-grok-pulse", "x-grok-bot")
# The shared transcript pipeline's list and Magpie's own config/youtube.json; a channel on both is one citer.
YOUTUBE = ("youtube-watchlist", "youtube-channels")
FEEDISH = re.compile(r"(?:rss|atom|feed)|\.xml(?:$|\?)", re.I)
YT_URL = re.compile(r"youtube\.com/(@[\w.-]{3,30}|channel/UC[\w-]{22}|c/[\w.-]+|user/[\w.-]+)", re.I)
SUBSTACK = re.compile(r"(?<![\w.-])([a-z0-9-]+)\.substack\.com", re.I)
BEEHIIV = re.compile(r"(?<![\w.-])([a-z0-9-]+)\.beehiiv\.com", re.I)
BUTTONDOWN = re.compile(r"buttondown\.(?:email|com)/([\w-]+)", re.I)
POST_PATH = re.compile(r"https?://(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)/p/[\w-]+", re.I)
NOT_PUBLICATIONS = {"www", "open", "on", "cdn", "api", "support", "substackcdn", "app", "rss", "blog", "help"}
BUTTONDOWN_PAGES = {"login", "register", "pricing", "features", "docs", "legal", "blog", "emails", "archive"}


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def _day(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# 1. What an item points at.

def references(item: dict) -> list[dict]:
    """Sources an item links to or mentions, not counting the item's own source."""
    links = item.get("extra", {}).get("links") or []
    text = " ".join([item.get("url") or ""] + links + [item.get("title") or "", item.get("summary") or ""])
    own_host = _host(item.get("url") or "")
    refs: dict[str, dict] = {}

    def add(eid, kind, label, url, **more):
        refs.setdefault(eid, {"id": eid, "kind": kind, "label": label, "url": url, **more})

    for path in YT_URL.findall(text):
        more = {"channel_id": path[len("channel/"):]} if path.lower().startswith("channel/") else {}
        add(f"youtube:{path.lower()}", "youtube", path, f"https://www.youtube.com/{path}", **more)
    for name in SUBSTACK.findall(text):
        host = f"{name.lower()}.substack.com"
        if name.lower() not in NOT_PUBLICATIONS and host != own_host:
            add(f"newsletter:{host}", "newsletter", host, f"https://{host}", feed_url=f"https://{host}/feed")
    for name in BEEHIIV.findall(text):
        host = f"{name.lower()}.beehiiv.com"
        if name.lower() not in NOT_PUBLICATIONS and host != own_host:
            add(f"newsletter:{host}", "newsletter", host, f"https://{host}", feed_url=None)
    for name in BUTTONDOWN.findall(text):
        if name.lower() not in BUTTONDOWN_PAGES:
            add(f"newsletter:buttondown.com/{name.lower()}", "newsletter", f"buttondown.com/{name}",
                f"https://buttondown.com/{name}", feed_url=f"https://buttondown.com/{name}/rss")
    for host in POST_PATH.findall(text):
        host = host.lower().removeprefix("www.")
        if host != own_host and not host.endswith(("substack.com", "beehiiv.com")):
            add(f"newsletter:{host}", "newsletter", host, f"https://{host}", feed_url=f"https://{host}/feed")
    for k in item.get("keys", []) + item.get("mentions", []):
        if k.startswith("gh:"):
            owner = k[3:].split("/")[0]
            add(f"github:{owner}", "github", owner, f"https://github.com/{owner}")
    return list(refs.values())


def citer_of(item: dict, roster: dict, discovered: set) -> tuple[str, str, bool]:
    """(id, display name, trusted) of the source that produced an item."""
    sid = item["source_id"]
    if sid in X_SWEEPS:
        h = (item.get("author") or "").lower()
        acct = roster.get(h) or {}
        doubted = (((item.get("extra") or {}).get("doubt") or {}).get("level") or 0) >= 2
        trusted = acct.get("signal_to_noise", 0) >= X_TRUSTED_SNR and acct.get("origin") != "discovery" and not doubted
        return f"x:{h}", f"X: {item.get('author') or ''}", trusted
    if sid in YOUTUBE:
        # High priority is trust 0.7; discovered channels join at medium priority (0.55), so they never vouch.
        return f"youtube-channel:{(item.get('author') or '').lower()}", item["source_name"], item["trust"] >= MIN_TRUST
    group = source_group(sid)
    return group, GROUP_NAMES.get(group, item["source_name"]), item["trust"] >= MIN_TRUST and sid not in discovered


def _is_ai(item: dict) -> bool:
    if item.get("extra", {}).get("ai") or item["source_kind"] not in ("repo", "community"):
        return True
    return bool(AI_HINT.search(f"{item.get('title') or ''} {item.get('summary') or ''}"))


def mine(week: list[tuple[str, list[dict]]], roster: dict, discovered: set) -> dict[str, dict]:
    """Every source the week's items point at, with who cited it and the evidence."""
    ents: dict[str, dict] = {}
    for date, items in week:
        for it in items:
            cid, cname, trusted = citer_of(it, roster, discovered)
            for ref in references(it):
                _cite(ents, ref, cid, cname, trusted, date, (it.get("title") or "")[:160], it["url"], _is_ai(it))
    return ents


def _cite(ents: dict, ref: dict, cid: str, cname: str, trusted: bool, date: str, title: str, url: str, ai: bool) -> None:
    e = ents.setdefault(ref["id"], {**ref, "citers": {}, "evidence": [], "dates": set(), "ai": False})
    c = e["citers"].setdefault(cid, {"name": cname, "trusted": False})
    c["trusted"] = c["trusted"] or trusted
    e["dates"].add(date)
    e["ai"] = e["ai"] or ai
    if all(ev["url"] != url or ev["source"] != cname for ev in e["evidence"]):
        e["evidence"].append({"date": date, "source": cname, "title": title, "url": url})


def scout_leads(path: Path) -> tuple[str | None, list[dict]]:
    """(updated date, source leads) from Magpie Scout's roster file; nothing when it is missing or malformed."""
    data = load_json(path, {})
    if not isinstance(data, dict):
        return None, []
    leads = data.get("source_leads")
    updated = str(data.get("updated_at") or "")[:10]
    try:
        _day(updated)
    except ValueError:
        updated = None
    return updated, [x for x in leads if isinstance(x, dict)] if isinstance(leads, list) else []


def lead_ref(lead: dict) -> dict | None:
    """The watch-list entity a Scout lead names: a YouTube channel, or a feed for anything else."""
    url = str(lead.get("url") or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
    if str(lead.get("kind") or "").lower() == "youtube" or "youtube.com/" in url:
        m = YT_URL.search(url)
        if not m:
            return None
        path = m.group(1)
        more = {"channel_id": path[len("channel/"):]} if path.lower().startswith("channel/") else {}
        return {"id": f"youtube:{path.lower()}", "kind": "youtube", "label": str(lead.get("name") or path)[:80],
                "url": f"https://www.youtube.com/{path}", **more}
    found = [r for r in references({"url": "", "extra": {"links": [url]}}) if r["kind"] == "newsletter"]
    if found:
        return found[0]
    host = _host(url)
    if not host:
        return None
    kind = str(lead.get("kind") or "").lower()
    return {"id": f"newsletter:{host}", "kind": "newsletter", "label": str(lead.get("name") or host)[:80],
            "url": url, "feed_url": url if FEEDISH.search(urllib.parse.urlsplit(url).path + "?" + urllib.parse.urlsplit(url).query)
            else f"https://{host}/feed", "feed_kind": kind if kind in ("blog", "changelog", "newsletter") else "newsletter"}


def add_leads(ents: dict, leads: list[dict], updated: str | None, roster: dict, start: str, date: str) -> None:
    """A Scout lead is cited by the X accounts it was seen via; only curated ones vouch, as for posts."""
    if not updated or not start <= updated <= date:
        return  # a stale roster file says nothing about this week
    for lead in leads:
        ref = lead_ref(lead)
        if not ref:
            continue
        via = lead.get("seen_via")
        if not isinstance(via, list):
            continue
        title = f"Scout lead: {lead.get('why') or lead.get('name') or ref['label']}"[:160]
        for h in via:
            h = h.strip().lstrip("@").lower() if isinstance(h, str) else ""
            if not h:
                continue
            acct = roster.get(f"@{h}") or {}
            trusted = acct.get("signal_to_noise", 0) >= X_TRUSTED_SNR and acct.get("origin") != "discovery"
            _cite(ents, ref, f"x:@{h}", f"X: @{h}", trusted, updated, title, ref["url"], True)


def trusted_citers(e: dict) -> list[str]:
    return sorted(c["name"] for c in e["citers"].values() if c["trusted"])


# 2. What is already watched.

def _yt_id(c: dict) -> str:
    return f"youtube:{c['handle'].lower()}" if c.get("handle") else f"youtube:channel/{c['channel_id'].lower()}"


def entries(sources: dict, youtube_cfg: dict | None = None):
    """(entity id, label, owner dict, list name, entry, kept key) for every active watch-list entry."""
    for f in sources.get("feeds", []):
        yield f"feed:{f['id']}", f["name"], sources, "feeds", f, f"name:{f['name'].lower()}"
    for o in sources.get("github_orgs", []):
        yield f"github:{o['org'].lower()}", o["org"], sources, "github_orgs", o, f"github:{o['org'].lower()}"
    for c in (youtube_cfg or {}).get("channels", []):
        # The owner prunes config/youtube.json by hand (its dropped list), so the loop only adds to it.
        yield _yt_id(c), c["name"], youtube_cfg, "channels", c, None


def benched_entries(sources: dict):
    for b in sources.get("benched", []):
        if "org" in b:
            yield f"github:{b['org'].lower()}", b["org"], sources, "github_orgs", b
        else:
            yield f"feed:{b['id']}", b["name"], sources, "feeds", b


def watched_ids(sources: dict, week: list[tuple[str, list[dict]]], youtube_cfg: dict | None = None) -> set[str]:
    ids = {e[0] for e in entries(sources)} | {e[0] for e in benched_entries(sources)}
    for c in (youtube_cfg or {}).get("channels", []):
        ids.add(f"youtube:channel/{c['channel_id'].lower()}")
        if c.get("handle"):
            ids.add(f"youtube:{c['handle'].lower()}")
    feeds = sources.get("feeds", []) + [b for b in sources.get("benched", []) if "url" in b]
    for f in feeds:
        ids.add(f"newsletter:{_host(f['url'])}")
        if f.get("discovered_as"):
            ids.add(f["discovered_as"])
        m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/", f["url"])
        if m:
            ids.add(f"github:{m.group(1).lower()}")
    feed_ids = {f["id"] for f in feeds}
    for _, items in week:
        for it in items:
            if it["source_id"] in feed_ids:
                ids.add(f"newsletter:{_host(it['url'])}")
    return ids


# 3. What the published editions kept.

def _live(day: dict) -> bool:
    return not (day.get("edition") or {}).get("backfilled") and (day.get("run") or {}).get("where") != "backfill"


def kept_stats(days_dir: Path, date: str, window: int = DEMOTE_DAYS) -> dict:
    """Kept counts come from the last `window` live editions, wherever they fall; hype flags from every
    edition in the last `window` calendar days."""
    start = (_day(date) - dt.timedelta(days=window - 1)).isoformat()
    stems = sorted((p.stem for p in days_dir.glob("*.json") if p.stem <= date), reverse=True)
    live, read, backfilled = [], [], 0
    kept, hype = Counter(), {}
    for d in stems:  # newest first, until both windows are covered
        if d < start and len(live) >= window:
            break
        day = load_json(days_dir / f"{d}.json", {})
        read.append(d)
        counts = _live(day) and len(live) < window
        if counts:
            live.append(d)
        elif not _live(day):
            backfilled += 1
        for e in day.get("events", []):
            subjects = {f"name:{s['name'].lower()}" for s in e.get("sources", [])}
            names = subjects | {f"name:{s['name'].lower()}" for s in e.get("mentions", [])}
            if e["key"].startswith("gh:"):
                owner = f"github:{e['key'][3:].split('/')[0]}"
                names.add(owner)
                subjects.add(owner)
            if counts:
                kept.update(names)
            if d >= start and (e.get("brief") or {}).get("hype"):
                for n in subjects:
                    hype.setdefault(n, set()).add(e["key"])
    ready = len(live) >= window
    return {"start": start, "kept_start": live[-1] if ready else None, "first_day": live[-1] if live else None,
            "live": len(live), "days": sorted(read), "backfilled": backfilled, "kept": kept, "hype": hype}


# 4. The rules.

def plan(ents: dict, watched: set, vetoes: dict, avoided: set, check_feed) -> dict:
    never = set(vetoes.get("never_promote", [])) | avoided
    fresh = [e for e in ents.values() if e["id"] not in watched and e["ai"]]
    fresh.sort(key=lambda e: (-len(trusted_citers(e)), -len(e["citers"]), -len(e["evidence"]), e["id"]))
    promoted, held, watching, skipped, taken = [], [], [], [], Counter()
    for e in fresh:
        n = len(trusted_citers(e))
        if e["id"] in never:
            skipped.append(e["id"])
            continue
        if n < 2:
            if n == 1:
                watching.append(e)
            continue
        reason = None
        if taken[e["kind"]] >= CAPS[e["kind"]]:
            reason = f"weekly cap of {CAPS[e['kind']]} new {KIND_LABEL[e['kind']]}s reached"
        elif e["kind"] == "youtube" and not e.get("channel_id"):
            reason = "only a handle was linked; config/youtube.json needs the channel id, so add it by hand if wanted"
        elif e["kind"] == "newsletter":
            if not e.get("feed_url"):
                reason = "no feed URL could be derived; add it by hand if wanted"
            elif check_feed is None:
                reason = "feed not checked (offline run)"
            else:
                ok, note = check_feed(e["feed_url"])
                if not ok:
                    reason = f"feed check failed: {note}"
        if reason:
            held.append({**e, "reason": reason})
        else:
            promoted.append(e)
            taken[e["kind"]] += 1
    return {"promoted": promoted, "held": held, "watching": watching, "skipped": skipped}


def pinned(eid: str, lst: str, entry: dict, vetoes: dict) -> bool:
    """Never benched: owner pins, and official lab and vendor feeds (their own blogs, news and changelogs)."""
    return eid in set(vetoes.get("never_demote", [])) or bool(entry.get("pinned")) or (lst == "feeds" and bool(entry.get("vendor")))


def demotions(sources: dict, vetoes: dict, stats: dict) -> tuple[list[dict], bool]:
    ready = stats["live"] >= DEMOTE_DAYS
    out = []
    for eid, label, owner, lst, entry, kept_key in entries(sources):
        if kept_key is None or pinned(eid, lst, entry, vetoes):
            continue
        flags = len(stats["hype"].get(kept_key, ()))
        if flags >= HYPE_LIMIT:
            reason = f"hype-flagged {flags} times in the last {DEMOTE_DAYS} days"
        elif ready and stats["kept"][kept_key] == 0 and (entry.get("since") or "") <= stats["kept_start"]:
            reason = f"zero kept items in the last {DEMOTE_DAYS} live editions"
        else:
            continue
        out.append({"id": eid, "label": label, "owner": owner, "list": lst, "entry": entry, "reason": reason})
    return out, ready


def _why(e: dict, date: str) -> str:
    return f"Discovered {date}: cited by {', '.join(trusted_citers(e))} within a week."


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _feed_id(label: str, taken: set) -> str:
    base = fid = f"nl-{_slug(label)}"
    n = 2
    while fid in taken:
        fid, n = f"{base}-{n}", n + 1
    taken.add(fid)
    return fid


def apply(result: dict, sources: dict, youtube_cfg: dict, date: str) -> None:
    feed_ids = {f["id"] for f in sources.get("feeds", []) + sources.get("benched", []) if "id" in f}
    for e in result["promoted"]:
        evidence = [ev for ev in _evidence(e) if ev["source"] in trusted_citers(e)][:3]
        base = {"origin": "discovery", "since": date, "discovered_as": e["id"], "evidence": evidence}
        if e["kind"] == "newsletter":
            sources.setdefault("feeds", []).append({
                "id": _feed_id(e["label"], feed_ids), "name": e["label"].removesuffix(".substack.com"),
                "kind": e.get("feed_kind") or "newsletter",
                "url": e["feed_url"], "trust": NEW_FEED_TRUST, "vendor": None, "product": None, "window_hours": 72,
                "why": _why(e, date), **base})
        elif e["kind"] == "github":
            sources.setdefault("github_orgs", []).append({"org": e["label"], "why": _why(e, date), **base})
        elif e["kind"] == "youtube":
            youtube_cfg.setdefault("channels", []).append({
                "channel_id": e["channel_id"], "handle": None, "name": e["label"], "class": None, "priority": "medium",
                "why": _why(e, date), "source": "discovery", **base})
    for d in result["demoted"]:
        d["owner"][d["list"]].remove(d["entry"])
        d["owner"].setdefault("benched", []).append({**d["entry"], "benched": {"date": date, "reason": d["reason"]}})


# 5. Reading and writing.

def check_feed_online(url: str) -> tuple[bool, str]:
    try:
        entries_ = parse_feed(fetch(url, timeout=15, retries=0))
    except Exception as ex:
        return False, f"{type(ex).__name__}: {str(ex)[:120]}"
    return (True, f"{len(entries_)} entries") if entries_ else (False, "feed has no entries")


def save_config(path: Path, obj: dict, indent: int = 1, newline: bool = False) -> None:
    """Config files keep their hand-written format: sources.json and vetoes.json use a one-space indent and
    no trailing newline; youtube.json uses two spaces and a trailing newline (save_youtube)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False) + ("\n" if newline else ""))
    tmp.replace(path)


def save_youtube(path: Path, obj: dict) -> None:
    save_config(path, obj, indent=2, newline=True)


def load_week(runs_dir: Path, date: str, days: int = WEEK_DAYS) -> list[tuple[str, list[dict]]]:
    out = []
    for back in range(days - 1, -1, -1):
        d = (_day(date) - dt.timedelta(days=back)).isoformat()
        saved = load_json(runs_dir / d / "items.json", None)
        if saved:
            out.append((d, saved.get("items", [])))
    return out


def _cmd(eid: str) -> str:
    return f"`python -m magpie discover --veto {eid}`"


def _evidence(e: dict) -> list[dict]:
    """Evidence from trusted citers first."""
    trusted = set(trusted_citers(e))
    return sorted(e["evidence"], key=lambda ev: (ev["source"] not in trusted, ev["date"]))


def _evidence_lines(e: dict, limit: int = 3) -> list[str]:
    return [f"  - {ev['date']}, {ev['source']}: {ev['title']} ({ev['url']})" for ev in _evidence(e)[:limit]]


def render_log(date: str, week: list, stats: dict, result: dict, zero_ready: bool, dry_run: bool) -> str:
    start = (_day(date) - dt.timedelta(days=WEEK_DAYS - 1)).isoformat()
    n_items = sum(len(items) for _, items in week)
    L = [f"# Magpie discovery, week ending {date}", ""]
    if dry_run:
        L += ["Dry run: nothing below was written to config/.", ""]
    L += [f"Mined {n_items} items from {len(week)} days of runs ({start} to {date}) and checked "
          f"{len(stats['days'])} published editions for demotions.", "",
          "Rules: promote a new source when two or more trusted, independent sources cite it within the week. "
          f"Demote a watched source after zero kept items in its last {DEMOTE_DAYS} live editions, or {HYPE_LIMIT} hype "
          f"flags in {DEMOTE_DAYS} days. Discovered sources never vouch for others.", "",
          "To undo a change, run its veto command. config/vetoes.json remembers every veto.", ""]
    promoted, demoted, held, watching = result["promoted"], result["demoted"], result["held"], result["watching"]
    effect = {"newsletter": "added as a feed", "github": "added to GitHub watched orgs",
              "youtube": "added to config/youtube.json at medium priority"}
    L += [f"## Promoted ({len(promoted)})", ""]
    for e in promoted:
        where = f" ({e['feed_url']})" if e["kind"] == "newsletter" else ""
        L += [f"- **{e['label']}**, {KIND_LABEL[e['kind']]}, {effect[e['kind']]}{where}. "
              f"Cited by {', '.join(trusted_citers(e))}."] + _evidence_lines(e) + [f"  - Veto: {_cmd(e['id'])}"]
    if not promoted:
        L.append("Nothing new met the rule this week.")
    L += ["", f"## Demoted ({len(demoted)})", ""]
    for d in demoted:
        L += [f"- **{d['label']}**: {d['reason']}. Moved to the benched list.", f"  - Veto (restore): {_cmd(d['id'])}"]
    if not demoted:
        L.append("No watched source broke a rule.")
    if held:
        L += ["", f"## Held ({len(held)})", "", "Met the citation rule but not applied:", ""]
        for e in held:
            L += [f"- **{e['label']}**, {KIND_LABEL[e['kind']]}: {e['reason']}. Cited by {', '.join(trusted_citers(e))}."]
    if watching:
        L += ["", f"## Watching ({len(watching)})", "", "One trusted source so far; a second one promotes them:", ""]
        for kind in KIND_LABEL:
            group = [e for e in watching if e["kind"] == kind]
            if group:
                names = ", ".join(f"{e['label']} ({trusted_citers(e)[0]})" for e in group[:8])
                more = f", and {len(group) - 8} more in config/candidates.json" if len(group) > 8 else ""
                L.append(f"- {KIND_LABEL[kind]}s: {names}{more}.")
    if result["skipped"]:
        L += ["", f"Skipped {len(result['skipped'])} sources the owner vetoed or dropped."]
    L += ["", "## Rule status", ""]
    if zero_ready:
        L.append(f"- Zero-kept demotion: active, over the {DEMOTE_DAYS} live editions from {stats['kept_start']} to {date}.")
    else:
        first = stats["first_day"] or date
        due = (_day(date) + dt.timedelta(days=DEMOTE_DAYS - stats["live"])).isoformat()
        L.append(f"- Zero-kept demotion: waiting for {DEMOTE_DAYS} live editions ({stats['live']} so far, first live "
                 f"edition {first}; first check {due} at the earliest; {stats['backfilled']} backfilled editions "
                 "do not count).")
    L.append(f"- Hype demotion: active, {HYPE_LIMIT} flags on a source's own stories in {DEMOTE_DAYS} days; official "
             "lab and vendor feeds are pinned.")
    L.append("- X accounts: left to Magpie Scout; the loop only reads their posts and the Scout's source leads.")
    return "\n".join(L) + "\n"


def _record(e: dict, status: str, reason: str | None = None) -> dict:
    out = {"id": e["id"], "kind": e["kind"], "label": e["label"], "url": e["url"], "status": status,
           "trusted_citers": trusted_citers(e), "citers": sorted(c["name"] for c in e["citers"].values()),
           "first_seen": min(e["dates"]), "last_seen": max(e["dates"]), "evidence": _evidence(e)[:6]}
    if e["kind"] == "newsletter":
        out["feed_url"] = e.get("feed_url")
    if reason:
        out["reason"] = reason
    return out


def run(root: Path = ROOT, date: str | None = None, dry_run: bool = False, check_feed=check_feed_online) -> dict:
    date = date or edition_date()
    cfg = root / "config"
    sources = load_json(cfg / "sources.json", {})
    roster_cfg = load_json(cfg / "x-accounts.json", {"accounts": []})
    youtube_cfg = load_json(cfg / "youtube.json", {"channels": []})
    vetoes = load_json(cfg / "vetoes.json", {"never_promote": [], "never_demote": []})
    week = load_week(root / "state/runs", date)
    # Read only: the curated accounts are the ones whose posts and leads vouch.
    roster = {a["handle"].lower(): a for a in roster_cfg.get("accounts", []) if a.get("handle")}
    # Repos from promoted GitHub owners are discovered too, so they never vouch for others.
    discovered = {f["id"] for f in sources.get("feeds", []) if f.get("origin") == "discovery"} | {"github-watched-orgs"}
    avoided = {f"youtube:channel/{c['channel_id'].lower()}" for c in youtube_cfg.get("dropped", [])}

    ents = mine(week, roster, discovered)
    updated, leads = scout_leads(root / "inbox/x-roster.json")
    add_leads(ents, leads, updated, roster, (_day(date) - dt.timedelta(days=WEEK_DAYS - 1)).isoformat(), date)
    result = plan(ents, watched_ids(sources, week, youtube_cfg), vetoes, avoided, check_feed)
    stats = kept_stats(root / "data/v1/days", date)
    result["demoted"], zero_ready = demotions(sources, vetoes, stats)
    log = render_log(date, week, stats, result, zero_ready, dry_run)

    candidates = {
        "schema": "magpie.candidates/1",
        "generated": date,
        "window": {"start": (_day(date) - dt.timedelta(days=WEEK_DAYS - 1)).isoformat(), "end": date,
                   "run_days": [d for d, _ in week]},
        "rules": {"promote": "two or more trusted, independent sources cite it within the week",
                  "demote": f"zero kept items in the last {DEMOTE_DAYS} live editions, or {HYPE_LIMIT} hype flags in "
                            f"{DEMOTE_DAYS} days",
                  "trusted": f"owner-curated sources with trust >= {MIN_TRUST}, config/x-accounts.json accounts with "
                             f"signal_to_noise >= {X_TRUSTED_SNR}, high-priority YouTube channels; discovered sources "
                             "never vouch",
                  "pinned": "official lab and vendor feeds (a vendor set) and owner pins are never benched",
                  "x": "X accounts are Magpie Scout's; the loop never promotes or benches them"},
        "candidates": [_record(e, "promoted") for e in result["promoted"]] +
                      [_record(e, "held", e["reason"]) for e in result["held"]],
        "demoted": [{"id": d["id"], "label": d["label"], "reason": d["reason"]} for d in result["demoted"]],
        "watching": [_record(e, "watching") for e in result["watching"][:60]],
        "skipped": sorted(result["skipped"]),
    }
    scrub(candidates)
    scrub(log)
    if not dry_run:
        apply(result, sources, youtube_cfg, date)
        if result["promoted"] or result["demoted"]:
            save_config(cfg / "sources.json", sources)
        if any(e["kind"] == "youtube" for e in result["promoted"]):
            save_youtube(cfg / "youtube.json", youtube_cfg)
        write_json(cfg / "candidates.json", candidates)
        path = root / "reports/discovery" / f"{date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        prior = path.read_text() if path.exists() else ""
        path.write_text(prior + ("\n---\n\n" if prior else "") + log)
    return {"log": log, "candidates": candidates, "result": result}


def veto(root: Path, ids: list[str]) -> list[str]:
    """Undo a promotion or demotion and remember the decision."""
    cfg = root / "config"
    sources = load_json(cfg / "sources.json", {})
    youtube_cfg = load_json(cfg / "youtube.json", None)
    vetoes = load_json(cfg / "vetoes.json", {"never_promote": [], "never_demote": []})
    msgs = []

    def remember(key, eid):
        if eid not in vetoes.setdefault(key, []):
            vetoes[key].append(eid)

    for raw in ids:
        vid = raw.strip().lower()
        if vid.startswith("x:"):
            msgs.append(f"{vid}: X accounts belong to Magpie Scout and config/x-accounts.json; the loop does not change them.")
            continue
        done = False
        for eid, label, owner, lst, entry, _ in list(entries(sources, youtube_cfg)):
            if vid not in (eid, (entry.get("discovered_as") or "").lower()):
                continue
            if entry.get("origin") == "discovery":
                owner[lst].remove(entry)
                remember("never_promote", entry.get("discovered_as") or eid)
                msgs.append(f"{label}: promotion undone; it will not be promoted again.")
            else:
                remember("never_demote", eid)
                msgs.append(f"{label}: pinned; the loop will not demote it.")
            done = True
            break
        if not done:
            for eid, label, owner, lst, entry in list(benched_entries(sources)):
                if vid != eid:
                    continue
                owner["benched"].remove(entry)
                owner.setdefault(lst, []).append({k: v for k, v in entry.items() if k != "benched"})
                remember("never_demote", eid)
                msgs.append(f"{label}: restored from the bench and pinned.")
                done = True
                break
        if not done:
            remember("never_promote", vid)
            msgs.append(f"{vid}: not on the watch list; recorded so it is never promoted.")
    vetoes.setdefault("note", "Owner decisions the discovery loop must respect. Ids look like github:owner, "
                              "newsletter:host, youtube:@handle or feed:<id>.")
    save_config(cfg / "sources.json", sources)
    if youtube_cfg is not None:
        save_youtube(cfg / "youtube.json", youtube_cfg)
    save_config(cfg / "vetoes.json", vetoes)
    return msgs


def main(a) -> None:
    if a.veto:
        for m in veto(ROOT, a.veto):
            print(m)
        return
    out = run(ROOT, a.date, a.dry_run, None if a.offline else check_feed_online)
    print(out["log"])
    r = out["result"]
    print(f"discover: {len(r['promoted'])} promoted, {len(r['demoted'])} demoted, {len(r['held'])} held, "
          f"{len(r['watching'])} watching", file=sys.stderr)
