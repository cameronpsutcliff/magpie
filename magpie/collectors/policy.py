"""Primary policy sources for the constraints lane: official actions that change who can train, publish,
host, reach or buy AI capability.

Only documents from the issuer itself: the Federal Register (US rules, orders and notices, which also carry
the Commerce export-control rules), government newsrooms, and translations of Chinese rules. Each item is
tagged source_kind "policy" so the capability build leaves it alone; constraints.py reads them.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse

from ..items import Item
from ..util import edition_date, fetch, strip_html
from .feeds import collect_feed

# Words that make a government document about AI, the chips it runs on, or access to models. Acronyms are
# matched case-sensitively so "ai" inside other words and "Al" never count.
AI_ACRONYMS = re.compile(r"\b(?:A\.?I\.?|LLMs?|GPUs?|HBM|AGI)\b")
AI_WORDS = re.compile(
    r"\b(?:artificial intelligence|machine learning|large language models?|frontier (?:AI|models?)|"
    r"foundation models?|general[- ]purpose AI|model weights|open[- ]weights?|AI Act|advanced computing|"
    r"semiconductors?|integrated circuits?|AI chips?|high[- ]bandwidth memory|data cent(?:er|re)s?|"
    r"supercomput\w*|DeepSeek|Qwen|Kimi|Moonshot|Zhipu|OpenAI|Anthropic|Nvidia|Hugging ?Face|chatbots?|"
    r"deepfakes?|generative)\b", re.I)

FR_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_TERMS = ['"artificial intelligence"', '"advanced computing"', '"large language model"', '"model weights"',
            '"AI model"', "DeepSeek", '"integrated circuits"']
FR_AGENCIES = ["industry-and-security-bureau"]
FR_FIELDS = ["title", "type", "abstract", "action", "document_number", "html_url", "publication_date", "agencies",
             "effective_on", "comments_close_on", "dates"]


def is_ai_policy(text: str) -> bool:
    return bool(AI_ACRONYMS.search(text or "") or AI_WORDS.search(text or ""))


def _fr_query(extra: dict, since: str) -> list[dict]:
    q = [("per_page", "50"), ("order", "newest"), ("conditions[publication_date][gte]", since)]
    q += [("fields[]", f) for f in FR_FIELDS]
    q += list(extra.items())
    return json.loads(fetch(f"{FR_API}?{urllib.parse.urlencode(q)}", timeout=30)).get("results", [])


def federal_register(days: int = 4) -> list[Item]:
    """US rules, presidential documents and notices about AI, from the Federal Register API (no key)."""
    since = (dt.date.fromisoformat(edition_date()) - dt.timedelta(days=days)).isoformat()
    seen, out = set(), []
    queries = [{"conditions[term]": t} for t in FR_TERMS] + [{"conditions[agencies][]": a} for a in FR_AGENCIES]
    errors = []
    for extra in queries:
        try:
            results = _fr_query(extra, since)
        except Exception as ex:  # one failed query must not lose the rest
            errors.append(str(ex)[:120])
            continue
        for r in results:
            num = r.get("document_number")
            if not num or num in seen:
                continue
            text = " ".join(filter(None, [r.get("title"), r.get("abstract"), r.get("action")]))
            if not is_ai_policy(text):
                continue
            seen.add(num)
            agencies = [a.get("name") for a in r.get("agencies") or [] if a.get("name")]
            out.append(Item(
                source_id="federal-register",
                source_kind="policy",
                source_name="Federal Register",
                url=r.get("html_url") or f"https://www.federalregister.gov/d/{num}",
                title=strip_html(r.get("title"), 300),
                summary=strip_html(r.get("abstract") or r.get("action") or "", 900),
                published_at=f"{r.get('publication_date')}T12:00:00+00:00" if r.get("publication_date") else None,
                author=", ".join(agencies) or None,
                trust=0.95,
                keys=[f"policy:fr:{num}"],
                extra={"jurisdiction": "US", "issuer": agencies[0] if agencies else "Federal Register", "primary": True,
                       "doc_type": r.get("type"), "action": r.get("action"), "document_number": num,
                       "effective_on": r.get("effective_on"), "comments_close_on": r.get("comments_close_on"),
                       "dates_text": strip_html(r.get("dates") or "", 400)},
            ))
    if not out and len(errors) == len(queries):
        raise RuntimeError(f"Federal Register unreachable: {errors[0]}")
    return out


def policy_feed(src: dict) -> list[Item]:
    """A government newsroom or translation feed, kept to AI-related entries."""
    out = []
    for it in collect_feed(src):
        if src.get("ai_filter", True) and not is_ai_policy(f"{it.title} {it.summary}"):
            continue
        it.extra.update({"jurisdiction": src.get("jurisdiction"), "issuer": src.get("issuer") or src["name"],
                         "primary": bool(src.get("primary", True))})
        it.keys = [f"policy:{it.keys[0]}"] if it.keys else []
        out.append(it)
    return out
