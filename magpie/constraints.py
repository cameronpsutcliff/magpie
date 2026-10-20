"""Constraints: the mirror of "newly possible". What became harder, slower or costlier in AI, and for whom.

A constraint is an official action with a document: a law or bill text, a rule, an order, an export-control
list, a procurement ban, a regulator or court decision, or a company's own notice that it is restricting
access to comply with one. The fight about it (positions, op-eds, predictions, lawsuits as news) stays out.
Magpie reports the mechanism and never takes a side.

Candidates come from the primary policy sources (collectors/policy.py) and from any item that links an
official document. One model call classifies them and writes the words, grounded in the document text;
code checks the dates and numbers, ranks by status, reach and urgency, and keeps a register of open
constraints (data/v1/constraints.json) so a draft with a comment deadline stays visible until it closes.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys

from .collectors.policy import is_ai_policy
from .enrich import LANES, _extract_json
from .util import ROOT, clean, fetch, load_json, short_hash, strip_html, url_key, write_json

LAYERS = {
    "training": "Training runs and compute",
    "publishing": "Publishing model weights",
    "hosting": "Hosting and deployment",
    "access": "Access to models and APIs",
    "chips": "Chips, memory and hardware",
    "distribution": "Model hubs and app stores",
    "data": "Training data",
    "use": "Use in a sector",
}
STATUSES = ("proposed", "draft", "final", "in_force", "withdrawn")
STATUS_WEIGHT = {"in_force": 1.0, "final": 0.85, "draft": 0.6, "proposed": 0.4, "withdrawn": 0.2}
INSTRUMENTS = ("law", "bill", "rule", "order", "export_control", "procurement", "standard", "guidance", "court",
               "company_notice")
OPEN_DAYS = 7  # a constraint with no future date stays on the page this long after Magpie first saw it

CONSTRAINT_WORDS = re.compile(
    r"\b(?:export controls?|entity list|ban(?:s|ned|ning)?|bill|regulations?|regulators?|executive orders?|"
    r"sanctions?|federal register|comment period|consultation|AI Act|restrict\w*|prohibit\w*|"
    r"licen[cs]e requirements?|procurement|unavailable in|suspend\w*|moratorium|pause on|outlaw\w*)\b", re.I)
PRIMARY_LINK = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:[a-z0-9-]+\.gov(?:\.[a-z]{2})?|gov\.uk|gov\.cn|europa\.eu|"
    r"federalregister\.gov|congress\.gov|legislation\.gov\.uk|whitehouse\.gov|parliament\.uk)(?:/|$)", re.I)

PROMPT = """You are the constraints editor for Magpie, a public daily brief on what became newly possible in AI.
Your section is its mirror image: official actions that make AI harder, slower or costlier to train, publish,
host, reach or buy. Today is {date}. Below are {n} candidate items from government sources and from posts or
vendor notices that link an official document.

Keep an item only if it is an official action with a document: a law or bill text, a rule (proposed, interim
or final), an executive order or presidential action, an export-control list or license requirement, a
procurement ban, a regulator or standards-body decision, a court order, or a company's own notice that it is
restricting access to comply with one. Drop everything else: positions and statements of intent, speeches,
op-eds, predictions, funding, lawsuits as news, events and hearings, and actions that do not touch AI models,
the chips and memory they run on, or access to them. Report the mechanism. Never take a side, never describe
anyone's politics, and never call an action good or bad.

Ground every field in the item's own text. No dates, numbers, thresholds, names or effects that are not in it.
If the effective date or comment deadline is not stated, use null. Plain English, short sentences. Never use
the em-dash character.

For each item return:
- id: copy it
- keep: true or false
- same_as: the id of another item in this list about the same action, else null
- headline: 5 to 12 words naming the action and who it hits
- harder: one sentence on what becomes harder, slower, costlier or impossible, and for whom
- who: who is affected, as a short phrase
- why: one sentence on why someone who builds with or buys AI should care
- limits: one short clause on what is not settled (draft status, text not public, scope unclear), or ""
- status: one of proposed, draft, final, in_force, withdrawn
- instrument: one of law, bill, rule, order, export_control, procurement, standard, guidance, court, company_notice
- layers: 1 to 3 of training, publishing, hosting, access, chips, distribution, data, use
  (training = compute or training-run rules; publishing = releasing model weights; hosting = who may run or
  deploy a model, including government procurement; access = who may use a model or API; chips = chips,
  memory and hardware; distribution = model hubs and app stores; data = data used to train; use = rules on
  using AI in one sector)
- jurisdiction: US, EU, UK, China, or the country or body named
- issuer: the agency, body or company that issued it
- effective: YYYY-MM-DD or null
- comment_deadline: YYYY-MM-DD or null
- access_now: true only if it is in force and changes who can use an existing model or product today
- impact: integer 1 to 5 for how far it reaches into what builders can do. 5 = blocks a model class or most
  builders in a major market; 3 = one sector or one channel; 1 = narrow or symbolic
- ripples: 1 or 2 objects {{"topic": topic id, "effect": "up to 12 words on the knock-on effect"}}
- hats: object with every hat id as a key; one implication of up to 20 words per role, or ""

Topic ids: {topic_ids}
Hat ids: {hat_ids}

Return ONLY one JSON object: {{"constraints": [...]}}. No markdown fences, no prose.

CANDIDATES:
{rows}
"""

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def constraint_key(it: dict, doc_url: str | None = None) -> str:
    for k in it.get("keys") or []:
        if k.startswith("policy:"):
            return k
    return f"policy:{url_key(doc_url or it['url'])}"


def candidates(items: list[dict], limit: int = 24) -> list[dict]:
    """Policy items, plus any post or vendor notice that names a restriction and links an official document."""
    out = []
    for it in items:
        extra = it.get("extra") or {}
        if it["source_kind"] == "policy":
            out.append({**it, "_key": constraint_key(it), "_primary": bool(extra.get("primary", True)),
                        "_doc": it["url"] if extra.get("primary", True) else next(
                            (l for l in extra.get("links") or [] if PRIMARY_LINK.search(l)), None)})
            continue
        text = f"{it['title']} {it['summary']}"
        if not (CONSTRAINT_WORDS.search(text) and is_ai_policy(text)):
            continue
        doc = next((l for l in [it["url"], *(extra.get("links") or [])] if PRIMARY_LINK.search(l or "")), None)
        vendor_notice = it["source_kind"] == "changelog" or (it["source_kind"] == "news" and it.get("vendor_hint"))
        if doc or vendor_notice:
            out.append({**it, "_key": constraint_key(it, doc), "_primary": True, "_doc": doc or it["url"]})
    out.sort(key=lambda c: c.get("published_at") or "", reverse=True)
    out.sort(key=lambda c: (not c["_primary"], -c.get("trust", 0.5)))  # stable: newest first within each tier
    by_key = {}
    for c in out:
        by_key.setdefault(c["_key"], c)
    return list(by_key.values())[:limit]


def _page_text(url: str, limit: int = 3000) -> str:
    """The document's own words, from its <main> or <article> when there is one."""
    try:
        html = fetch(url, timeout=20, retries=0).decode("utf-8", "replace")
    except Exception:
        return ""
    m = re.search(r"<(main|article)\b", html, re.I)
    return strip_html(html[m.start():] if m else html, limit)


def _row(c: dict, text: str) -> dict:
    extra = c.get("extra") or {}
    return {"id": c["_id"], "source": c["source_name"], "title": c["title"], "summary": c["summary"],
            "issuer": extra.get("issuer") or c.get("author"), "jurisdiction": extra.get("jurisdiction"),
            "document_type": extra.get("doc_type"), "action": extra.get("action"),
            "effective_on": extra.get("effective_on"), "comments_close_on": extra.get("comments_close_on"),
            "dates": extra.get("dates_text"), "published": (c.get("published_at") or "")[:10],
            "url": c["url"], "document_url": c.get("_doc"), "text": text}


def grounded_date(value, text: str) -> str | None:
    """A model-supplied date counts only if its day, month and year all appear in the document text."""
    try:
        d = dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    t = text.lower()
    month_ok = MONTHS[d.month - 1] in t or MONTHS[d.month - 1][:3] + "." in t or d.isoformat() in t or f"{d.month}/{d.day}/{d.year}" in t
    return d.isoformat() if (str(d.year) in t and month_ok and re.search(rf"\b0?{d.day}\b", t)) else None


def ungrounded(fields: list[str], source: str) -> list[str]:
    claim = " ".join(fields)
    return sorted({n for n in re.findall(r"\d+(?:\.\d+)?", claim) if n not in source})


def _signal(rec: dict, date: str) -> tuple[float, dict]:
    layers = set(rec.get("layers") or [])
    reach = min(1.0, 0.3 * len(layers) + (0.25 if layers & {"publishing", "access", "chips"} else 0))
    future = [d for d in (rec.get("comment_deadline"), rec.get("effective")) if d and d >= date]
    if future:
        days = (dt.date.fromisoformat(min(future)) - dt.date.fromisoformat(date)).days
        urgency = 1.0 if days <= 30 else 0.6 if days <= 90 else 0.3
    else:
        urgency = 0.8 if rec.get("status") == "in_force" else 0.3
    parts = {"status": STATUS_WEIGHT.get(rec.get("status"), 0.4), "reach": reach,
             "primary": 1.0 if rec.get("primary") else 0.6, "novelty": 1.0 if rec.get("first_seen") == date else 0.5,
             "urgency": urgency, "impact": (rec.get("impact") or 2) / 5}
    w = {"status": 0.26, "reach": 0.2, "primary": 0.12, "novelty": 0.14, "urgency": 0.14, "impact": 0.14}
    return round(100 * sum(w[k] * v for k, v in parts.items()), 1), {k: round(v, 3) for k, v in parts.items()}


def classify(cands: list[dict], date: str, timeout: int = 900) -> tuple[dict, list[dict], dict]:
    """One model call over the new candidates. Returns (verdicts by key, dropped, receipt)."""
    topics = load_json(ROOT / "config/topics.json", {}).get("topics", {})
    hats = [h["id"] for h in load_json(ROOT / "config/hats.json", {}).get("hats", [])]
    for c in cands:
        c["_id"] = short_hash(c["_key"], 8)
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda c: _page_text(c["_doc"]) if c.get("_doc") else "", cands))
    rows = [_row(c, t) for c, t in zip(cands, texts)]
    by_id = {r["id"]: (c, r) for c, r in zip(cands, rows)}
    prompt = PROMPT.format(date=date, n=len(rows), topic_ids=", ".join(topics), hat_ids=", ".join(hats),
                           rows=json.dumps(rows, ensure_ascii=False, indent=0))
    errors = []
    for lane in [l.strip() for l in os.environ.get("MAGPIE_LANES", "claude,codex").split(",") if l.strip()]:
        try:
            data = _extract_json(LANES[lane](prompt, timeout))
        except Exception as ex:  # try the next lane
            errors.append(f"{lane}: {str(ex)[:160]}")
            continue
        verdicts, dropped = {}, []
        for v in data.get("constraints", []):
            hit = by_id.get(v.get("id"))
            if not hit:
                continue
            c, row = hit
            if v.get("keep") is not True:
                verdicts[c["_key"]] = {"kept": False, "date": date}
                continue
            source = json.dumps(row, ensure_ascii=False)
            words = {k: clean(v.get(k) or "") for k in ("headline", "harder", "who", "why", "limits")}
            loose = ungrounded([words["headline"], words["harder"], words["why"]], source)
            if loose or not words["headline"] or not words["harder"]:
                dropped.append({"title": c["title"], "why": f"numbers not in its document: {', '.join(loose)}" if loose else "no headline"})
                verdicts[c["_key"]] = {"kept": False, "date": date, "dropped": True}
                continue
            extra = c.get("extra") or {}
            try:
                impact = max(1, min(5, int(v.get("impact") or 2)))
            except (TypeError, ValueError):
                impact = 2
            verdicts[c["_key"]] = {
                "kept": True, "date": date, "same_as": v.get("same_as"), "_id": c["_id"],
                **words,
                "status": v.get("status") if v.get("status") in STATUSES else "proposed",
                "instrument": v.get("instrument") if v.get("instrument") in INSTRUMENTS else "guidance",
                "layers": [l for l in v.get("layers") or [] if l in LAYERS][:3],
                "jurisdiction": clean(v.get("jurisdiction") or extra.get("jurisdiction") or ""),
                "issuer": clean(v.get("issuer") or extra.get("issuer") or ""),
                # Structured dates from the issuer win; a model's date must appear in the document text.
                "effective": extra.get("effective_on") or grounded_date(v.get("effective"), source),
                "comment_deadline": extra.get("comments_close_on") or grounded_date(v.get("comment_deadline"), source),
                "access_now": bool(v.get("access_now")) and v.get("status") == "in_force",
                "impact": impact,
                "ripples": [{"topic": r["topic"], "effect": clean(r.get("effect") or "")} for r in v.get("ripples") or []
                            if isinstance(r, dict) and r.get("topic") in topics][:2],
                "hats": {k: clean(x) for k, x in (v.get("hats") or {}).items() if isinstance(x, str) and x.strip() and k in hats},
            }
        return verdicts, dropped, {"lane": lane, "classified": len(verdicts), "errors": errors}
    return {}, [], {"lane": None, "classified": 0, "errors": errors}


def generate(items: list[dict], date: str, data_dir, state_dir, *, llm: bool = True) -> tuple[list[dict], dict]:
    """Returns (today's new constraint events, the day's constraints section)."""
    cache_path = state_dir / "constraints" / "verdicts.json"
    cache = load_json(cache_path, {})
    register = load_json(data_dir / "constraints.json", {"items": []})
    known = {r["key"]: r for r in register.get("items", [])}
    cands = candidates(items)
    fresh = [c for c in cands if c["_key"] not in cache]
    receipt = {"lane": None, "classified": 0, "errors": []}
    dropped: list[dict] = []
    if fresh and llm:
        verdicts, dropped, receipt = classify(fresh, date)
        cache.update(verdicts)
        write_json(cache_path, cache)
    elif fresh:
        receipt["errors"].append("the editor did not run; new candidates wait for the next edited run")

    # Merge duplicates the editor named, then build one record per kept action.
    kept = [(c, cache[c["_key"]]) for c in cands if cache.get(c["_key"], {}).get("kept")]
    id_to_key = {v.get("_id"): c["_key"] for c, v in kept}
    groups: dict[str, list[dict]] = {}
    for c, v in kept:
        lead = id_to_key.get(v.get("same_as")) or c["_key"]
        if lead not in {cc["_key"] for cc, _ in kept}:
            lead = c["_key"]
        groups.setdefault(lead, []).append(c)
    records = []
    for lead_key, group in groups.items():
        v = cache[lead_key]
        lead = next(c for c in group if c["_key"] == lead_key) if any(c["_key"] == lead_key for c in group) else group[0]
        prior = known.get(lead_key, {})
        rec = {
            "id": short_hash(lead_key, 12), "key": lead_key,
            "headline": v["headline"], "title": lead["title"], "harder": v["harder"], "who": v["who"], "why": v["why"],
            "limits": v["limits"], "status": v["status"], "instrument": v["instrument"], "layers": v["layers"],
            "jurisdiction": v["jurisdiction"], "issuer": v["issuer"], "effective": v["effective"],
            "comment_deadline": v["comment_deadline"], "access_now": v["access_now"], "impact": v["impact"],
            "ripples": v["ripples"], "hats": v["hats"],
            "url": lead["url"], "doc_url": lead.get("_doc") or lead["url"], "primary": any(c["_primary"] for c in group),
            "sources": [{"name": c["source_name"], "url": c["url"], "title": c["title"]} for c in group][:6],
            "published_at": max((c.get("published_at") or "" for c in group), default="") or None,
            "first_seen": prior.get("first_seen") or date, "last_seen": date,
        }
        rec["signal"], rec["parts"] = _signal(rec, date)
        records.append(rec)

    today = {r["key"] for r in records}
    events = [_event(r, date) for r in records if r["first_seen"] == date]
    section_items = [dict(r, new=r["first_seen"] == date) for r in records]
    for r in register.get("items", []):
        if r["key"] in today or not is_open(r, date):
            continue
        r = dict(r)
        r["signal"], r["parts"] = _signal(r, date)
        section_items.append(dict(r, new=False))
    section_items.sort(key=lambda r: (not r["new"], -r["signal"]))
    section = {"items": section_items[:12], "lane": receipt.get("lane"), "candidates": len(cands),
               "classified": receipt.get("classified", 0), "dropped": dropped, "errors": receipt.get("errors", []),
               "layers": LAYERS,
               "note": "Official actions with a document that make AI harder, slower or costlier to train, publish, host, reach or buy. Magpie reports the mechanism, not the politics."}
    return events, section


def is_open(rec: dict, date: str) -> bool:
    if rec.get("status") == "withdrawn":
        return False
    future = [d for d in (rec.get("comment_deadline"), rec.get("effective")) if d]
    if any(d >= date for d in future):
        return True
    first = rec.get("first_seen") or date
    return (dt.date.fromisoformat(date) - dt.date.fromisoformat(first)).days <= OPEN_DAYS


def _event(rec: dict, date: str) -> dict:
    """The same record as an event, so search, the Hoard and the MCP server find it like any capability."""
    return {
        "id": rec["id"], "key": rec["key"], "kind": "constraint", "title": rec["title"], "summary": rec["harder"],
        "doubt": None, "url": rec["url"], "date": date, "published_at": rec.get("published_at"),
        "vendors": [rec["issuer"]] if rec.get("issuer") else [], "maker": rec.get("issuer") or None,
        "topics": [r["topic"] for r in rec.get("ripples") or []],
        "evidence": "official" if rec.get("primary") else "reported",
        "signal": rec["signal"], "impact": rec.get("impact") or 2,
        "parts": {**rec["parts"], "ripple": 0.0, "momentum": 0.0}, "metrics": {},
        "language": None, "license": None, "created_at": None,
        "sources": [{"name": s["name"], "kind": "policy", "url": s["url"], "title": s["title"]} for s in rec["sources"]],
        "mentions": [], "source_groups": len({s["name"] for s in rec["sources"]}),
        "brief": {"headline": rec["headline"], "harder": rec["harder"], "who": rec["who"], "why": rec["why"],
                  "limits": rec["limits"], "impact": rec["impact"], "ripples": rec.get("ripples") or [],
                  "hats": rec.get("hats") or {}},
        "constraint": {k: rec.get(k) for k in ("status", "instrument", "layers", "jurisdiction", "issuer", "effective",
                                                "comment_deadline", "access_now", "doc_url", "primary")},
    }


def update_register(section: dict, date: str, data_dir) -> None:
    """Keep every constraint Magpie has seen, newest sighting wins, in data/v1/constraints.json."""
    path = data_dir / "constraints.json"
    register = load_json(path, {"schema": "magpie.constraints/1", "items": []})
    by_key = {r["key"]: r for r in register.get("items", [])}
    for r in section.get("items", []):
        if r.get("last_seen") != date:
            continue
        rec = {k: v for k, v in r.items() if k not in ("new", "parts")}
        old = by_key.get(r["key"], {})
        rec["first_seen"] = min(old.get("first_seen", date), rec.get("first_seen", date))
        by_key[r["key"]] = rec
    items = sorted(by_key.values(), key=lambda r: (r.get("last_seen", ""), r.get("signal", 0)), reverse=True)
    write_json(path, {"schema": "magpie.constraints/1", "updated": date, "layers": LAYERS, "items": items})
    print(f"constraints register: {len(items)} actions", file=sys.stderr)


def run_for(date: str | None = None) -> None:
    """Add the constraints lane and the trackers to an edition that is already published."""
    from collections import Counter

    from . import trackers
    from .collectors import policy
    from .publish import DATA, STATE, index_entry, scrub, write_feed

    path = DATA / "days" / f"{date}.json" if date else DATA / "latest.json"
    day = load_json(path, None)
    if not day:
        raise SystemExit(f"no edition at {path}")
    reg = load_json(ROOT / "config/sources.json", {})
    items, errors = [], []
    jobs = [policy.federal_register] + [lambda src=f: policy.policy_feed(src) for f in reg.get("policy_feeds", [])]
    for job in jobs:
        try:
            items += [i.to_dict() for i in job()]
        except Exception as ex:  # one source down must not stop the rest
            errors.append(f"{type(ex).__name__}: {str(ex)[:120]}")
    # On the home runner, the day's collected items add X posts and vendor notices that link a document.
    items += load_json(STATE / "runs" / day["date"] / "items.json", {}).get("items", [])
    events, section = generate(items, day["date"], DATA, STATE)
    section["errors"] = section.get("errors", []) + errors
    day["events"] = [e for e in day["events"] if e["kind"] != "constraint"] + events
    day["facets"]["kinds"] = Counter(e["kind"] for e in day["events"]).most_common()
    day["constraints"] = section
    day["trackers"] = trackers.build(day["date"], DATA, STATE)
    scrub(day)
    write_json(DATA / "days" / f"{day['date']}.json", day, compact=True)
    index = load_json(DATA / "index.json", {"schema": "magpie.index/1", "days": []})
    if index.get("latest") == day["date"]:
        write_json(DATA / "latest.json", day, compact=True)
    index["days"] = [index_entry(day) if d["date"] == day["date"] else d for d in index.get("days", [])]
    write_json(DATA / "index.json", index)
    update_register(section, day["date"], DATA)
    trackers.persist(day["date"], DATA)
    write_feed()
    print(f"constraints for {day['date']}: {len(section['items'])} ({section.get('lane') or section.get('errors')})", file=sys.stderr)
