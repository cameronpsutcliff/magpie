"""Views: a public id for every new possibility, and the week's and month's best, built from published editions by code.

- `idea_id` gives each possibility an id that is also its page, /magpie/i/<id>: the edition date and the first words of
  its title ("2026-10-23-saas-vendors-can-hand-auditors-machine"), with "-2" and on for a repeat within that day.
- `ideas/index.json` lists every possibility of every edition with its id, ingredients, what it builds on and what
  later built on it, so a page can be found from its id and link both ways.
- `rollups/<date>.json` holds the week (the 7 days ending on that edition) and the month (30 days): the highest-impact
  capabilities, the strongest possibilities, and the threats and constraints first seen in the window, with the counts
  of the window before for comparison. A capability's
  impact is the editor's 1 to 5 weight; ties go to the one that led the page on more days, then to the one seen on more
  days, then to the higher signal. Possibilities rank by their own rank score. Nothing here calls a model: every line
  is copied from a published edition, sources included, so a reader can click out to them.

Every file here is a side file: `refresh` never fails an edition.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path

from .util import load_json, write_json

ID_WORDS = 6      # title words in an idea id
WINDOWS = {"week": 7, "month": 30}
TOP_CAPABILITIES = 12
TOP_IDEAS = 10
TOP_REGISTER = 6  # threats and constraints each
CLIP = 400
SITE = "https://www.cameronsutcliff.com/magpie"


def _clip(v, n: int = CLIP) -> str:
    return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""


def idea_id(date: str, title: str, taken: set[str] | frozenset = frozenset()) -> str:
    """The edition date and the first ID_WORDS words of the title, ASCII letters and digits only."""
    ascii_title = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", ascii_title)[:ID_WORDS]
    base = f"{date}-{'-'.join(words) or 'idea'}"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def ids_for(date: str, items: list) -> list[str | None]:
    """The id of each possibility in an edition's list, in order: its own when it has one, else made from its title.
    None for an entry that is not a titled possibility."""
    taken: set[str] = {p["id"] for p in items if isinstance(p, dict) and isinstance(p.get("id"), str)}
    out: list[str | None] = []
    for p in items:
        if not isinstance(p, dict) or not isinstance(p.get("title"), str) or not p["title"].strip():
            out.append(None)
        elif isinstance(p.get("id"), str):
            out.append(p["id"])
        else:
            i = idea_id(date, p["title"], taken)
            taken.add(i)
            out.append(i)
    return out


def assign_ids(day: dict) -> int:
    """Give every possibility of the day that lacks one its id, in place; returns how many were given."""
    items = (day.get("possibilities") or {}).get("items") if isinstance(day.get("possibilities"), dict) else None
    if not isinstance(items, list):
        return 0
    n = 0
    for p, i in zip(items, ids_for(day.get("date", ""), items)):
        if i and "id" not in p:
            p["id"] = i
            n += 1
    return n


def _days(data_dir: Path) -> list[dict]:
    """Every readable edition, oldest first."""
    out = []
    for path in sorted((Path(data_dir) / "days").glob("*.json")):
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("date"), str):
            out.append(d)
    return out


def _items(day: dict) -> list:
    poss = day.get("possibilities")
    items = poss.get("items") if isinstance(poss, dict) else None
    return items if isinstance(items, list) else []


def _idea(day: dict, p: dict, i: str, pos: int, ids_by_date: dict) -> dict:
    refs = [r for r in p.get("ingredient_refs") or [] if isinstance(r, dict)]
    built = p.get("builds_on") if isinstance(p.get("builds_on"), dict) else None
    out = {"id": i, "date": day["date"], "position": pos, "title": _clip(p["title"], 200), "connection": _clip(p.get("connection")),
           "score": p.get("rank") if isinstance(p.get("rank"), (int, float)) else None,
           "effort": p.get("effort") if isinstance(p.get("effort"), str) else None,
           "ingredients": [{"id": r.get("id"), "title": _clip(r.get("title"), 200), "date": r.get("date"), "url": r.get("url")} for r in refs]}
    if built and isinstance(built.get("date"), str):
        # An idea built on another is stored with that idea's pool id ("idea-<date>-<position>"); the index names its page.
        target = built.get("id")
        m = re.fullmatch(r"idea-(\d{4}-\d{2}-\d{2})-(\d+)", target or "")
        if m:
            at = ids_by_date.get(m.group(1)) or []
            k = int(m.group(2)) - 1
            target = at[k] if 0 <= k < len(at) else None
        out["builds_on"] = {"id": target, "date": built["date"], "title": _clip(built.get("title"), 200)}
    return out


def ideas_index(days: list[dict]) -> dict:
    """Every possibility of every edition, newest edition first, each with what it builds on and what built on it."""
    ids_by_date = {d["date"]: ids_for(d["date"], _items(d)) for d in days}
    ideas = []
    for d in sorted(days, key=lambda d: d["date"], reverse=True):
        for pos, (p, i) in enumerate(zip(_items(d), ids_by_date[d["date"]]), 1):
            if i:
                ideas.append(_idea(d, p, i, pos, ids_by_date))
    by_id = {x["id"]: x for x in ideas}
    for x in ideas:
        target = (x.get("builds_on") or {}).get("id")
        if target in by_id:
            by_id[target].setdefault("built_on_by", []).append(x["id"])
    return {"schema": "magpie.ideas/1", "updated": max((d["date"] for d in days), default=None), "ideas": ideas}


def _sources(e: dict) -> list[dict]:
    return [{"name": _clip(s.get("name"), 80), "title": _clip(s.get("title"), 200), "url": s["url"]}
            for s in (e.get("sources") or [])[:3] if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("http")]


def _capabilities(window: list[dict], slugs: dict[str, str]) -> list[dict]:
    seen: dict[str, dict] = {}
    for d in window:
        top = set(d.get("top") or [])
        for e in d.get("events") or []:
            b = e.get("brief") if isinstance(e, dict) else None
            if not isinstance(b, dict) or not isinstance(b.get("headline"), str) or not isinstance(e.get("key"), str):
                continue
            impact = b.get("impact") if isinstance(b.get("impact"), int) else 0
            signal = e.get("signal") if isinstance(e.get("signal"), (int, float)) else 0
            c = seen.setdefault(e["key"], {"key": e["key"], "days": 0, "top_days": 0, "first": d["date"], "_best": None})
            c["days"] += 1
            c["top_days"] += e.get("id") in top
            c["last"] = d["date"]
            if c["_best"] is None or (impact, signal, d["date"]) >= c["_best"][:3]:
                c["_best"] = (impact, signal, d["date"], e)
    out = []
    for c in seen.values():
        impact, signal, date, e = c.pop("_best")
        b = e["brief"]
        out.append({**c, "slug": slugs.get(c["key"]), "title": _clip(b["headline"], 200), "newly_possible": _clip(b.get("newly_possible")),
                    "impact": impact or None, "signal": signal, "maker": e.get("maker"), "date": date, "event_id": e.get("id"),
                    "url": e.get("url") if isinstance(e.get("url"), str) else None, "sources": _sources(e),
                    **({"reported": e["reported"]} if isinstance(e.get("reported"), str) else {})})
    out.sort(key=lambda c: (-(c["impact"] or 0), -c["top_days"], -c["days"], -c["signal"], c["key"]))
    return out[:TOP_CAPABILITIES]


def _threats(window: list[dict], start: str) -> list[dict]:
    from .threats import rank_key

    seen: dict[str, tuple[str, dict]] = {}
    for d in window:
        for t in d.get("threats") or []:
            if isinstance(t, dict) and isinstance(t.get("id"), str) and str(t.get("first_seen") or "") >= start and not t.get("incident"):
                seen[t["id"]] = (d["date"], t)  # the latest edition's record of it wins
    rows = sorted(seen.values(), key=lambda dt_: (*rank_key(dt_[1]), dt_[1]["id"]))
    return [{"id": t["id"], "date": date, "title": _clip(t.get("title"), 200), "headline": _clip(t.get("headline")),
             "severity": t.get("severity"), "status": t.get("status"), "exploited": t.get("exploited"),
             "first_seen": t.get("first_seen"), "doc": t.get("doc") if isinstance(t.get("doc"), str) else None}
            for date, t in rows[:TOP_REGISTER]]


def _constraints(window: list[dict], start: str) -> list[dict]:
    seen: dict[str, tuple[str, dict]] = {}
    for d in window:
        cons = d.get("constraints")
        for c in (cons.get("items") if isinstance(cons, dict) else None) or []:
            if isinstance(c, dict) and isinstance(c.get("id"), str) and str(c.get("first_seen") or "") >= start:
                seen[c["id"]] = (d["date"], c)
    rows = sorted(seen.values(), key=lambda dc: (-(dc[1].get("signal") or 0), dc[1]["id"]))
    return [{"id": c["id"], "date": date, "headline": _clip(c.get("headline") or c.get("title"), 200), "status": c.get("status"),
             "jurisdiction": c.get("jurisdiction"), "first_seen": c.get("first_seen"),
             "doc": next((u for u in (c.get("doc_url"), c.get("url")) if isinstance(u, str) and u.startswith("http")), None)}
            for date, c in rows[:TOP_REGISTER]]


def rollup(date: str, days: list[dict], index: dict, slugs: dict[str, str]) -> dict:
    """The week and the month ending on `date`, from the editions in `days`."""
    d0 = dt.date.fromisoformat(date)
    out = {"schema": "magpie.rollup/1", "date": date, "views": {}}
    for name, span in WINDOWS.items():
        start = (d0 - dt.timedelta(days=span - 1)).isoformat()
        window = [d for d in days if start <= d["date"] <= date]
        before = (d0 - dt.timedelta(days=2 * span - 1)).isoformat()
        prior = [d for d in days if before <= d["date"] < start]  # the window before, for comparison
        ideas = [x for x in index["ideas"] if start <= x["date"] <= date]
        ideas.sort(key=lambda x: (-(x.get("score") or 0), "".join(chr(0x10FFFF - ord(ch)) for ch in x["date"]), x["position"]))
        out["views"][name] = {
            "from": start, "to": date, "editions": len(window),
            "capabilities": sum(int(((d.get("coverage") or {}).get("events")) or 0) for d in window),
            "possibilities": len(ideas),
            "prior": {"from": before, "editions": len(prior),
                      "capabilities": sum(int(((d.get("coverage") or {}).get("events")) or 0) for d in prior),
                      "possibilities": sum(1 for x in index["ideas"] if before <= x["date"] < start)},
            "top": _capabilities(window, slugs),
            "ideas": [{k: v for k, v in x.items() if k in ("id", "date", "title", "connection", "score", "effort", "builds_on", "built_on_by")}
                      for x in ideas[:TOP_IDEAS]],
            "threats": _threats(window, start),
            "constraints": _constraints(window, start),
        }
    return out


def refresh(data_dir: Path, dates: list[str] | None = None) -> dict:
    """Write the ideas index and the rollups for `dates` (default: the latest edition). A side file: never raises."""
    from .publish import scrub

    try:
        days = _days(data_dir)
        if not days:
            return {"ideas": 0, "rollups": 0}
        index = ideas_index(days)
        scrub(index)
        write_json(Path(data_dir) / "ideas" / "index.json", index, compact=True)
        slugs = {e["key"]: e["slug"] for e in (load_json(Path(data_dir) / "elements" / "index.json", {}) or {}).get("elements", [])
                 if isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("slug"), str)}
        written = 0
        for date in dates or [days[-1]["date"]]:
            r = rollup(date, days, index, slugs)
            scrub(r)
            write_json(Path(data_dir) / "rollups" / f"{date}.json", r, compact=True)
            written += 1
        return {"ideas": len(index["ideas"]), "rollups": written}
    except (Exception, SystemExit) as ex:  # the redaction gate raises SystemExit; here it must not end the run
        print(f"views not updated: {type(ex).__name__}: {str(ex)[:160]}", file=sys.stderr)
        return {"error": type(ex).__name__}


def run(all_dates: bool = False, date: str | None = None) -> None:
    """Give every published possibility its id (rewriting the editions that lacked them, and latest.json), then write the
    ideas index and the rollups: for every edition with --all, else for `date` or the latest."""
    from .publish import DATA, scrub

    given = 0
    index = load_json(DATA / "index.json", {})
    for path in sorted((DATA / "days").glob("*.json")):
        day = load_json(path, None)
        if isinstance(day, dict) and assign_ids(day):
            scrub(day)
            write_json(path, day, compact=True)
            given += 1
            if index.get("latest") == day.get("date"):
                write_json(DATA / "latest.json", day, compact=True)
    dates = [d["date"] for d in _days(DATA)] if all_dates else ([date] if date else None)
    print(f"views: ids given in {given} editions; {refresh(DATA, dates)}", file=sys.stderr)
