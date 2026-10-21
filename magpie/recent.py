"""No blank sections: when a section of the day is empty, the most impactful recent items, each with its date.

`recent.<section>` is filled only when the day's own list for that section is empty. Constraints and threats come
from their registers; possibilities, ripples and viewing from the last 14 editions. Every line is copied from stored,
already grounded fields: no model call, no new words. `findings` carries one code-built line per empty section
("No new constraint today. Latest: <title> (<date>)"), so nothing generated ever says only that nothing happened.
"""
from __future__ import annotations

import datetime as dt

from .util import load_json

LIMIT = 5
EDITIONS = 14      # editions read for possibilities, ripples and viewing
REGISTER_DAYS = 30  # a register item counts as recent this long after Magpie first saw it
SEV_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
NOUN = {"constraints": "constraint", "threats": "threat", "possibilities": "possibility", "ripples": "ripple",
        "viewing": "video pick"}


def _desc(s: str | None) -> str:
    """A key that sorts dates newest first inside an ascending sort."""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in (s or ""))


def editions(date: str, data_dir, days: int = EDITIONS) -> list[dict]:
    """The published editions before `date`, newest first, at most `days` of them."""
    out = []
    for p in sorted((data_dir / "days").glob("*.json"), reverse=True):
        if p.stem >= date:
            continue
        d = load_json(p, None)
        if d:
            out.append(d)
        if len(out) >= days:
            break
    return out


def _window(rows: list[dict], date: str, field: str = "first_seen") -> list[dict]:
    """Register rows seen on or before `date`, the last REGISTER_DAYS of them when there are any, else all of them."""
    rows = [r for r in rows if r.get(field) and r[field] <= date]
    d0 = dt.date.fromisoformat(date)
    near = [r for r in rows if (d0 - dt.date.fromisoformat(r[field][:10])).days <= REGISTER_DAYS]
    return near or rows


def constraints(date: str, data_dir) -> list[dict]:
    """In force first, then newest."""
    rows = [r for r in load_json(data_dir / "constraints.json", {}).get("items", []) if r.get("status") != "withdrawn"]
    rows = sorted(_window(rows, date), key=lambda r: (r.get("status") != "in_force", _desc(r["first_seen"]), -(r.get("signal") or 0)))
    return [{"date": r["first_seen"], "id": r["id"], "title": r.get("headline") or r.get("title") or "",
             "status": r.get("status"), "doc": r.get("doc_url") or r.get("url"), "why": r.get("why") or r.get("harder") or ""}
            for r in rows[:LIMIT] if r.get("id")]


def threats(date: str, data_dir) -> list[dict]:
    """Severity, then exploited, then newest."""
    rows = load_json(data_dir / "threats.json", {}).get("threats", [])
    rows = sorted(_window(rows, date), key=lambda r: (-SEV_RANK.get(r.get("severity") or "", 0), r.get("exploited") is not True,
                                                      _desc(r["first_seen"])))
    return [{"date": r["first_seen"], "id": r["id"], "title": r.get("title") or "", "severity": r.get("severity"),
             "doc": r.get("doc"), "why": r.get("headline") or ""} for r in rows[:LIMIT] if r.get("id")]


def _spread(rows: list[tuple[int, str, dict]]) -> list[dict]:
    """Each day's best first (newest day first), then each day's second best, and so on; output newest first."""
    picked = sorted(rows, key=lambda t: (t[0], _desc(t[1])))[:LIMIT]
    return [r for _, _, r in sorted(picked, key=lambda t: (_desc(t[1]), t[0]))]


def possibilities(prior: list[dict]) -> list[dict]:
    """By the stored rank score within each edition."""
    rows = []
    for d in prior:
        items = sorted((p for p in (d.get("possibilities") or {}).get("items") or [] if p.get("title")),
                       key=lambda p: -(p.get("rank") or 0))
        for i, p in enumerate(items):
            row = {"date": d["date"], "title": p["title"], "why": p.get("connection") or ""}
            if p.get("id"):
                row["id"] = p["id"]
            rows.append((i, d["date"], row))
    return _spread(rows)


def ripples(prior: list[dict]) -> list[dict]:
    """Knock-on effects the editor mapped, from the edition's most impactful capability first."""
    rows = []
    for d in prior:
        nodes = {n["id"]: n for n in (d.get("graph") or {}).get("nodes") or [] if n.get("id")}
        topics = d.get("topics") or {}
        links = [l for l in (d.get("graph") or {}).get("links") or []
                 if l.get("effect") and l.get("source") in nodes and str(l.get("target", "")).startswith("topic:")]
        links.sort(key=lambda l: -(nodes[l["source"]].get("impact") or 0))
        for i, l in enumerate(links):
            t = l["target"].split(":", 1)[1]
            rows.append((i, d["date"], {"date": d["date"], "from": nodes[l["source"]].get("label") or "",
                                        "to": topics.get(t, t), "why": l["effect"]}))
    return _spread(rows)


def viewing(prior: list[dict]) -> list[dict]:
    """By the stored rank: each edition lists its picks best first."""
    rows = []
    for d in prior:
        for i, v in enumerate(v for v in (d.get("viewing") or {}).get("items") or [] if v.get("title") and v.get("url")):
            rows.append((i, d["date"], {"date": d["date"], "id": v.get("id") or "", "title": v["title"], "url": v["url"],
                                        "why": v.get("why") or ""}))
    return _spread(rows)


def empty_sections(day: dict) -> list[str]:
    """The sections whose own list is empty today (a section the edition does not carry at all counts as empty)."""
    out = []
    if not (day.get("constraints") or {}).get("items"):
        out.append("constraints")
    if not day.get("threats"):
        out.append("threats")
    if not (day.get("possibilities") or {}).get("items"):
        out.append("possibilities")
    if not (day.get("graph") or {}).get("links"):
        out.append("ripples")
    if not (day.get("viewing") or {}).get("items"):
        out.append("viewing")
    return out


def build(day: dict, data_dir) -> dict:
    date = day["date"]
    empty = empty_sections(day)
    prior = editions(date, data_dir) if {"possibilities", "ripples", "viewing"} & set(empty) else []
    make = {"constraints": lambda: constraints(date, data_dir), "threats": lambda: threats(date, data_dir),
            "possibilities": lambda: possibilities(prior), "ripples": lambda: ripples(prior), "viewing": lambda: viewing(prior)}
    out = {}
    for s in empty:
        rows = make[s]()
        if rows:
            out[s] = rows
    return out


def finding(section: str, rows: list[dict]) -> str | None:
    """The line for an empty section, built from its latest recent item; None when there is nothing to point to."""
    if not rows:
        return None
    r = max(rows, key=lambda x: x.get("date") or "")  # the newest; the first listed among equals
    what = f"{r['from']}, {r['why']}" if section == "ripples" else r.get("title")
    if not what:
        return None
    return f"No new {NOUN[section]} today. Latest: {what} ({r['date']})"


def attach(day: dict, data_dir) -> dict:
    """Set day["recent"] and day["findings"] from the registers and past editions. Never raises: a blank is better
    than a failed edition, and the next publish fills it."""
    try:
        recent = build(day, data_dir)
    except Exception as ex:  # a side section must never fail the edition
        import sys
        print(f"recent not built: {type(ex).__name__}: {str(ex)[:160]}", file=sys.stderr)
        recent = {}
    day["recent"] = recent
    lines = {s: finding(s, rows) for s, rows in recent.items()}
    day["findings"] = {s: line for s, line in lines.items() if line}
    return recent
