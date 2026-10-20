"""The discovery loop: a watch list that grows and prunes itself, once a week.

`python -m magpie discover` mines the last seven days of run items (state/runs/*/items.json) for
sources Magpie does not watch yet: X accounts, YouTube channels, newsletters and GitHub owners that
other sources linked or mentioned. Explicit rules then change the watch list:

- Promote a new source when two or more trusted, independent sources cited it within the week.
- Demote a watched source after zero kept items in 21 days of editions, or three hype flags in that window.

A trusted source is an owner-curated one: a registry source with trust of at least 0.65, an X roster
account the pulse reads (signal_to_noise 4 or more), or a high-priority YouTube channel. Discovered
sources never vouch for others, so the list cannot snowball from its own picks.

Every change lands in config/ with its evidence, candidates in config/candidates.json, and a readable
change log in reports/discovery/. The owner undoes any change with `--veto <id>`; config/vetoes.json
remembers it so the loop never repeats it.
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
CAPS = {"x": 5, "github": 5, "newsletter": 3, "youtube": 3}
KIND_LABEL = {"x": "X account", "youtube": "YouTube channel", "newsletter": "newsletter", "github": "GitHub owner"}
GROUP_NAMES = {"github": "GitHub", "huggingface": "Hugging Face"}
PULSE = ("x-grok-pulse", "X pulse (Grok)")

X_URL = re.compile(r"(?<![\w.-])(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", re.I)
X_AT = re.compile(r"(?<![\w@./])@([A-Za-z0-9_]{2,15})(?![A-Za-z0-9_@]|\.[A-Za-z])")
X_RESERVED = {"i", "home", "search", "intent", "share", "hashtag", "explore", "settings", "messages", "notifications",
              "compose", "login", "signup", "tos", "privacy", "about", "jobs", "status", "web"}
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
    author = (item.get("author") or "").lower()
    refs: dict[str, dict] = {}

    def add(eid, kind, label, url, **more):
        refs.setdefault(eid, {"id": eid, "kind": kind, "label": label, "url": url, **more})

    handles = [h for h in X_URL.findall(text)]
    if item["source_id"] == "x-grok-pulse":
        handles += X_AT.findall(f"{item.get('title') or ''} {item.get('summary') or ''}")
    for h in handles:
        at = f"@{h}"
        if h.lower() in X_RESERVED or at.lower() == author:
            continue
        add(f"x:{at.lower()}", "x", at, f"https://x.com/{h}")
    for path in YT_URL.findall(text):
        add(f"youtube:{path.lower()}", "youtube", path, f"https://www.youtube.com/{path}")
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
    if sid == "x-grok-pulse":
        h = (item.get("author") or "").lower()
        acct = roster.get(h) or {}
        trusted = acct.get("signal_to_noise", 0) >= X_TRUSTED_SNR and acct.get("origin") != "discovery"
        return f"x:{h}", item["source_name"], trusted
    if sid == "youtube-watchlist":
        return f"youtube-watchlist:{(item.get('author') or '').lower()}", item["source_name"], item["trust"] >= MIN_TRUST
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
            found = [(r, cid, cname, trusted) for r in references(it)]
            author = (it.get("author") or "").lower()
            if it["source_id"] == "x-grok-pulse" and author.startswith("@") and author not in roster:
                # The pulse surfaced a post from someone outside the roster: the pulse is the citer.
                found.append(({"id": f"x:{author}", "kind": "x", "label": it["author"], "url": f"https://x.com/{author[1:]}"},
                              PULSE[0], PULSE[1], True))
            for ref, cid, cname, trusted in found:
                e = ents.setdefault(ref["id"], {**ref, "citers": {}, "evidence": [], "dates": set(), "ai": False})
                c = e["citers"].setdefault(cid, {"name": cname, "trusted": False})
                c["trusted"] = c["trusted"] or trusted
                e["dates"].add(date)
                e["ai"] = e["ai"] or _is_ai(it)
                if all(ev["url"] != it["url"] or ev["source"] != cname for ev in e["evidence"]):
                    e["evidence"].append({"date": date, "source": cname, "title": (it.get("title") or "")[:160], "url": it["url"]})
    return ents


def trusted_citers(e: dict) -> list[str]:
    return sorted(c["name"] for c in e["citers"].values() if c["trusted"])


# 2. What is already watched.

def entries(sources: dict, roster_cfg: dict):
    """(entity id, label, owner dict, list name, entry, kept key) for every active watch-list entry."""
    for f in sources.get("feeds", []):
        yield f"feed:{f['id']}", f["name"], sources, "feeds", f, f"name:{f['name'].lower()}"
    for a in roster_cfg.get("accounts", []):
        h = a["handle"].lower()
        yield f"x:{h}", a["handle"], roster_cfg, "accounts", a, f"name:x: {h}"
    for o in sources.get("github_orgs", []):
        yield f"github:{o['org'].lower()}", o["org"], sources, "github_orgs", o, f"github:{o['org'].lower()}"
    for y in sources.get("youtube_channels", []):
        # The YouTube watch list itself lives in the home tier's transcript pipeline, which owns its pruning.
        yield f"youtube:{y['channel'].lower()}", y["channel"], sources, "youtube_channels", y, None


def benched_entries(sources: dict, roster_cfg: dict):
    for b in sources.get("benched", []):
        if "org" in b:
            yield f"github:{b['org'].lower()}", b["org"], sources, "github_orgs", b
        else:
            yield f"feed:{b['id']}", b["name"], sources, "feeds", b
    for b in roster_cfg.get("benched", []):
        yield f"x:{b['handle'].lower()}", b["handle"], roster_cfg, "accounts", b


def watched_ids(sources: dict, roster_cfg: dict, week: list[tuple[str, list[dict]]]) -> set[str]:
    ids = {e[0] for e in entries(sources, roster_cfg)} | {e[0] for e in benched_entries(sources, roster_cfg)}
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

def kept_stats(days_dir: Path, date: str, window: int = DEMOTE_DAYS) -> dict:
    start = (_day(date) - dt.timedelta(days=window - 1)).isoformat()
    dates = sorted(p.stem for p in days_dir.glob("*.json"))
    kept, hype = Counter(), {}
    used = [d for d in dates if start <= d <= date]
    for d in used:
        for e in load_json(days_dir / f"{d}.json", {}).get("events", []):
            subjects = {f"name:{s['name'].lower()}" for s in e.get("sources", [])}
            names = subjects | {f"name:{s['name'].lower()}" for s in e.get("mentions", [])}
            if e["key"].startswith("gh:"):
                owner = f"github:{e['key'][3:].split('/')[0]}"
                names.add(owner)
                subjects.add(owner)
            kept.update(names)
            if (e.get("brief") or {}).get("hype"):
                for n in subjects:
                    hype.setdefault(n, set()).add(e["key"])
    return {"start": start, "first_day": dates[0] if dates else None, "days": used, "kept": kept, "hype": hype}


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


def demotions(sources: dict, roster_cfg: dict, vetoes: dict, stats: dict) -> tuple[list[dict], bool]:
    pinned = set(vetoes.get("never_demote", []))
    ready = stats["first_day"] is not None and stats["first_day"] <= stats["start"]
    out = []
    for eid, label, owner, lst, entry, kept_key in entries(sources, roster_cfg):
        if kept_key is None or eid in pinned or entry.get("pinned"):
            continue
        flags = len(stats["hype"].get(kept_key, ()))
        if flags >= HYPE_LIMIT:
            reason = f"hype-flagged {flags} times in the last {DEMOTE_DAYS} days"
        elif ready and stats["kept"][kept_key] == 0 and (entry.get("since") or "") <= stats["start"]:
            reason = f"zero kept items in the last {DEMOTE_DAYS} days of editions"
        else:
            continue
        out.append({"id": eid, "label": label, "owner": owner, "list": lst, "entry": entry, "reason": reason})
    return out, ready


def _why(e: dict, date: str) -> str:
    return f"Discovered {date}: cited by {', '.join(trusted_citers(e))} within a week."


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def apply(result: dict, sources: dict, roster_cfg: dict, date: str) -> None:
    for e in result["promoted"]:
        evidence = [ev for ev in _evidence(e) if ev["source"] in trusted_citers(e)][:3]
        base = {"origin": "discovery", "since": date, "discovered_as": e["id"], "evidence": evidence}
        if e["kind"] == "x":
            roster_cfg.setdefault("accounts", []).append({
                "handle": e["label"], "display_name": e["label"].lstrip("@"), "category": None, "affiliation": "",
                "why": _why(e, date), "cadence": "unknown", "signal_to_noise": X_TRUSTED_SNR,
                "example": evidence[0]["title"] if evidence else "", **base})
        elif e["kind"] == "newsletter":
            sources.setdefault("feeds", []).append({
                "id": f"nl-{_slug(e['label'])}", "name": e["label"].removesuffix(".substack.com"), "kind": "newsletter",
                "url": e["feed_url"], "trust": NEW_FEED_TRUST, "vendor": None, "product": None, "window_hours": 72,
                "why": _why(e, date), **base})
        elif e["kind"] == "github":
            sources.setdefault("github_orgs", []).append({"org": e["label"], "why": _why(e, date), **base})
        elif e["kind"] == "youtube":
            sources.setdefault("youtube_channels", []).append({
                "channel": e["label"], "url": e["url"], "why": _why(e, date),
                "note": "Add to the home tier's YouTube transcript pipeline to start collecting.", **base})
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


def save_config(path: Path, obj: dict) -> None:
    """Config files keep their hand-written format: one-space indent, no trailing newline."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(path)


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
          f"Demote a watched source after zero kept items in {DEMOTE_DAYS} days of editions, or {HYPE_LIMIT} hype flags "
          "in that window. Discovered sources never vouch for others.", "",
          "To undo a change, run its veto command. config/vetoes.json remembers every veto.", ""]
    promoted, demoted, held, watching = result["promoted"], result["demoted"], result["held"], result["watching"]
    effect = {"x": "joins the X pulse roster", "newsletter": "added as a feed", "github": "added to GitHub watched orgs",
              "youtube": "listed for the YouTube transcript pipeline (add it there to start collecting)"}
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
        L += ["", f"Skipped {len(result['skipped'])} sources the owner vetoed or the X roster avoids."]
    L += ["", "## Rule status", ""]
    if zero_ready:
        L.append(f"- Zero-kept demotion: active, over editions from {stats['start']} to {date}.")
    else:
        first = stats["first_day"] or date
        due = (_day(first) + dt.timedelta(days=DEMOTE_DAYS - 1)).isoformat()
        L.append(f"- Zero-kept demotion: waiting for {DEMOTE_DAYS} days of editions (first edition {first}; "
                 f"first check {due}).")
    L.append(f"- Hype demotion: active, {HYPE_LIMIT} flags on a source's own stories in {DEMOTE_DAYS} days.")
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
    vetoes = load_json(cfg / "vetoes.json", {"never_promote": [], "never_demote": []})
    week = load_week(root / "state/runs", date)
    roster = {a["handle"].lower(): a for a in roster_cfg.get("accounts", [])}
    discovered = {f["id"] for f in sources.get("feeds", []) if f.get("origin") == "discovery"}
    avoided = {f"x:{a['handle'].lower()}" for a in roster_cfg.get("avoid", [])}

    ents = mine(week, roster, discovered)
    result = plan(ents, watched_ids(sources, roster_cfg, week), vetoes, avoided, check_feed)
    stats = kept_stats(root / "data/v1/days", date)
    result["demoted"], zero_ready = demotions(sources, roster_cfg, vetoes, stats)
    log = render_log(date, week, stats, result, zero_ready, dry_run)

    candidates = {
        "schema": "magpie.candidates/1",
        "generated": date,
        "window": {"start": (_day(date) - dt.timedelta(days=WEEK_DAYS - 1)).isoformat(), "end": date,
                   "run_days": [d for d, _ in week]},
        "rules": {"promote": "two or more trusted, independent sources cite it within the week",
                  "demote": f"zero kept items in {DEMOTE_DAYS} days of editions, or {HYPE_LIMIT} hype flags in that window",
                  "trusted": f"owner-curated sources with trust >= {MIN_TRUST}, X roster accounts with signal_to_noise >= "
                             f"{X_TRUSTED_SNR}, high-priority YouTube channels; discovered sources never vouch"},
        "candidates": [_record(e, "promoted") for e in result["promoted"]] +
                      [_record(e, "held", e["reason"]) for e in result["held"]],
        "demoted": [{"id": d["id"], "label": d["label"], "reason": d["reason"]} for d in result["demoted"]],
        "watching": [_record(e, "watching") for e in result["watching"][:60]],
        "skipped": sorted(result["skipped"]),
    }
    scrub(candidates)
    scrub(log)
    if not dry_run:
        apply(result, sources, roster_cfg, date)
        if result["promoted"] or result["demoted"]:
            save_config(cfg / "sources.json", sources)
            save_config(cfg / "x-accounts.json", roster_cfg)
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
    roster_cfg = load_json(cfg / "x-accounts.json", {"accounts": []})
    vetoes = load_json(cfg / "vetoes.json", {"never_promote": [], "never_demote": []})
    msgs = []

    def remember(key, eid):
        if eid not in vetoes.setdefault(key, []):
            vetoes[key].append(eid)

    for raw in ids:
        vid = raw.strip().lower()
        done = False
        for eid, label, owner, lst, entry, _ in list(entries(sources, roster_cfg)):
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
            for eid, label, owner, lst, entry in list(benched_entries(sources, roster_cfg)):
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
    vetoes.setdefault("note", "Owner decisions the discovery loop must respect. Ids look like x:@handle, "
                              "github:owner, newsletter:host, youtube:@handle or feed:<id>.")
    save_config(cfg / "sources.json", sources)
    save_config(cfg / "x-accounts.json", roster_cfg)
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
