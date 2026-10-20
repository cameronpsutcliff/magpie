"""RSS and Atom: newsletters, blogs, research feeds and product changelogs."""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET

from ..items import Item
from ..util import fetch, now_utc, parse_time, strip_html

# For feeds that cover more than AI: keep an entry only when its title or summary is about AI.
AI_TEXT = re.compile(r"\b(?:AI|A\.I\.|LLMs?|GenAI|gen-?AI|agents?|agentic|generative|machine learning|language models?|"
                     r"copilots?|coding assistants?|prompts?|inference|model weights|open[- ]weights?|RAG|MCP|"
                     r"Claude|GPT|Gemini|Llama|Qwen|DeepSeek)\b")

NS = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/",
      "dc": "http://purl.org/dc/elements/1.1/", "media": "http://search.yahoo.com/mrss/"}


def _text(el, path: str) -> str:
    x = el.find(path, NS)
    return (x.text or "") if x is not None and x.text else ""


def parse_feed(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    entries = []
    if root.tag.endswith("rss") or root.find("channel") is not None:
        for it in root.iter("item"):
            entries.append({
                "title": _text(it, "title"),
                "url": _text(it, "link").strip(),
                "published": _text(it, "pubDate") or _text(it, "dc:date"),
                "author": _text(it, "dc:creator") or _text(it, "author"),
                "summary": _text(it, "description"),
                "body": _text(it, "content:encoded") or _text(it, "description"),
            })
    else:
        for it in root.iter("{http://www.w3.org/2005/Atom}entry"):
            link = ""
            for l in it.findall("atom:link", NS):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            entries.append({
                "title": _text(it, "atom:title"),
                "url": link,
                "published": _text(it, "atom:published") or _text(it, "atom:updated"),
                "author": _text(it, "atom:author/atom:name"),
                "summary": _text(it, "atom:summary") or _text(it, "media:group/media:description"),
                "body": _text(it, "atom:content") or _text(it, "atom:summary"),
            })
    return entries


def collect_feed(src: dict) -> list[Item]:
    raw = fetch(src["url"])
    hours = src.get("window_hours", 36)
    cutoff = now_utc() - dt.timedelta(hours=hours)
    out = []
    for e in parse_feed(raw)[: src.get("max_entries", 60)]:
        t = parse_time(e["published"])
        if t is None or t < cutoff or not e["url"]:
            continue
        title = strip_html(e["title"], 220)
        if src.get("title_must") and not any(w.lower() in title.lower() for w in src["title_must"]):
            continue
        if src.get("ai_only") and not AI_TEXT.search(f"{title} {e['summary'] or e['body']}"):
            continue  # a company or author feed that covers more than AI
        out.append(Item(
            source_id=src["id"],
            source_kind=src["kind"],
            source_name=src["name"],
            url=e["url"],
            title=title,
            summary=strip_html(e["summary"] or e["body"], 420),
            published_at=t.isoformat(),
            author=strip_html(e["author"]) or None,
            vendor_hint=src.get("vendor"),
            trust=src.get("trust", 0.6),
            extra={"product": src.get("product")},
        ).finalize(e["body"] if src["kind"] in ("newsletter", "blog", "news", "community") else ""))
    return out
