"""The library: every capability Magpie has edited before a given day, with how far it has taken hold.

New possibilities combine today's developments with each other and with what already exists. This module supplies the
second pool: for each strong new arrival, the existing capabilities it relates to most, whatever their age and whether
they were ever featured. Retrieval is stdlib only (shared words weighted by rarity, shared topics, a named integration
either way, the same maker) so the cloud tier runs it as the home tier does. No model is called.

A library row carries the same fields as a row from today, from its latest edited appearance, plus its history before the
day: first and last seen, days seen, days featured, peak signal, stars gained, and works-with neighbours (what it was
combined with in a new possibility or covered with in the same post, as on its element page). All of it comes from the
editions before the day, so re-running a past day sees what that day saw.
`adoption` turns that history into one 0 to 1 score; the possibility ranking uses it as the amplification term.
"""
from __future__ import annotations

import math
import re
import urllib.error
from collections import Counter, defaultdict
from pathlib import Path

from .util import fetch_json, github_headers, load_json

PER_ARRIVAL = 3      # existing capabilities kept for each strong new arrival
STRONG = 8           # today's best edited arrivals that look for partners
MAX_ROWS = 20        # library rows in one pool at most
MIN_SCORE = 0.12     # below this relatedness a pairing is noise
MAX_CHECKS = 40      # live checks per pool at most

STOP = set("""a an and are as at be been but by can could do does for from has have how in into is it its just more most
new no not now of on one or our out over so than that the their them then there these they this to up use used uses using
via was were what when which while who will with without would you your also any all about after before only other some
such each per across more less than very much many make makes made lets let model models tool tools ai open source""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.+-]*[a-z0-9]|[a-z]", (text or "").lower()) if len(w) > 2 and w not in STOP]


def _name(key: str) -> str:
    """The short name a key goes by: a repo or model name, the last part of a path."""
    rest = key.split(":", 1)[-1].rstrip("/")
    return rest.rsplit("/", 1)[-1].lower()


def _named(brief: dict) -> list[str]:
    return [str(w.get("name", "")).lower() for w in (brief.get("works_with") or []) if isinstance(w, dict) and w.get("name")]


def build(date: str, data_dir: Path) -> dict[str, dict]:
    """key -> library entry, from every edition before `date`. An entry is {"event", "date", "first_seen", "last_seen",
    "days_seen", "featured", "peak", "stars_gained", "works_with"}; "event" is its latest edited appearance, from "date"."""
    from .elements import _ties

    lib: dict[str, dict] = {}
    totals: dict[str, list[int]] = {}
    partners: dict[str, set[str]] = defaultdict(set)
    key_of: dict[str, str] = {}  # event id -> key; ids are a hash of the key, the same on every day
    for path in sorted(Path(data_dir, "days").glob("*.json")):
        if path.stem >= date:
            continue
        day = load_json(path, None)
        if not day:
            continue
        top = set(day.get("top") or [])
        for e in day.get("events") or []:
            k = e["key"]
            s = lib.setdefault(k, {"event": None, "date": None, "first_seen": day["date"], "last_seen": day["date"],
                                   "days_seen": 0, "featured": 0, "peak": 0.0, "stars_gained": 0, "works_with": 0})
            s["last_seen"] = day["date"]
            s["days_seen"] += 1
            s["featured"] += e["id"] in top
            s["peak"] = max(s["peak"], float(e.get("signal") or 0))
            m = e.get("metrics") or {}
            s["stars_gained"] = max([s["stars_gained"]] + [int(m.get(f) or 0) for f in ("stars_day", "stars_week", "stars_month")])
            if m.get("stars_total"):
                totals.setdefault(k, []).append(int(m["stars_total"]))
            if e.get("brief"):
                s["event"], s["date"] = e, day["date"]
            key_of[e["id"]] = k
        # Works with, as element pages count it: covered by the same post that day, or combined in a new possibility.
        for k, ties in _ties(day.get("events") or []).items():
            partners[k] |= {t["key"] for t in ties}
        for p in (day.get("possibilities") or {}).get("items") or []:
            keys = {key_of[i] for i in p.get("ingredients") or [] if i in key_of}
            for k in keys:
                partners[k] |= keys - {k}
    for k, t in totals.items():
        lib[k]["stars_gained"] = max(lib[k]["stars_gained"], max(t) - min(t))
    for k, s in lib.items():
        s["works_with"] = len(partners[k])
    return {k: s for k, s in lib.items() if s["event"]}


def adoption(s: dict | None) -> float:
    """How far a capability had taken hold before the day, 0 to 1: days seen, peak signal, stars gained and works-with
    neighbours, each capped and weighted equally. A capability first seen today scores 0."""
    if not s:
        return 0.0
    parts = [min(1.0, s["days_seen"] / 7), min(1.0, s["peak"] / 100), min(1.0, math.log10(1 + s["stars_gained"]) / 4),
             min(1.0, s["works_with"] / 6)]
    return round(sum(parts) / len(parts), 2)


def signals(s: dict) -> dict:
    return {k: s[k] for k in ("first_seen", "days_seen", "featured", "peak", "stars_gained", "works_with")}


def _doc(e: dict) -> list[str]:
    b = e.get("brief") or {}
    return _words(" ".join([b.get("headline") or e.get("title", ""), e.get("title", ""), b.get("newly_possible") or "",
                            b.get("why") or "", e.get("summary", "")[:400], _name(e["key"]), " ".join(_named(b))]))


def same(a: dict, b: dict) -> bool:
    """The same release under two keys (a model page and its launch post): the same short name, or headlines that share
    most of their words. A capability does not combine with itself."""
    if len(_name(a["key"])) > 3 and _name(a["key"]) == _name(b["key"]):
        return True
    wa, wb = (set(_words((e.get("brief") or {}).get("headline") or e.get("title", ""))) for e in (a, b))
    return bool(wa | wb) and len(wa & wb) / len(wa | wb) >= 0.5


def related(arrival: dict, entry: dict, vec) -> float:
    """How strongly an existing capability relates to a new arrival: shared rare words, shared topics, either naming the
    other as an integration, the same maker."""
    a, b = vec(arrival), vec(entry["event"])
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    norm = math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))
    cos = dot / norm if norm else 0.0
    ta, tb = set(arrival.get("topics") or []), set(entry["event"].get("topics") or [])
    topics = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    na, nb = _name(arrival["key"]), _name(entry["event"]["key"])
    named = any(nb == n or (len(nb) > 3 and nb in n) for n in _named(arrival.get("brief") or {})) or \
        any(na == n or (len(na) > 3 and na in n) for n in _named(entry["event"].get("brief") or {}))
    maker = bool(arrival.get("maker")) and arrival.get("maker") == entry["event"].get("maker") and \
        arrival.get("maker") not in ("Open source", "Research", "Other")
    return round(cos + 0.25 * topics + 0.5 * named + 0.1 * maker, 3)


def neighbours(arrivals: list[dict], lib: dict[str, dict], exclude: set[str]) -> dict[str, list[tuple[str, float]]]:
    """arrival id -> [(library key, relatedness)], best first: every candidate at or above MIN_SCORE, or when none is, every
    candidate that relates at all, so a lone arrival on a quiet day still has partners to try. Relatedness is weighted by
    the candidate's adoption, so of two equally related capabilities the one that has taken hold comes first."""
    keys = [k for k in lib if k not in exclude]
    docs = {k: _doc(lib[k]["event"]) for k in keys}
    df = Counter(t for d in docs.values() for t in set(d))
    n = len(docs) + 1
    idf = {t: math.log(n / (1 + c)) + 1 for t, c in df.items()}
    cache: dict[str, dict[str, float]] = {}

    def vec(e: dict) -> dict[str, float]:
        if e["key"] not in cache:
            tf = Counter(docs.get(e["key"]) or _doc(e))
            cache[e["key"]] = {t: c * idf.get(t, math.log(n) + 1) for t, c in tf.items()}
        return cache[e["key"]]

    out = {}
    for a in arrivals:
        scored = [(k, related(a, lib[k], vec)) for k in keys if not same(a, lib[k]["event"])]
        scored = [(k, s) for k, s in scored if s >= MIN_SCORE] or [(k, s) for k, s in scored if s > 0]
        scored.sort(key=lambda ks: (-(ks[1] * (1 + adoption(lib[ks[0]]))), ks[0]))
        out[a["id"]] = scored
    return out


def live(key: str) -> bool | None:
    """Whether a repo, model or space still exists (and a repo is not archived): True, False, or None when the check
    could not run. Papers, posts and documents are not checked."""
    kind, _, rest = key.partition(":")
    if kind not in ("gh", "hf", "hfs"):
        return True
    url = {"gh": "https://api.github.com/repos/", "hf": "https://huggingface.co/api/models/",
           "hfs": "https://huggingface.co/api/spaces/"}[kind] + rest
    try:
        r = fetch_json(url, headers=github_headers() if kind == "gh" else None, retries=0, timeout=15)
    except Exception as ex:
        cause = ex.__cause__
        if isinstance(cause, urllib.error.HTTPError) and cause.code in (404, 410):
            return False
        return None
    return not r.get("archived") and not r.get("disabled")


def pick(arrivals: list[dict], lib: dict[str, dict], exclude: set[str], check=live) -> tuple[list[str], dict]:
    """The library keys for one pool, live ones only: up to PER_ARRIVAL for each arrival, best first, at most MAX_ROWS.
    Returns the keys and a receipt of what was checked."""
    near = neighbours(arrivals, lib, exclude)
    chosen: list[str] = []
    state: dict[str, bool | None] = {}
    for a in arrivals:
        got = 0
        for k, _ in near.get(a["id"], []):
            if got >= PER_ARRIVAL or len(chosen) >= MAX_ROWS:
                break
            if k in chosen:
                got += 1
                continue
            if k not in state:
                if len(state) >= MAX_CHECKS:
                    break
                state[k] = check(k)
            if state[k]:
                chosen.append(k)
                got += 1
    return chosen, {"checked": len(state), "not_live": sorted(k for k, v in state.items() if v is False),
                    "unchecked": sorted(k for k, v in state.items() if v is None)}
