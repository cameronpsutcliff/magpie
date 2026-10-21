"""The editor: one model call writes the words for the day's strongest events.

Code has already scored and clustered everything. The model only writes headlines, what became
possible, ripple targets, an impact weight, the role "hats" and the integrations the sources
name, grounded in the supplied items; code keeps a named integration only if the event's own text
spells it. Any lane can do it; the first one available wins. With no lane, the edition still
publishes with deterministic copy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .util import ROOT, WS, clean, load_json, ungrounded_numbers

PROMPT = """You are the editor of Magpie, a daily brief on what became newly possible in AI.
Today is {date}. Below are today's {n} strongest candidate events, already clustered and scored by code.
Write the words. Ground every statement in the supplied titles, summaries, metrics and sources. Do not
add facts, numbers, dates or names that are not in the input. Plain English, short sentences, no hype.
Never use the em-dash character.

Topic ids you may use for ripples and topics: {topic_ids}
Hat ids: {hat_ids}

For each event return:
- id: copy it
- keep: false if it is not a real AI capability (job posts, generic lists, drama, policy, regulation or political
  news, duplicates of another id)
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
- works_with: up to 5 objects {{"name": "...", "how": "..."}} for the tools, models or platforms this capability
  integrates with, runs on or plugs into, as this event's own title, summary, context or source titles state it.
  Copy each name exactly as it is written there; never the capability itself, its maker, a site that reported it, or
  a model, harness, benchmark or machine used only to test or measure it. Proper names only (a product, project,
  model, library, protocol or service), never a generic term, category, file format or endpoint type ("an
  OpenAI-compatible endpoint", "HTML", "local models"), and never an API the text names only as a bare word in a list
  ("the Responses and Converse APIs"). Never the model it was quantized, fine-tuned or distilled from: that is what it
  is, not what it works with.
  "how": up to 10 words on how they connect. [] when the input names none.

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


def _own_text(e: dict) -> str:
    """The event's own words as the editor saw them: title, summary, context and its sources' titles (not who posted them)."""
    parts = [e.get("title"), (e.get("summary") or "")[:400], (e.get("_context") or [""])[0][:700],
             *(s.get("title") for s in (e.get("sources") or [])[:5] if isinstance(s, dict))]
    return WS.sub(" ", " ".join(p for p in parts if isinstance(p, str)))


_ALNUM = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731  "Llama-3.1" and "llama 3.1" are one name
_TOKENS = lambda s: re.findall(r"[a-z0-9]+", s.lower())  # noqa: E731
_VERSION = re.compile(r"v?\d+[a-z]*")  # "2", "v2", "8b"
# Another build of the same model: quant formats and widths ("exl3", "gguf", "4bit", "q4"), and the usual variant words.
_QUANT = re.compile(r"exl\d*|gguf|ggml|awq|gptq|mlx|onnx|bnb|hqq|eetq|nf4|fp\d+|bf16|int\d+|q\d+|w\d+a\d+|\d+bit|\d+bpw|quant(ized)?")
_VARIANT = {"instruct", "chat", "base", "it", "hf"}
# Terms, not names, when a name is made only of them ("HTML", "API", "agents", "local models", "open source").
_GENERIC = {"html", "css", "json", "yaml", "xml", "csv", "markdown", "pdf", "http", "https", "rest", "url", "api", "apis", "sdk",
            "cli", "gpu", "gpus", "cpu", "cpus", "llm", "llms", "endpoint", "endpoints", "agent", "agents", "model", "models",
            "server", "servers", "provider", "providers", "tool", "tools", "plugin", "plugins", "framework", "frameworks", "library",
            "libraries", "app", "apps", "backend", "backends", "client", "clients", "platform", "platforms", "service", "services",
            "weights", "inference", "format", "formats", "file", "files", "local", "open", "source", "custom", "hosted", "remote",
            "cloud", "self", "any", "other", "third", "party"}
_CATEGORY = re.compile(r"\w-compatible\b|\bcompatible\s+(?:endpoint|api|server|provider|backend|client|interface)s?\b", re.I)
# What the capability came from, not what it works with: "fine-tuned from X", "distilled from X", "a quant of X".
_LINEAGE = re.compile(r"(?:fine[- ]?tuned|distilled|quanti[sz]ed|post[- ]trained|merged)\s+(?:from|on|of)\s+(?:the\s+|a\s+)?$"
                      r"|(?:quants?|quanti[sz]ations?|fine[- ]?tunes?|distillations?|merges?|gguf|exl\d|awq|gptq)\s+of\s+(?:the\s+|a\s+)?$", re.I)
# A bare word in a list of APIs: what follows "Responses" in "the Responses, Chat Completions, and Converse APIs".
_API_LIST = re.compile(r"(?:(?:\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+)[A-Z][\w.-]*(?:\s+[A-Z][\w.-]*)*)*\s+APIs?\b")
_PARTS = re.compile(r"\s+[|\u2013-]\s+|:\s+")  # "Canvas | GitHub Copilot", "Show HN: Kit"


def _itself(name: list[str], e: dict) -> bool:
    """Whether a name (as tokens) is the capability, its maker or a source that reported it. The capability: its title, a
    part of the title set off by "|", " - " or ":", its repo or model, its owner, any of those before a version
    ("Product" for "Product 2.0 adds agents"), or any of those followed only by build tokens ("Qwen3-8B" for
    qwen3-8b-instruct-gguf, the model a quant is of; but not "MLX" for "MLX ONNX converter"). The maker: any leading part
    of its name ("Acme" for "Acme, Inc.")."""
    title = e.get("title") if isinstance(e.get("title"), str) else ""
    key = str(e.get("key") or "")
    path = key.split(":", 1)[-1]
    forms = [title, *_PARTS.split(title), path.rsplit("/", 1)[-1]]
    if key.startswith(("gh:", "hf:", "hfs:")):
        forms.append(path.split("/", 1)[0])
    for f in forms:
        t = _TOKENS(f)
        rest = t[len(name):]
        if t[:len(name)] == name and ((rest and _VERSION.fullmatch(rest[0]))
                                      or all(_VERSION.fullmatch(x) or _QUANT.fullmatch(x) or x in _VARIANT for x in rest)):
            return True
    if isinstance(e.get("maker"), str) and _TOKENS(e["maker"])[:len(name)] == name:
        return True
    reporters = [s.get("name") for s in (e.get("sources") or []) + (e.get("mentions") or []) if isinstance(s, dict)]
    return any(isinstance(r, str) and _TOKENS(r) == name for r in reporters)


def _generic(name: str) -> bool:
    """A term, not a name: a category ("OpenAI-compatible endpoint") or only formats, common nouns and their usual
    modifiers ("HTML", "API", "agents", "local models")."""
    return bool(_CATEGORY.search(name)) or all(w in _GENERIC for w in _TOKENS(name))


def _not_an_integration(name: str, text: str) -> bool:
    """Whether the event's own text makes the name something other than an integration: what it was fine-tuned,
    distilled, quantized or merged from (anywhere), or only ever a bare word in a list of APIs."""
    spots = list(re.finditer(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.I))
    if any(_LINEAGE.search(text[max(0, m.start() - 48):m.start()]) for m in spots):
        return True
    return bool(spots) and all(_API_LIST.match(text, m.end()) for m in spots)


def plain_name(name: str, e: dict) -> bool:
    """Whether a name can stand as one of this event's integrations: a proper name, not the capability, its maker or a
    source in any form, and not what the text says it came from or a bare API word. Needs only the published event, so
    element pages apply it to earlier editions too (without the transcript context, which is never published)."""
    return (bool(re.search(r"[A-Za-z]", name)) and not _generic(name) and not _itself(_TOKENS(name), e)
            and not _not_an_integration(name, _own_text(e)))


def _blocked(*texts: str) -> bool:
    """Whether the redaction gate publish runs would stop on these words. Such an item is dropped here, so a name or clause
    (which may draw on a transcript that is never published) can never stop an edition."""
    from .publish import scrub
    try:
        scrub(list(texts))
    except SystemExit:
        return True
    return False


def works_with(raw, e: dict) -> list[dict]:
    """The editor's named integrations, kept only when the name is written in the event's own text, passes `plain_name`
    (a proper name, not the event, its maker or a source that reported it), every number in it is in that text, and the
    redaction gate passes it."""
    if not isinstance(raw, list):
        return []
    text = _own_text(e)
    out, seen = [], set()
    for w in raw:
        if not isinstance(w, dict) or not isinstance(w.get("name"), str):
            continue
        name = clean(WS.sub(" ", w["name"])).strip(" .,;:")
        how = clean(WS.sub(" ", w["how"])).strip() if isinstance(w.get("how"), str) else ""
        if len(how.split()) > 16:
            how = ""  # the name is what was checked; an overlong clause is dropped, not cut mid-thought
        if not 2 <= len(name) <= 60 or _ALNUM(name) in seen or not plain_name(name, e):
            continue
        found = re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.I)
        if not found or ungrounded_numbers([name, how], text) or _blocked(name, how):
            continue
        seen.add(_ALNUM(name))
        out.append({"name": found.group(0), **({"how": how} if how else {})})  # spelled as the sources spell it
    return out[:5]


def keep_works_with(brief: dict, e: dict) -> None:
    """Leave on the brief only the names that pass `works_with` against this event, or none. Run on a fresh answer and on
    a cached brief reused for a rebuilt event, whose text may have changed. Never raises: names are never worth an edition."""
    try:
        named = works_with(brief.get("works_with"), e)
    except Exception:
        named = []
    if named:
        brief["works_with"] = named
    else:
        brief.pop("works_with", None)


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
            keep_works_with(brief, e)  # after the maker is settled, so the maker never counts as an integration
            e["brief"] = brief
            n += 1
        edition = {k: clean(v) if isinstance(v, str) else v for k, v in (data.get("edition") or {}).items()}
        return edition, {"lane": lane, "enriched": n, "errors": errors}
    return {}, {"lane": None, "enriched": 0, "errors": errors}
