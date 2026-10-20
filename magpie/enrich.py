"""The editor: one model call writes the words for the day's strongest events.

Code has already scored and clustered everything. The model only writes headlines, what became
possible, ripple targets, an impact weight and the role "hats", grounded in the supplied
items. Any lane can do it; the first one available wins. With no lane, the edition still
publishes with deterministic copy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .util import ROOT, clean, load_json

PROMPT = """You are the editor of Magpie, a daily brief on what became newly possible in AI.
Today is {date}. Below are today's {n} strongest candidate events, already clustered and scored by code.
Write the words. Ground every statement in the supplied titles, summaries, metrics and sources. Do not
add facts, numbers, dates or names that are not in the input. Plain English, short sentences, no hype.
Never use the em-dash character.

Topic ids you may use for ripples and topics: {topic_ids}
Hat ids: {hat_ids}

For each event return:
- id: copy it
- keep: false if it is not a real AI capability (job posts, generic lists, drama, duplicates of another id)
- headline: 4 to 10 words, plain, names the thing
- newly_possible: one sentence on what someone can now do that they could not do before
- why: one sentence on why a builder or consultant should care
- try: one concrete 15-minute thing to try with the linked artifact, or "Read the source" if nothing is runnable
- limits: one short clause on what the claim does not prove yet
- impact: integer 1 to 5. How far this ripples into OTHER technologies and workflows. 5 = changes several
  adjacent stacks (a new open model class, a protocol everyone adopts, a big cost or capability threshold crossed);
  4 = reshapes one whole workflow; 3 = a solid new building block; 2 = incremental; 1 = niche. Use the whole scale:
  across the set, roughly one in five events should be a 4 or 5 and at least a quarter should be 1 or 2.
  Popularity alone is not impact. A paper without code rarely exceeds 3.
- ripples: 1 to 3 objects {{"topic": topic id, "effect": "up to 12 words on the knock-on effect"}}
- hats: object with every hat id as a key; each value is one implication of up to 20 words for that role,
  or "" when there is nothing specific to say. Hats add no new facts.
- topics: up to 3 topic ids
- vendor: the company or project behind it, or "Open source"
- hype: true if the evidence is mostly launch claims or screenshots

Also return an "edition" object:
- headline: up to 14 words, in the style of a "next new thing" episode title: the day's top capabilities as
  verbs, ending with "and more" if there were more than three
- dek: two sentences on the shape of the day
- quiet: true if fewer than three events are genuinely new capabilities

Return ONLY one JSON object: {{"edition": {{...}}, "events": [{{...}}]}}. No markdown fences, no prose.

EVENTS:
{events}
"""


def _payload(events: list[dict]) -> str:
    rows = []
    for e in events:
        m = e["metrics"]
        rows.append({
            "id": e["id"], "title": e["title"], "kind": e["kind"], "summary": e["summary"][:400],
            "vendors": e["vendors"], "topics": e["topics"], "evidence": e["evidence"],
            "metrics": {k: m[k] for k in ("stars_day", "stars_week", "stars_month", "stars_total", "hn_points", "hf_likes", "hf_upvotes") if k in m},
            "sources": [f"{s['name']}: {s['title']}" for s in e["sources"][:5]],
            "mentioned_by": [s["name"] for s in e["mentions"][:5]],
            "created": e.get("created_at"), "license": e.get("license"), "url": e["url"],
            "context": (e.get("_context") or [""])[0][:700],
        })
    return json.dumps(rows, ensure_ascii=False, indent=0)


QUOTAS = {"repo": 8, "model": 5, "space": 2, "paper": 5, "social": 5, "video": 4, "news": 3, "changelog": 3, "blog": 2, "community": 2}


def _pick_pool(events: list[dict], limit: int) -> list[dict]:
    """A mixed slate for the editor, so one source type cannot crowd out the rest."""
    ranked = sorted((e for e in events if e["kind"] != "newsletter"), key=lambda e: -e["signal"])
    pool, taken = [], {}
    for e in ranked:
        if taken.get(e["kind"], 0) < QUOTAS.get(e["kind"], 2):
            pool.append(e)
            taken[e["kind"]] = taken.get(e["kind"], 0) + 1
    for e in ranked:
        if len(pool) >= limit:
            break
        if e not in pool:
            pool.append(e)
    return pool[: max(limit, len(pool))][:limit + 6]


def _extract_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _run_claude(prompt: str, timeout: int) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise FileNotFoundError("claude")
    argv = [exe, "--print", "--model", os.environ.get("MAGPIE_MODEL", "opus"), "--output-format", "json",
            "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    if os.environ.get("MAGPIE_SAFE_MODE", "1") == "1":
        argv.insert(1, "--safe-mode")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(argv, input=prompt, cwd=tmp, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude rc {r.returncode}: {r.stderr[-400:]}")
    return json.loads(r.stdout).get("result", "")


def _run_codex(prompt: str, timeout: int) -> str:
    exe = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    if not Path(exe).exists():
        raise FileNotFoundError("codex")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "answer.md"
        r = subprocess.run([exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                            "-s", "read-only", "-o", str(out), "-"], input=prompt, cwd=tmp,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"codex rc {r.returncode}")
        return out.read_text()


LANES = {"claude": _run_claude, "codex": _run_codex}


def enrich(events: list[dict], date: str, limit: int = 26, timeout: int = 1200) -> tuple[dict, dict]:
    """Returns (edition, receipt). Mutates events in place with a `brief` field."""
    topics = load_json(ROOT / "config/topics.json", {}).get("topics", {})
    hats = [h["id"] for h in load_json(ROOT / "config/hats.json", {}).get("hats", [])]
    pool = _pick_pool(events, limit)
    prompt = PROMPT.format(date=date, n=len(pool), topic_ids=", ".join(topics), hat_ids=", ".join(hats),
                           events=_payload(pool))
    order = [l.strip() for l in os.environ.get("MAGPIE_LANES", "claude,codex").split(",") if l.strip()]
    errors = []
    for lane in order:
        try:
            data = _extract_json(LANES[lane](prompt, timeout))
        except Exception as ex:  # try the next lane
            errors.append(f"{lane}: {str(ex)[:200]}")
            continue
        by_id = {e["id"]: e for e in pool}
        n = 0
        for b in data.get("events", []):
            e = by_id.get(b.get("id"))
            if not e:
                continue
            if b.get("keep") is False:
                e["dropped"] = True
                continue
            brief = {k: clean(v) if isinstance(v, str) else v for k, v in b.items() if k not in ("id", "keep")}
            brief["hats"] = {k: clean(v) for k, v in (b.get("hats") or {}).items() if isinstance(v, str) and v.strip() and k in hats}
            brief["ripples"] = [r for r in (b.get("ripples") or []) if isinstance(r, dict) and r.get("topic") in topics][:3]
            try:
                brief["impact"] = max(1, min(5, int(b.get("impact") or 0))) or None
            except (TypeError, ValueError):
                brief["impact"] = None
            if b.get("topics"):
                e["topics"] = [t for t in b["topics"] if t in topics][:3] or e["topics"]
            if b.get("vendor") and b["vendor"] not in ("Open source", e.get("maker")):
                e["maker"] = b["vendor"]
                e["vendors"] = [b["vendor"]] + [v for v in e["vendors"] if v != b["vendor"]][:2]
            e["brief"] = brief
            n += 1
        edition = {k: clean(v) if isinstance(v, str) else v for k, v in (data.get("edition") or {}).items()}
        return edition, {"lane": lane, "enriched": n, "errors": errors}
    return {}, {"lane": None, "enriched": 0, "errors": errors}
