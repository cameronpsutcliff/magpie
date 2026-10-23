"""New possibilities: what becomes possible when today's developments are combined, with each other and with what exists.

This is the capability version of PRISM's coalition logic, and the Combination strategist role in
the Practice Atlas: not an "X plus Y" mashup generator, but seeing how seemingly unrelated
developments connect into a non-obvious, testable consequence (the sesame-seed read). Each
possibility names its ingredients, its causal chain with second- and third-order effects, who
benefits and which variable moves, why it beats doing nothing or buying, the smallest credible
test, and what would prove it wrong. Code ranks; the model only proposes.

Two pools are ranked together: today x today, and today x library, where the library is every capability edited before
today (see library.py). A quiet day with one new repo still pairs it with the existing capabilities it lifts. The rank
carries an amplification term: how far the most established ingredient had taken hold before today, so a new piece that
lifts a widely used capability outranks an equally scored pairing of two unknowns.

With MAGPIE_BUILD_ON_IDEAS=1, Magpie's own earlier possibilities join the pool as ideas (see earlier_ideas): when an
arrival makes one of them much stronger, the new possibility builds on it ("On Oct 21 we suggested A with B; D, out
today, removes B's limit") and records it in `builds_on`. An idea is never an ingredient: the ingredients stay
capabilities, and only their words can vouch for a number, apart from the idea's date, which the connection names.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

from . import library, pairing
from .enrich import LANES, _extract_json
from .util import clean, load_json

PROMPT = """You are the combination strategist for Magpie, a daily brief on what became newly possible in AI.
Today is {date}. Below are capabilities, each with an id. Rows with "today": true appeared today. Rows with
"today": false are the library: capabilities that already existed, of any age (see "first_seen"), picked because
they relate to today's arrivals.{pairing}{ideas}

Your job is not to list them and not to mash two together. Find {n} NON-OBVIOUS consequences: places where
two to four of these capabilities, taken together with ordinary public conditions (falling costs, existing
tools, common constraints in real organizations), make something newly possible that none of them does
alone. Think like someone who sees a cicada cycle and buys sesame futures: follow the chain past the obvious
first-order effect. At least one ingredient of every possibility must be from today.

Look for two kinds{need}:
- today with today: two or more of today's arrivals together.
- today with the library: a new arrival that makes an existing capability far more useful, or the reverse. A new
  repo that makes a popular existing tool exponentially better is exactly this kind, even if it is the only news.

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
IDEAS_FLAG = "MAGPIE_BUILD_ON_IDEAS"  # "1" lets today's arrivals build on earlier editions' possibilities
IDEA_EDITIONS = 7  # editions whose possibilities can be built on
IDEA_ROWS = 6      # earlier ideas in one pool at most
SITE = "https://www.cameronsutcliff.com/magpie"
IDEAS = """ Rows with "kind": "idea" are possibilities Magpie published on an earlier
"date", with the capabilities they combined in "recipe". When a capability from today makes one of them much
stronger, cheaper or newly practical, you may return a possibility that builds on it: list the idea's id among the
ingredients with at least one capability from today, open "connection" with what Magpie suggested on that date, and
say what today's arrival adds and why that changes the outcome. An idea is never one of today's ingredients, one
possibility builds on one idea at most, and every number other than the idea's date must come from a capability, not
from the idea."""
PAIRED = """ A library row with "pairing" was chosen by a panel of models as a partner for the
arrival in "with", with the relation and the reason; treat that as a lead to test, not as a fact about either one."""


def _pool(day: dict, lib: dict[str, dict], limit_today: int = 24, check=library.live, data_dir=None) -> tuple[list[dict], dict]:
    """Today's edited capabilities, then the live library capabilities its strongest arrivals relate to most (or, with
    MAGPIE_PAIR_JUDGE=1, the partners a panel of models chose for them; see pairing.py)."""
    edited = [e for e in day["events"] if e.get("brief")][:limit_today]
    rows = [_row(e, day["date"], True, lib.get(e["key"])) for e in edited]
    # The day's events are ranked, so the first edited ones are its strongest arrivals.
    strong, exclude, notes = edited[:library.STRONG], {e["key"] for e in day["events"]}, {}
    if pairing.enabled() and data_dir is not None:
        keys, receipt, notes = pairing.pick(day, strong, lib, exclude, check, data_dir)
    else:
        keys, receipt = library.pick(strong, lib, exclude, check)
    rows += [_row(lib[k]["event"], lib[k]["date"], False, lib[k], notes.get(k)) for k in keys]
    ideas = earlier_ideas(day["date"], data_dir, strong) if ideas_enabled() and data_dir is not None else []
    rows += ideas
    return rows, {"today": len(edited), "library": len(keys), **({"ideas": len(ideas)} if ideas else {}), **receipt}


def ideas_enabled() -> bool:
    return os.environ.get(IDEAS_FLAG) == "1"


def earlier_ideas(date: str, data_dir, arrivals: list[dict], editions: int = IDEA_EDITIONS, limit: int = IDEA_ROWS) -> list[dict]:
    """The possibilities of the last `editions` editions before `date`, as pool rows of kind "idea": the `limit` closest
    to any of today's strongest `arrivals`, newest first among equals; one that shares no word is left out. Closeness is
    the cosine of TF-IDF weights over the library's word lists (library._words for the idea, library._doc for the
    arrival), with document frequencies from these ideas and arrivals: only the shared-words part of library.related,
    without its topic, integration or maker terms. An earlier edition file that cannot be read or parsed, or that is not
    an edition with a date, is skipped and does not count toward `editions`; anything of the wrong type inside one is
    skipped too."""
    text = lambda v: v if isinstance(v, str) else ""  # noqa: E731
    listed = lambda v: v if isinstance(v, list) else []  # noqa: E731
    found = []
    for path in sorted((Path(data_dir) / "days").glob("*.json"), reverse=True):
        if len(found) >= editions:
            break
        if path.stem >= date:
            continue
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):  # unreadable, not UTF-8, or not JSON
            continue
        if isinstance(d, dict) and isinstance(d.get("date"), str):
            found.append(d)
    rows = []
    for d in found:
        poss = d.get("possibilities")
        for rank, p in enumerate(listed(poss.get("items") if isinstance(poss, dict) else None), 1):
            if not isinstance(p, dict) or not text(p.get("title")).strip():
                continue
            recipe = [r["title"] for r in listed(p.get("ingredient_refs")) if isinstance(r, dict) and isinstance(r.get("title"), str)]
            rows.append({"id": f"idea-{d['date']}-{rank}", "date": d["date"], "today": False, "first_seen": d["date"],
                         "kind": "idea", "title": p["title"], "what": text(p.get("connection")), "recipe": recipe,
                         "why": "", "limits": text(p.get("disconfirm")), "maker": None, "topics": [], "evidence": "idea",
                         "_summary": " ".join(c for c in listed(p.get("chain")) if isinstance(c, str)),
                         "_url": f"{SITE}/{d['date']}", "_adoption": 0.0, "_signals": None})
    if not rows or not arrivals:
        return []
    docs = {r["id"]: library._words(" ".join([r["title"], r["what"], " ".join(r["recipe"]), r["_summary"]])) for r in rows}
    targets = [library._doc(a) for a in arrivals]
    df = Counter(t for d in [*docs.values(), *targets] for t in set(d))
    n = len(docs) + len(targets) + 1
    vec = lambda words: {t: c * (math.log(n / (1 + df[t])) + 1) for t, c in Counter(words).items()}  # noqa: E731

    def cos(a: dict, b: dict) -> float:
        norm = math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))
        return sum(w * b.get(t, 0.0) for t, w in a.items()) / norm if norm else 0.0
    tv = [vec(t) for t in targets]
    scored = [(max(cos(vec(docs[r["id"]]), t) for t in tv), r) for r in rows]
    scored = [(s, r) for s, r in scored if s > 0]
    scored.sort(key=lambda sr: (-round(sr[0], 6), "".join(chr(0x10FFFF - ord(c)) for c in sr[1]["date"]), sr[1]["id"]))
    return [r for _, r in scored[:limit]]


def _row(e: dict, date: str, today: bool, hist: dict | None = None, paired: list[dict] | None = None) -> dict:
    """One capability as the model sees it; `hist` is its library entry (its history before today), if it has one, and
    `paired` the panel's reasons for choosing it as a partner of today's arrivals."""
    b = e.get("brief") or {}
    row = {"id": e["id"], "date": date, "today": today, "first_seen": (hist or {}).get("first_seen") or date,
           "title": b.get("headline") or e["title"],
           "what": b.get("newly_possible") or e.get("summary", "")[:300], "why": b.get("why", ""),
           "limits": b.get("limits", ""), "maker": e.get("maker"), "kind": e["kind"], "topics": e.get("topics", []),
           "evidence": e.get("evidence"), "_summary": e.get("summary", ""), "_url": e.get("url"),
           "_adoption": library.adoption(hist), "_signals": library.signals(hist) if hist else None}
    if paired:
        row["pairing"] = paired
    return row


def ungrounded_numbers(p: dict, rows: list[dict]) -> list[str]:
    """Numbers in the connection or chain that no ingredient's own text contains. Tests may set thresholds; claims may not."""
    claim = " ".join([p.get("connection", ""), *p.get("chain", [])])
    source = " ".join(json.dumps({k: v for k, v in r.items() if k not in ("_adoption", "_signals", "pairing")}, ensure_ascii=False)
                      for r in rows)  # a panel's reason is not a source
    return sorted({n for n in re.findall(r"\d+(?:\.\d+)?", claim) if n not in source})


def refs(ids: list[str], by_id: dict) -> list[dict]:
    """`date` is the edition the ingredient's text comes from; `first_seen` the first edition it appeared in."""
    return [{"id": i, "title": by_id[i]["title"], "date": by_id[i]["date"], "today": by_id[i]["today"],
             "first_seen": by_id[i]["first_seen"], "maker": by_id[i].get("maker"), "url": by_id[i].get("_url")} for i in ids]


def amplification(ings: list[dict]) -> dict:
    """How much the idea lifts something that had already taken hold: 1 plus the adoption of its most established
    ingredient (0 to 1, from its history before today), with that ingredient and its signals. 1 when every ingredient
    is new today."""
    best = max(ings, key=lambda r: r["_adoption"])
    if not best["_adoption"]:
        return {"factor": 1.0}
    return {"factor": round(1 + best["_adoption"], 2), "lifts": best["id"], "adoption": best["_adoption"], "signals": best["_signals"]}


def rank(items: list[dict]) -> list[dict]:
    for p in items:
        s = p.get("scores", {})
        g = lambda k: max(1, min(5, int(s.get(k) or 1)))  # noqa: E731
        days = EFFORT_DAYS.get(p.get("effort"), 20)
        amp = (p.get("amplification") or {}).get("factor", 1.0)
        # Expected value over time to learn: value and feasibility dominate, evidence and novelty temper, and lifting an
        # established capability multiplies the reach.
        p["rank"] = round(amp * g("value") * g("feasibility") * (g("evidence") ** 0.5) * (g("novelty") ** 0.5) / (1 + days / 30), 2)
    items.sort(key=lambda p: -p["rank"])
    return items


def choose(items: list[dict], n: int) -> list[dict]:
    """The best n by rank, keeping at least one of each mix when both were proposed."""
    out = items[:n]
    for mix in ("today_x_today", "today_x_library"):
        if out and not any(p["mix"] == mix for p in out):
            best = next((p for p in items[n:] if p["mix"] == mix), None)
            if best:
                out = sorted(out[:-1] + [best], key=lambda p: -p["rank"])
    return out


def generate(day: dict, data_dir, n: int = 5, timeout: int = 1200, check=library.live) -> dict:
    """Up to n possibilities for one edition. The model proposes a few more than n across both mixes; code drops the
    ungrounded, ranks the rest and keeps n."""
    pool, receipt = _pool(day, library.build(day["date"], data_dir), check=check, data_dir=data_dir)
    if not any(r["today"] for r in pool) or len(pool) < 2:
        return {"items": [], "pool": receipt, "note": "Too few edited capabilities today to combine."}
    ids = {r["id"] for r in pool}
    today_ids = {r["id"] for r in pool if r["today"]}
    shown = [{k: v for k, v in r.items() if not k.startswith("_")} for r in pool]
    need = " and return at least 3 of each" if receipt["library"] and len(today_ids) > 1 else ""
    paired = PAIRED if any(r.get("pairing") for r in pool) else ""
    idea_ids = [r["id"] for r in pool if r.get("kind") == "idea"]
    prompt = PROMPT.format(date=day["date"], n=n + 3, need=need, pairing=paired, ideas=IDEAS if idea_ids else "",
                           pool=json.dumps(shown, ensure_ascii=False, indent=0))
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
            idea = next((i for i in ing if i in idea_ids), None)  # one idea at most: the first named
            ing = [i for i in ing if i not in idea_ids]  # an idea is what it builds on, never an ingredient
            if len(ing) + bool(idea) < 2 or not (set(ing) & today_ids):
                dropped.append({"title": p.get("title"), "why": "needs two known ingredients (or one and an earlier idea), one from today"})
                continue
            item = {k: clean(v) if isinstance(v, str) else v for k, v in p.items()}
            item["ingredients"] = ing
            item["chain"] = [clean(c) for c in p.get("chain", []) if isinstance(c, str)][:4]
            # Numbers rest on the capabilities' own words; of an idea, only its date counts (the connection names it).
            loose = ungrounded_numbers(item, [by_id[i] for i in ing] + ([{"date": by_id[idea]["date"]}] if idea else []))
            if loose:
                dropped.append({"title": item.get("title"), "why": f"numbers not in its sources: {', '.join(loose)}"})
                continue
            item["ingredient_refs"] = refs(ing, by_id)
            item["mix"] = "today_x_today" if set(ing) <= today_ids else "today_x_library"
            if idea:
                item["builds_on"] = {"id": idea, "date": by_id[idea]["date"], "title": by_id[idea]["title"], "url": by_id[idea]["_url"]}
            item["amplification"] = amplification([by_id[i] for i in ing])
            out.append(item)
        return {"items": choose(rank(out), n), "lane": lane, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "dropped": dropped, "pool": receipt,
                "note": "Reasoned ideas built from the day's capabilities and the earlier ones they lift, not reported news. Each one names the test that would prove it wrong."}
    return {"items": [], "pool": receipt, "errors": errors}


def run_for(date: str | None = None) -> None:
    from .publish import DATA, scrub
    from .util import write_json

    path = DATA / "days" / f"{date}.json" if date else DATA / "latest.json"
    day = load_json(path, None)
    if not day:
        raise SystemExit(f"no edition at {path}")
    result = generate(day, DATA)
    day["possibilities"] = result
    from .recent import attach
    attach(day, DATA)  # the section may have filled or emptied: refresh its fallback
    scrub(day)
    write_json(DATA / "days" / f"{day['date']}.json", day, compact=True)
    index = load_json(DATA / "index.json", {})
    if index.get("latest") == day["date"]:
        write_json(DATA / "latest.json", day, compact=True)
    print(f"possibilities for {day['date']}: {len(result.get('items', []))} ({result.get('lane') or result.get('errors')})", file=sys.stderr)
    from .publish import refresh_elements
    refresh_elements(day["date"])
