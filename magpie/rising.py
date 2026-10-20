"""What put a repo on the rising list: its README's opening, and the buzz around it that day.

For every repo in the rising windows, attach `readme` (the opening of its README as plain text, a few
sentences) and `buzz` (the X posts, videos, Hacker News threads, newsletters and other sources that
mentioned it in the day's events), plus `event_id` when the repo is a published capability. The page's
side panel shows these, so a reader sees why a repo is climbing without leaving Magpie.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import urllib.request

from .util import clean, load_json, now_utc, write_json

README_CHARS = 520
CACHE_DAYS = 3


def _plain(md: str) -> str:
    """The README's opening prose: no badges, images, HTML, code, tables or link syntax."""
    md = re.sub(r"```.*?```", " ", md, flags=re.S)
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.S)
    md = re.sub(r"<[^>]+>", " ", md)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    md = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", md)
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)
    lines = []
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith(("|", "[", "---", "===", ">")) or re.match(r"^#+\s*$", s):
            continue
        if s.startswith("#"):
            continue  # headings are titles, not prose
        if s.count("\u00b7") >= 2 or s.count(" | ") >= 2 or s.count(" / ") >= 3:
            continue  # language switchers and nav rows
        s = re.sub(r"^[-*+]\s+|^\d+\.\s+", "", s)
        s = re.sub(r"[*_`]{1,3}", "", s)
        if len(s) > 30:
            lines.append(s)
        if sum(len(x) for x in lines) > README_CHARS * 1.5:
            break
    text = clean(" ".join(lines))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= README_CHARS:
        return text
    cut = text[:README_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > README_CHARS // 2 else cut.rsplit(" ", 1)[0] + "...").strip()


def _fetch_readme(repo: str, token: str | None, timeout: int = 10) -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/readme",
                                 headers={"Accept": "application/vnd.github.raw+json", "User-Agent": "magpie"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(200_000).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def readmes(repos: list[str], cache_dir, token: str | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    todo = []
    fresh = now_utc() - dt.timedelta(days=CACHE_DAYS)
    for r in repos:
        hit = load_json(cache_dir / f"{r.replace('/', '__')}.json", None)
        if hit and hit.get("at", "") >= fresh.isoformat():
            out[r] = hit.get("text", "")
        else:
            todo.append(r)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for r, md in zip(todo, ex.map(lambda r: _fetch_readme(r, token), todo)):
            text = _plain(md) if md else ""
            out[r] = text
            write_json(cache_dir / f"{r.replace('/', '__')}.json", {"at": now_utc().isoformat(), "text": text})
    return out


def _buzz(event: dict, limit: int = 10) -> list[dict]:
    """Where people talked about it: everything except the GitHub listings themselves."""
    seen, out = set(), []
    for s in (event.get("sources") or []) + (event.get("mentions") or []):
        if s.get("kind") == "repo" or "github.com" in (s.get("url") or "") or s.get("url") in seen:
            continue
        seen.add(s.get("url"))
        out.append({k: s[k] for k in ("name", "kind", "url", "title", "discussion") if s.get(k)})
        if len(out) >= limit:
            break
    return out


def enrich(rising: dict, events: list[dict], cache_dir, fetch: bool = True, cap: int = 90) -> dict:
    by_key = {e.get("key"): e for e in events}
    published = {e["id"] for e in events[:260]}
    repos: list[str] = []
    for rows in rising.values():
        for r in rows or []:
            if r.get("repo") and r["repo"] not in repos:
                repos.append(r["repo"])
    texts = readmes(repos[:cap], cache_dir, os.environ.get("GITHUB_TOKEN")) if fetch else {}
    for rows in rising.values():
        for r in rows or []:
            ev = by_key.get(f"gh:{r.get('repo', '').lower()}")
            if ev:
                r["buzz"] = _buzz(ev)
                if ev["id"] in published:
                    r["event_id"] = ev["id"]
            if texts.get(r.get("repo")):
                r["readme"] = texts[r["repo"]]
    return rising


def run_for(date: str | None = None) -> None:
    from .publish import DATA, STATE, scrub

    path = DATA / "days" / f"{date}.json" if date else DATA / "latest.json"
    day = load_json(path, None)
    if not day:
        raise SystemExit(f"no edition at {path}")
    day["rising"] = enrich(day["rising"], day["events"], STATE / "readme")
    scrub(day)
    write_json(DATA / "days" / f"{day['date']}.json", day, compact=True)
    if load_json(DATA / "index.json", {}).get("latest") == day["date"]:
        write_json(DATA / "latest.json", day, compact=True)
    rows = [r for rows in day["rising"].values() for r in rows or []]
    print(f"rising for {day['date']}: {sum(1 for r in rows if r.get('readme'))} readmes, "
          f"{sum(1 for r in rows if r.get('buzz'))} with buzz, of {len(rows)} rows", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(_plain(sys.stdin.read())))
