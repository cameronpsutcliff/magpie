"""GitHub: trending (stars gained per window) and brand-new repos rising fast."""
from __future__ import annotations

import datetime as dt
import html
import re

from ..items import Item
from ..util import fetch, fetch_json, github_headers, iso, now_utc, strip_html

WINDOWS = {"daily": "day", "weekly": "week", "monthly": "month"}
AI_HINT = re.compile(
    r"\b(ai|llm|llms|agent|agents|agentic|gpt|claude|gemini|grok|llama|qwen|deepseek|mistral|model|models|"
    r"inference|rag|mcp|embedding|diffusion|transformer|neural|ml|copilot|codex|cursor|prompt|chatbot|"
    r"multimodal|vision|speech|tts|voice|autonomous|reasoning|fine-tun\w*|lora|quantiz\w*|gguf|vllm|"
    r"ollama|langchain|openai|anthropic|hugging ?face|robot\w*|eval\w*|benchmark\w*|memory|skills?)\b",
    re.I,
)


def _parse_trending(page: str, window: str) -> list[dict]:
    rows = []
    for art in page.split('<article class="Box-row">')[1:]:
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="/([^"/]+)/([^"/]+)"', art, re.S)
        if not m:
            continue
        owner, repo = m.group(1), m.group(2)
        desc = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', art, re.S)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        total = re.search(r'href="/[^"]+/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', art, re.S)
        gained = re.search(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)", art)
        rows.append({
            "repo": f"{owner}/{repo}",
            "description": strip_html(html.unescape(desc.group(1))) if desc else "",
            "language": lang.group(1).strip() if lang else None,
            "stars_total": int(total.group(1).replace(",", "")) if total else None,
            "stars_window": int(gained.group(1).replace(",", "")) if gained else 0,
            "window": window,
        })
    return rows


def trending() -> tuple[list[Item], dict]:
    items: list[Item] = []
    rising: dict[str, list[dict]] = {}
    for since, window in WINDOWS.items():
        page = fetch(f"https://github.com/trending?since={since}").decode("utf-8", "replace")
        rows = _parse_trending(page, window)
        for i, r in enumerate(rows):
            r["rank"] = i + 1
            r["ai"] = bool(AI_HINT.search(f"{r['repo']} {r['description']}"))
        rising[window] = rows
        for r in rows:
            items.append(Item(
                source_id=f"github-trending-{since}",
                source_kind="repo",
                source_name=f"GitHub trending ({window})",
                url=f"https://github.com/{r['repo']}",
                title=r["repo"],
                summary=r["description"],
                trust=0.7,
                metrics={f"stars_{window}": r["stars_window"], "stars_total": r["stars_total"], f"trend_rank_{window}": r["rank"]},
                extra={"language": r["language"], "ai": r["ai"]},
            ).finalize())
    return items, rising


def new_and_rising(days: int = 10, limit: int = 30) -> list[Item]:
    since = (now_utc() - dt.timedelta(days=days)).date().isoformat()
    q = f"created:>{since} stars:>80"
    data = fetch_json(
        f"https://api.github.com/search/repositories?q={q.replace(' ', '+')}&sort=stars&order=desc&per_page={limit}",
        headers=github_headers(),
    )
    out = []
    for r in data.get("items", []):
        text = f"{r['full_name']} {r.get('description') or ''} {' '.join(r.get('topics') or [])}"
        out.append(Item(
            source_id="github-new-rising",
            source_kind="repo",
            source_name="GitHub new and rising",
            url=r["html_url"],
            title=r["full_name"],
            summary=strip_html(r.get("description") or "", 300),
            published_at=r.get("created_at"),
            trust=0.6,
            metrics={"stars_total": r.get("stargazers_count"), "forks": r.get("forks_count")},
            extra={"language": r.get("language"), "topics": r.get("topics") or [],
                   "license": (r.get("license") or {}).get("spdx_id"), "created_at": r.get("created_at"),
                   "ai": bool(AI_HINT.search(text))},
        ).finalize())
    return out


def repo_details(full_names: list[str]) -> dict[str, dict]:
    """Created date, license and topics for repos that made the shortlist."""
    out = {}
    for name in full_names:
        try:
            r = fetch_json(f"https://api.github.com/repos/{name}", headers=github_headers(), retries=0)
        except Exception:
            continue
        out[name.lower()] = {
            "created_at": r.get("created_at"),
            "pushed_at": r.get("pushed_at"),
            "license": (r.get("license") or {}).get("spdx_id"),
            "topics": r.get("topics") or [],
            "homepage": r.get("homepage"),
            "stars_total": r.get("stargazers_count"),
            "owner_type": (r.get("owner") or {}).get("type"),
            "checked_at": iso(now_utc()),
        }
    return out
