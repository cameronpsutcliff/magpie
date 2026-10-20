"""Hacker News and Hugging Face: community momentum and open models."""
from __future__ import annotations

import datetime as dt
import re

from ..items import Item
from ..util import fetch_json, now_utc, strip_html
from .github import AI_HINT


def hacker_news(hours: int = 30, min_points: int = 60) -> list[Item]:
    since = int((now_utc() - dt.timedelta(hours=hours)).timestamp())
    data = fetch_json(
        "https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=200"
        f"&numericFilters=created_at_i>{since},points>{min_points}"
    )
    out = []
    for h in data.get("hits", []):
        title = h.get("title") or ""
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        if not (AI_HINT.search(title) or re.search(r"github\.com|huggingface\.co|arxiv\.org", url)):
            continue
        out.append(Item(
            source_id="hacker-news",
            source_kind="community",
            source_name="Hacker News",
            url=url,
            title=strip_html(title),
            published_at=h.get("created_at"),
            author=h.get("author"),
            trust=0.65,
            metrics={"hn_points": h.get("points"), "hn_comments": h.get("num_comments")},
            extra={"discussion": f"https://news.ycombinator.com/item?id={h['objectID']}"},
        ).finalize())
    return out


def hf_models(limit: int = 30) -> list[Item]:
    data = fetch_json(f"https://huggingface.co/api/models?sort=trendingScore&limit={limit}&full=false")
    out = []
    for m in data:
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        out.append(Item(
            source_id="hf-trending-models",
            source_kind="model",
            source_name="Hugging Face trending models",
            url=f"https://huggingface.co/{mid}",
            title=mid,
            summary=(m.get("pipeline_tag") or "").replace("-", " "),
            published_at=m.get("createdAt"),
            trust=0.7,
            metrics={"hf_trending": m.get("trendingScore"), "hf_likes": m.get("likes"), "hf_downloads": m.get("downloads")},
            extra={"pipeline": m.get("pipeline_tag"), "library": m.get("library_name"), "tags": (m.get("tags") or [])[:12]},
        ).finalize())
    return out


def hf_spaces(limit: int = 20) -> list[Item]:
    data = fetch_json(f"https://huggingface.co/api/spaces?sort=trendingScore&limit={limit}")
    out = []
    for s in data:
        sid = s.get("id")
        if not sid:
            continue
        out.append(Item(
            source_id="hf-trending-spaces",
            source_kind="space",
            source_name="Hugging Face trending spaces",
            url=f"https://huggingface.co/spaces/{sid}",
            title=sid,
            summary=strip_html((s.get("cardData") or {}).get("short_description") or "", 200),
            published_at=s.get("createdAt"),
            trust=0.55,
            metrics={"hf_trending": s.get("trendingScore"), "hf_likes": s.get("likes")},
            extra={"sdk": s.get("sdk")},
        ).finalize())
    return out


def hf_papers(limit: int = 40) -> list[Item]:
    data = fetch_json(f"https://huggingface.co/api/daily_papers?limit={limit}")
    out = []
    for p in data:
        paper = p.get("paper") or {}
        pid = paper.get("id")
        if not pid:
            continue
        repo = paper.get("githubRepo") or ""
        body = f"{repo} {paper.get('projectPage') or ''}"
        it = Item(
            source_id="hf-daily-papers",
            source_kind="paper",
            source_name="Hugging Face daily papers",
            url=f"https://huggingface.co/papers/{pid}",
            title=strip_html(paper.get("title") or p.get("title") or pid),
            summary=strip_html(paper.get("ai_summary") or paper.get("summary") or "", 420),
            published_at=p.get("publishedAt") or paper.get("publishedAt"),
            trust=0.6,
            metrics={"hf_upvotes": paper.get("upvotes"), "gh_stars": paper.get("githubStars")},
            extra={"github": repo or None, "keywords": (paper.get("ai_keywords") or [])[:10],
                   "org": (paper.get("organization") or {}).get("fullname")},
        )
        it.keys = [f"arxiv:{pid}"]
        out.append(it.finalize(body))
    return out
