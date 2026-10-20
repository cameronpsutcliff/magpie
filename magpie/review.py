"""The weekly quality check: was the week's page any good?

`python -m magpie review --week` reads the published editions in data/v1/days and writes
reports/review/YYYY-Www.md: the week's top fives, the hype flags, how many leads had a runnable
artifact, and which top stories later reached "corroborated". Code only; no model is called.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from collections import Counter
from pathlib import Path

from .redact import scrub
from .util import ROOT, canonical_keys, load_json

RUNNABLE_KINDS = {"repo", "model", "space"}
RUNNABLE_KEYS = ("gh:", "hf:", "hfs:")
LABEL = {"reported": "Reported", "reproduced": "Runnable", "corroborated": "Corroborated"}
RANK = {"reported": 0, "reproduced": 1, "corroborated": 2}


def parse_week(spec: str | None, latest: str) -> tuple[int, int]:
    if not spec or spec == "latest":
        y, w, _ = dt.date.fromisoformat(latest).isocalendar()
        return y, w
    m = re.fullmatch(r"(\d{4})-W(\d{1,2})", spec)
    if m:
        return int(m.group(1)), int(m.group(2))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec):
        y, w, _ = dt.date.fromisoformat(spec).isocalendar()
        return y, w
    raise SystemExit("--week takes YYYY-Www (for example 2026-W43) or a date inside the week")


def week_dates(year: int, week: int) -> list[str]:
    return [dt.date.fromisocalendar(year, week, d).isoformat() for d in range(1, 8)]


def artifact(e: dict) -> str | None:
    """Link to a public repo, model or demo someone can run today, if the story has one."""
    urls = [e["url"]] + [s.get("url") or "" for s in e.get("sources", []) + e.get("mentions", [])]
    if e["key"].startswith(RUNNABLE_KEYS):
        same = next((u for u in urls if e["key"] in canonical_keys(u)), None)
        if same:
            return same
        kind, name = e["key"].split(":", 1)
        return {"gh": "https://github.com/", "hf": "https://huggingface.co/", "hfs": "https://huggingface.co/spaces/"}[kind] + name
    if e["kind"] in RUNNABLE_KINDS:
        return e["url"]
    return next((u for u in urls if any(k.startswith(RUNNABLE_KEYS) for k in canonical_keys(u))), None)


def _title(e: dict) -> str:
    return (e.get("brief") or {}).get("headline") or e["title"]


def _top(day: dict) -> list[dict]:
    by_id = {e["id"]: e for e in day.get("events", [])}
    return [by_id[i] for i in day.get("top", []) if i in by_id]


def review(days: dict[str, dict], year: int, week: int) -> tuple[str, dict]:
    """Markdown report and scorecard for one ISO week. `days` is every published edition by date."""
    dates = week_dates(year, week)
    have = [d for d in dates if d in days]
    later_days = sorted(d for d in days if d > dates[0])

    tops = {d: _top(days[d]) for d in have}
    slots = [(d, i, e) for d in have for i, e in enumerate(tops[d])]
    leads = [(d, tops[d][0]) for d in have if tops[d]]
    lead_runnable = sum(1 for _, e in leads if artifact(e))
    slot_runnable = sum(1 for _, _, e in slots if artifact(e))
    top_keys = {e["key"] for _, _, e in slots}

    hype: dict[str, dict] = {}
    for d in have:
        for e in days[d].get("events", []):
            if (e.get("brief") or {}).get("hype"):
                h = hype.setdefault(e["key"], {"event": e, "days": [], "sources": set()})
                h["days"].append(d)
                h["sources"].update(s["name"] for s in e.get("sources", []))
    hype_in_top = [k for k in hype if k in top_keys]
    by_source = Counter(n for h in hype.values() for n in h["sources"])

    # Later corroboration: follow each top story's key through every later edition.
    stories = []
    for k in dict.fromkeys(e["key"] for _, _, e in slots):
        first_d, e = next((d, e) for d, _, e in slots if e["key"] == k)
        at_lead = e["evidence"]
        reached, last_seen, best = None, first_d, at_lead
        for d in later_days:
            if d <= first_d:
                continue
            later = next((x for x in days[d].get("events", []) if x["key"] == k), None)
            if not later:
                continue
            last_seen = d
            if RANK.get(later["evidence"], 0) > RANK.get(best, 0):
                best = later["evidence"]
            if later["evidence"] == "corroborated" and not reached and at_lead != "corroborated":
                reached = d
        stories.append({"key": k, "title": _title(e), "led": first_d, "at_lead": at_lead, "reached": reached,
                        "last_seen": last_seen, "best": best})
    already = [s for s in stories if s["at_lead"] == "corroborated"]
    later = [s for s in stories if s["reached"]]
    never = [s for s in stories if s["at_lead"] != "corroborated" and not s["reached"]]

    card = {"week": f"{year}-W{week:02d}", "editions": len(have), "edited": sum(1 for d in have if days[d].get("run", {}).get("enriched")),
            "leads": len(leads), "leads_runnable": lead_runnable, "slots": len(slots), "slots_runnable": slot_runnable,
            "hype_events": len(hype), "hype_in_top": len(hype_in_top),
            "corroborated_at_lead": len(already), "corroborated_later": len(later), "not_corroborated": len(never)}

    pct = lambda a, b: f"{round(100 * a / b)}%" if b else "n/a"
    L = [f"# Magpie weekly review, {card['week']}", "",
         f"{dates[0]} to {dates[-1]}. {len(have)} of 7 editions published ({card['edited']} edited by the model)"
         + (". Partial week." if len(have) < 7 else "."), ""]
    if not have:
        L.append("No editions were published this week.")
        return "\n".join(L) + "\n", card
    L += ["## Scorecard", "",
          f"- Leads with a runnable artifact: {lead_runnable} of {len(leads)} days ({pct(lead_runnable, len(leads))}); "
          f"across all top-five slots, {slot_runnable} of {len(slots)} ({pct(slot_runnable, len(slots))}).",
          f"- Hype flags: {len(hype)} stories flagged; {len(hype_in_top)} made a top five.",
          f"- Top stories corroborated: {len(already)} when they led, {len(later)} later, {len(never)} not yet "
          f"(followed through {later_days[-1] if later_days else dates[-1]}).", "",
          "## Top fives", ""]
    for d in have:
        ed = days[d].get("edition", {})
        L += [f"### {dt.date.fromisoformat(d).strftime('%a')} {d}: {ed.get('headline') or 'untitled edition'}", ""]
        for i, e in enumerate(tops[d], 1):
            art = artifact(e)
            flags = [f"evidence {LABEL.get(e['evidence'], e['evidence'])}", f"impact {e.get('impact', '?')}",
                     f"signal {e.get('signal')}", ("artifact: yes" if art == e["url"] else f"artifact: [yes]({art})") if art else "artifact: none"]
            if (e.get("brief") or {}).get("hype"):
                flags.append("HYPE")
            L.append(f"{i}. **[{_title(e)}]({e['url']})** ({e.get('maker') or 'Open source'}). {', '.join(flags)}.")
        L.append("")
    L += ["## Hype flags", ""]
    if hype:
        for k, h in sorted(hype.items(), key=lambda kv: kv[1]["days"][0]):
            e = h["event"]
            L.append(f"- **{_title(e)}**, flagged {', '.join(h['days'])}, from {', '.join(sorted(h['sources']))}. "
                     f"Made a top five: {'YES' if k in top_keys else 'no'}.")
        L += ["", "By source (stories flagged): " + ", ".join(f"{n} {c}" for n, c in by_source.most_common()) + "."]
    else:
        L.append("The editor flagged nothing as hype this week.")
    L += ["", "## Runnable artifacts in the lead", ""]
    for d, e in leads:
        art = artifact(e)
        n = sum(1 for x in tops[d] if artifact(x))
        L.append(f"- {d}: {'yes' if art else 'no'}, {_title(e)}{f' ({art})' if art else ''}. Top five: {n} of {len(tops[d])} runnable.")
    L += ["", "## Later corroboration", "",
          "Each top story's capability key, followed through every later edition until it reaches Corroborated "
          "(three or more independent sources).", ""]
    for s in later:
        L.append(f"- **{s['title']}** led {s['led']} as {LABEL[s['at_lead']]}; corroborated on {s['reached']}.")
    if not later:
        L.append("- No top story has reached Corroborated after it led, so far.")
    if already:
        L += ["", "Corroborated when they led:", ""] + [f"- **{s['title']}** ({s['led']})." for s in already]
    if never:
        L += ["", "Not corroborated yet:", ""]
        L += [f"- {s['title']} (led {s['led']} as {LABEL[s['at_lead']]}; best since: {LABEL[s['best']]}; last seen {s['last_seen']})."
              for s in never]
    return "\n".join(L) + "\n", card


def load_days(days_dir: Path) -> dict[str, dict]:
    return {p.stem: load_json(p, {}) for p in sorted(days_dir.glob("*.json"))}


def run(root: Path = ROOT, spec: str | None = None) -> tuple[Path, dict]:
    days = load_days(root / "data/v1/days")
    if not days:
        raise SystemExit("no published editions in data/v1/days")
    year, week = parse_week(spec, max(days))
    text, card = review(days, year, week)
    scrub(text)
    path = root / "reports/review" / f"{card['week']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path, card


def main(a) -> None:
    path, card = run(ROOT, a.week)
    print(f"review {card['week']}: {card['editions']} editions, leads runnable {card['leads_runnable']}/{card['leads']}, "
          f"hype {card['hype_events']} ({card['hype_in_top']} in top five), corroborated later {card['corroborated_later']}",
          file=sys.stderr)
    print(path.relative_to(ROOT))
