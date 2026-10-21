"""No blank sections: when a section of the day is empty, the most impactful recent items, each with its date.

`recent.<section>` is filled only when the day's own list for that section is empty. Constraints and threats come
from their registers; possibilities, ripples and viewing from the last 14 editions. Every line is copied from stored,
already grounded fields: no model call, no new words. Registers count as recent for 90 days; a constraint older than
that is shown only while it is in force. `findings` carries one code-built line per empty section ("No new constraint
today. Latest: <title> (<date>)"; for threats, the most serious item still open), and when nothing is recent it says
when the last one was recorded, so nothing generated ever says only that nothing happened.
"""
from __future__ import annotations

import datetime as dt

from .util import load_json

LIMIT = 5
EDITIONS = 14      # editions read for possibilities, ripples and viewing
REGISTER_DAYS = 90  # a register item counts as recent this long after Magpie first saw it
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


def _recorded(rows: list[dict], date: str) -> list[dict]:
    """Register rows Magpie had recorded by `date`."""
    return [r for r in rows if r.get("first_seen") and r["first_seen"] <= date]


def _within(rows: list[dict], date: str, days: int = REGISTER_DAYS) -> list[dict]:
    d0 = dt.date.fromisoformat(date)
    return [r for r in rows if (d0 - dt.date.fromisoformat(r["first_seen"][:10])).days <= days]


def constraints(date: str, data_dir) -> list[dict]:
    """In force first, then newest, from the last 90 days. With nothing that recent, older actions still in force."""
    rows = _recorded([r for r in load_json(data_dir / "constraints.json", {}).get("items", []) if r.get("status") != "withdrawn"], date)
    rows = _within(rows, date) or [r for r in rows if r.get("status") == "in_force"]
    rows = sorted(rows, key=lambda r: (r.get("status") != "in_force", _desc(r["first_seen"]), -(r.get("signal") or 0)))
    return [{"date": r["first_seen"], "id": r["id"], "title": r.get("headline") or r.get("title") or "",
             "status": r.get("status"), "doc": r.get("doc_url") or r.get("url"), "why": r.get("why") or r.get("harder") or ""}
            for r in rows[:LIMIT] if r.get("id")]


def _threat_rank(r: dict) -> tuple:
    return (-SEV_RANK.get(r.get("severity") or "", 0), r.get("exploited") is not True, _desc(r["first_seen"]))


def threats(date: str, data_dir) -> list[dict]:
    """Severity, then exploited, then newest, from the last 90 days. A package advisory that belongs to an incident is
    shown through the incident."""
    rows = _within(_recorded(load_json(data_dir / "threats.json", {}).get("threats", []), date), date)
    rows = sorted((r for r in rows if not r.get("incident")), key=_threat_rank)
    return [{"date": r["first_seen"], "id": r["id"], "title": r.get("title") or "", "severity": r.get("severity"),
             "status": r.get("status"), "doc": r.get("doc"), "why": r.get("headline") or ""} for r in rows[:LIMIT] if r.get("id")]


def last_recorded(section: str, date: str, data_dir) -> str | None:
    """When nothing is recent: the newest entry the register holds, as an honest line."""
    name, field = {"constraints": ("constraints.json", "items"), "threats": ("threats.json", "threats")}[section]
    rows = _recorded(load_json(data_dir / name, {}).get(field, []), date)
    if not rows:
        return None
    r = max(rows, key=lambda r: r["first_seen"])
    title = r.get("title") if section == "threats" else (r.get("headline") or r.get("title"))
    return f"No {section} recorded since {r['first_seen']} ({title}, {r.get('status') or 'status unknown'})"


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
    """The line for an empty section, from its recent items; None when there is nothing to point to.

    Threats lead with the most serious item still open, in the same ranking as recent.threats; the other sections with
    the newest item."""
    if not rows:
        return None
    if section == "threats":
        r = next((x for x in rows if x.get("status") != "patched"), None)
        if r:
            return f"No new threat today. Most serious still open: {r['title']} ({r.get('severity')}, {r['date']})"
        r = rows[0]
        return f"No new threat today. Most serious recent: {r['title']} ({r.get('severity')}, {r.get('status')}, {r['date']})"
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
    for s in ("constraints", "threats"):
        if s in empty_sections(day) and s not in recent:
            try:
                lines[s] = last_recorded(s, day["date"], data_dir)
            except Exception:
                lines[s] = None
    day["findings"] = {s: line for s, line in lines.items() if line}
    return recent
