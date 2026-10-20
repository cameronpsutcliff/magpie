"""New possibilities: what becomes possible when today's developments are combined.

This is the capability version of PRISM's coalition logic, and the Combination strategist role in
the Practice Atlas: not an "X plus Y" mashup generator, but seeing how seemingly unrelated
developments connect into a non-obvious, testable consequence (the sesame-seed read). Each
possibility names its ingredients, its causal chain with second- and third-order effects, who
benefits and which variable moves, why it beats doing nothing or buying, the smallest credible
test, and what would prove it wrong. Code ranks; the model only proposes.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

from .enrich import LANES, _extract_json
from .util import clean, load_json

PROMPT = """You are the combination strategist for Magpie, a daily brief on what became newly possible in AI.
Today is {date}. Below are capabilities that appeared today and in the past week, each with an id.

Your job is not to list them and not to mash two together. Find {n} NON-OBVIOUS consequences: places where
two to four of these developments, taken together with ordinary public conditions (falling costs, existing
tools, common constraints in real organizations), make something newly possible that none of them does
alone. Think like someone who sees a cicada cycle and buys sesame futures: follow the chain past the obvious
first-order effect. At least one ingredient of every possibility must be from today.

Rules:
- Ground every ingredient claim in the supplied text. You may add general public context (for example
  "inference costs fall over time" or "most firms cannot send code to third-party APIs"), but no invented
  facts, numbers, products or names about the ingredients.
- Prefer consequences a builder, a consultant or a product team could act on within a quarter.
- Diversity: different industries or roles across the {n}; no two built on the same pair of ingredients.
- Plain English, short sentences. Never use the em-dash character.

For each possibility return:
- title: the consequence stated as a claim, up to 14 words (for example "Small clinics can run private transcription and coding review on one laptop")
- connection: one sentence on how the ingredients connect
- ingredients: list of 2 to 4 ids from the input
- context: optional short phrase of public context you relied on, or ""
- chain: 3 to 4 short steps: what changed, the mechanism, and then what, and then what (second and third order)
- who: who benefits and who would pay
- variable: the business variable that moves (for example cost per case, time to first draft, defects found per release)
- versus: one sentence on why this beats doing nothing or buying an existing product
- smallest_test: the smallest credible experiment, with a threshold that would count as success
- disconfirm: the observation that would prove it wrong
- effort: one of "an afternoon", "a week", "a month", "a quarter"
- scores: novelty, feasibility, value, evidence as integers 1 to 5 (evidence = how well the ingredients' own sources support the chain)

Return ONLY one JSON object: {{"possibilities": [...]}}. No markdown fences, no prose.

CAPABILITIES:
{pool}
"""

EFFORT_DAYS = {"an afternoon": 0.5, "a week": 5, "a month": 20, "a quarter": 60}


def _pool(day: dict, prior: list[dict], limit_today: int = 24, per_prior: int = 5) -> list[dict]:
    rows = []
    edited = [e for e in day["events"] if e.get("brief")][:limit_today]
    for e in edited:
        rows.append(_row(e, day["date"], today=True))
    for p in prior:
        by = {e["id"]: e for e in p.get("events", [])}
        for i in p.get("top", [])[:per_prior]:
            if i in by:
                rows.append(_row(by[i], p["date"], today=False))
    return rows


def _row(e: dict, date: str, today: bool) -> dict:
    b = e.get("brief") or {}
    return {"id": e["id"], "date": date, "today": today, "title": b.get("headline") or e["title"],
            "what": b.get("newly_possible") or e.get("summary", "")[:300], "why": b.get("why", ""),
            "limits": b.get("limits", ""), "maker": e.get("maker"), "kind": e["kind"], "topics": e.get("topics", []),
            "evidence": e.get("evidence"), "_summary": e.get("summary", ""), "_url": e.get("url")}


def ungrounded_numbers(p: dict, rows: list[dict]) -> list[str]:
    """Numbers in the connection or chain that no ingredient's own text contains. Tests may set thresholds; claims may not."""
    claim = " ".join([p.get("connection", ""), *p.get("chain", [])])
    source = " ".join(json.dumps(r, ensure_ascii=False) for r in rows)
    return sorted({n for n in re.findall(r"\d+(?:\.\d+)?", claim) if n not in source})


def refs(ids: list[str], by_id: dict) -> list[dict]:
    return [{"id": i, "title": by_id[i]["title"], "date": by_id[i]["date"], "today": by_id[i]["today"],
             "maker": by_id[i].get("maker"), "url": by_id[i].get("_url")} for i in ids]


def rank(items: list[dict]) -> list[dict]:
    for p in items:
        s = p.get("scores", {})
        g = lambda k: max(1, min(5, int(s.get(k) or 1)))  # noqa: E731
        days = EFFORT_DAYS.get(p.get("effort"), 20)
        # Expected value over time to learn: value and feasibility dominate, evidence and novelty temper.
        p["rank"] = round(g("value") * g("feasibility") * (g("evidence") ** 0.5) * (g("novelty") ** 0.5) / (1 + days / 30), 2)
    items.sort(key=lambda p: -p["rank"])
    return items


def generate(day: dict, prior: list[dict], n: int = 5, timeout: int = 1200) -> dict:
    pool = _pool(day, prior)
    if len([r for r in pool if r["today"]]) < 2:
        return {"items": [], "note": "Too few edited capabilities today to combine."}
    ids = {r["id"] for r in pool}
    today_ids = {r["id"] for r in pool if r["today"]}
    shown = [{k: v for k, v in r.items() if not k.startswith("_")} for r in pool]
    prompt = PROMPT.format(date=day["date"], n=n, pool=json.dumps(shown, ensure_ascii=False, indent=0))
    errors = []
    for lane in ("claude", "codex"):
        raw = ""
        try:
            raw = LANES[lane](prompt, timeout)
            data = _extract_json(raw)
        except Exception as ex:
            errors.append(f"{lane}: {str(ex)[:160]} [{raw[:120]!r}]")
            continue
        by_id = {r["id"]: r for r in pool}
        out, dropped = [], []
        for p in data.get("possibilities", []):
            ing = list(dict.fromkeys(i for i in p.get("ingredients", []) if i in ids))
            if len(ing) < 2 or not (set(ing) & today_ids):
                dropped.append({"title": p.get("title"), "why": "needs two known ingredients, one from today"})
                continue
            item = {k: clean(v) if isinstance(v, str) else v for k, v in p.items()}
            item["ingredients"] = ing
            item["chain"] = [clean(c) for c in p.get("chain", []) if isinstance(c, str)][:4]
            loose = ungrounded_numbers(item, [by_id[i] for i in ing])
            if loose:
                dropped.append({"title": item.get("title"), "why": f"numbers not in its sources: {', '.join(loose)}"})
                continue
            item["ingredient_refs"] = refs(ing, by_id)
            out.append(item)
        return {"items": rank(out), "lane": lane, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "dropped": dropped,
                "note": "Reasoned ideas built from the day's capabilities, not reported news. Each one names the test that would prove it wrong."}
    return {"items": [], "errors": errors}


def prior_days(date: str, data_dir, days: int = 6) -> list[dict]:
    out = []
    d0 = dt.date.fromisoformat(date)
    for k in range(1, days + 1):
        p = load_json(data_dir / "days" / f"{(d0 - dt.timedelta(days=k)).isoformat()}.json", None)
        if p:
            out.append(p)
    return out


def run_for(date: str | None = None) -> None:
    from .publish import DATA, scrub
    from .util import write_json

    path = DATA / "days" / f"{date}.json" if date else DATA / "latest.json"
    day = load_json(path, None)
    if not day:
        raise SystemExit(f"no edition at {path}")
    result = generate(day, prior_days(day["date"], DATA))
    day["possibilities"] = result
    scrub(day)
    write_json(DATA / "days" / f"{day['date']}.json", day, compact=True)
    index = load_json(DATA / "index.json", {})
    if index.get("latest") == day["date"]:
        write_json(DATA / "latest.json", day, compact=True)
    print(f"possibilities for {day['date']}: {len(result.get('items', []))} ({result.get('lane') or result.get('errors')})", file=sys.stderr)
