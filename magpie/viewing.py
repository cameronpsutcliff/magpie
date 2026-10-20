"""Recommended viewing: videos worth someone's time, judged from what was actually said in them.

Candidates are the day's transcribed videos, written by `magpie youtube` (magpie/ytintel.py) to
MAGPIE_YT_HOME/transcripts/<date>/<id>.md. Code filters them (length, AI relevance, not already recommended this week); the editor reads an
excerpt of each transcript and returns watch, skim or skip, with what a viewer learns and any caution.
Code ranks the keepers. X videos come from Magpie Scout, which vets them on X before they arrive.
Transcripts stay private: only titles, links and the editor's own words reach the page.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from .enrich import LANES, _extract_json
from .util import ROOT, clean, load_json, write_json

AI_WORDS = re.compile(r"\b(ai|llm|llms|gpt|claude|gemini|grok|llama|qwen|deepseek|mistral|agent|agents|agentic|model|models|"
                      r"inference|transformer|diffusion|fine-?tun\w*|rag|embedding\w*|prompt\w*|mcp|copilot|cursor|codex|"
                      r"open[- ]weights?|benchmark\w*|eval\w*|robot\w*|gpu\w*|token\w*|neural|machine learning|"
                      r"reinforcement learning|multimodal|vision|speech|voice)\b", re.I)
VERDICTS = ("watch", "skim", "skip")

PROMPT = """You are the viewing editor for Magpie, a public daily brief on what became newly possible in AI.
Below are {n} videos published in the last day or two, each with an excerpt of its transcript. Decide which ones
are worth a busy builder's time. Be strict: most AI video is recap, reaction, hype or advertising.

Recommend "watch" only when the video shows or explains something substantive about a new capability: a working
demo, a build walkthrough, a measured result, an expert explaining how something works, or a first-hand account
from the people who built it. Use "skim" when there is one useful idea inside a lot of filler. Use "skip" for
news recaps, reactions, clickbait titles the content does not back up, sponsored pitches, course or community
sales, finance and politics, and anything that is mostly speculation.

Rules: judge only from the excerpt and title. Plain English, short sentences. No numbers that are not in the
excerpt. Never use the em-dash character.

For each video return: id, verdict ("watch" | "skim" | "skip"), substance (1 to 5), novelty (1 to 5),
hype (1 to 5, 5 = mostly hype), why (one sentence a reader sees: what makes it worth or not worth the time),
learn (2 or 3 short phrases: what a viewer comes away with), audience (who should watch), caution (one short
phrase if a claim in it is unverified or a section can be skipped, else "").

Return ONLY one JSON object: {{"videos": [...]}}. No markdown fences.

VIDEOS:
{videos}
"""


def _seconds(text: str | int | None) -> int | None:
    if isinstance(text, (int, float)):
        return int(text)
    if not text:
        return None
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    s = re.search(r"(\d+)\s*s", text)
    if not (h or m or s):
        return None
    return (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0) + (int(s.group(1)) if s else 0)


def _video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _from_home(date: str, days: int = 2) -> dict[str, dict]:
    """Transcripts the home YouTube pipeline already made: header fields plus the text after 'Transcript:'."""
    home = os.environ.get("MAGPIE_YT_HOME")
    out: dict[str, dict] = {}
    if not home:
        return out
    d0 = dt.date.fromisoformat(date)
    for k in range(days):
        folder = Path(home) / "transcripts" / (d0 - dt.timedelta(days=k)).isoformat()
        if not folder.is_dir():
            continue
        for f in folder.glob("*.md"):
            text = f.read_text(errors="ignore")
            head, _, body = text.partition("Transcript:")
            url = (re.search(r"\*\*URL\*\*:\s*(\S+)", head) or [None, ""])[1]
            vid = _video_id(url)
            if not vid or vid in out:
                continue
            out[vid] = {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                        "title": clean((re.search(r"^# (.+)$", head, re.M) or [None, ""])[1]),
                        "channel": clean((re.search(r"\*\*Channel\*\*:\s*(.+)", head) or [None, ""])[1]),
                        "duration_s": _seconds((re.search(r"\*\*Duration\*\*:\s*(.+)", head) or [None, ""])[1]),
                        "transcript": " ".join(body.split())}
    return out


def _relevant(c: dict, own_channels: set[str]) -> bool:
    if c.get("channel", "").lower() in own_channels:
        return True
    return len(AI_WORDS.findall(f"{c['title']} {c['transcript'][:4000]}")) >= 4


def candidates(date: str, recent_ids: set[str], limit: int = 24) -> list[dict]:
    from .ytintel import roster
    channels = roster() or load_json(ROOT / "config/youtube.json", {}).get("channels", [])
    own_names = {c.get("name", "").lower() for c in load_json(ROOT / "config/youtube.json", {}).get("channels", [])}
    prio = {c.get("name", "").lower(): (0 if c.get("priority") == "high" else 1) for c in channels}
    keep = []
    for c in _from_home(date).values():
        words = len(c["transcript"].split())
        if c["id"] in recent_ids or words < 600:
            continue
        if c.get("duration_s") is not None and not 240 <= c["duration_s"] <= 9000:
            continue
        if not _relevant(c, own_names):
            continue
        keep.append(c)
    keep.sort(key=lambda c: (prio.get(c["channel"].lower(), 2), -len(c["transcript"].split())))
    return keep[:limit]


def _excerpt(text: str, head: int = 1500, tail: int = 250) -> str:
    w = text.split()
    return " ".join(w[:head]) + (" ... " + " ".join(w[-tail:]) if len(w) > head + tail else " ".join(w[head:]))


def _grounded(v: dict, transcript: str) -> bool:
    claim = " ".join([v.get("why", ""), *v.get("learn", [])])
    return all(n in transcript for n in re.findall(r"\d+(?:\.\d+)?", claim))


def vet(cands: list[dict], cache_dir: Path, timeout: int = 1200) -> tuple[dict[str, dict], str | None, list[str]]:
    verdicts: dict[str, dict] = {}
    todo = []
    for c in cands:
        hit = load_json(cache_dir / f"{c['id']}.json", None)
        if hit:
            verdicts[c["id"]] = hit
        else:
            todo.append(c)
    if not todo:
        return verdicts, "cache", []
    shown = [{"id": c["id"], "title": c["title"], "channel": c["channel"],
              "minutes": round(c["duration_s"] / 60) if c.get("duration_s") else None, "excerpt": _excerpt(c["transcript"])}
             for c in todo]
    prompt = PROMPT.format(n=len(shown), videos=json.dumps(shown, ensure_ascii=False))
    errors = []
    for lane in ("claude", "codex"):
        raw = ""
        try:
            raw = LANES[lane](prompt, timeout)
            data = _extract_json(raw)
        except Exception as ex:
            errors.append(f"{lane}: {str(ex)[:160]}")
            continue
        for v in data.get("videos", []):
            if v.get("id") not in {c["id"] for c in todo} or v.get("verdict") not in VERDICTS:
                continue
            v = {k: (clean(x) if isinstance(x, str) else x) for k, x in v.items()}
            v["learn"] = [clean(x) for x in v.get("learn", []) if isinstance(x, str)][:3]
            write_json(cache_dir / f"{v['id']}.json", v)
            verdicts[v["id"]] = v
        return verdicts, lane, errors
    return verdicts, None, errors


def _score(v: dict) -> float:
    g = lambda k: max(1, min(5, int(v.get(k) or 1)))  # noqa: E731
    return 2 * g("substance") + g("novelty") - g("hype")


def x_videos(items: list[dict], limit: int = 3) -> list[dict]:
    """Videos Magpie Scout vetted on X: native video it judged worth watching, with no strong doubt."""
    out = []
    for it in items:
        ex = it.get("extra") or {}
        video = ex.get("video") or {}
        doubt = (ex.get("doubt") or {}).get("level", 0)
        if it.get("source_id") != "x-grok-bot" or not video.get("worth_watching") or doubt >= 2:
            continue
        out.append({"platform": "x", "id": it["url"].rsplit("/", 1)[-1], "url": it["url"], "title": clean(it.get("title", "")),
                    "channel": it.get("author") or "", "duration_s": video.get("duration_s"),
                    "thumbnail": video.get("thumbnail") if str(video.get("thumbnail", "")).startswith("https://pbs.twimg.com/") else None,
                    "why": clean(video.get("why", "")), "learn": [clean(x) for x in video.get("learn", []) if isinstance(x, str)][:3],
                    "audience": clean(video.get("audience", "")), "caution": "", "verdict": "watch",
                    "reception": (ex.get("reception") or {}).get("sentiment")})
    return out[:limit]


def recently_recommended(date: str, data_dir: Path, days: int = 7) -> set[str]:
    ids = set()
    d0 = dt.date.fromisoformat(date)
    for k in range(1, days + 1):
        p = load_json(data_dir / "days" / f"{(d0 - dt.timedelta(days=k)).isoformat()}.json", None) or {}
        ids |= {v.get("id") for v in (p.get("viewing") or {}).get("items", [])}
    return ids


def generate(date: str, items: list[dict], data_dir: Path, state_dir: Path, top: int = 5) -> dict:
    recent = recently_recommended(date, data_dir)
    cands = candidates(date, recent)
    verdicts, lane, errors = vet(cands, state_dir / "viewing") if cands else ({}, None, [])
    by_id = {c["id"]: c for c in cands}
    keep = []
    for vid, v in verdicts.items():
        c = by_id.get(vid)
        if not c or v.get("verdict") != "watch" or not _grounded(v, c["transcript"]):
            continue
        keep.append({"platform": "youtube", "id": vid, "url": c["url"], "title": c["title"], "channel": c["channel"],
                     "duration_s": c.get("duration_s"), "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                     "why": v.get("why", ""), "learn": v.get("learn", []), "audience": v.get("audience", ""),
                     "caution": v.get("caution", ""), "verdict": "watch",
                     "scores": {k: v.get(k) for k in ("substance", "novelty", "hype")}, "_score": _score(v)})
    keep.sort(key=lambda x: -x["_score"])
    for x in keep:
        x.pop("_score")
    picked = keep[:top] + x_videos(items)
    skims = sum(1 for v in verdicts.values() if v.get("verdict") == "skim")
    return {"items": picked, "considered": len(cands), "watch": len(keep), "skim": skims,
            "skip": sum(1 for v in verdicts.values() if v.get("verdict") == "skip"), "lane": lane, "errors": errors,
            "note": "Judged from each video's transcript, not its title. Most AI video is recap or hype; these are the exceptions."}


def run_for(date: str | None = None) -> None:
    from .publish import DATA, STATE, scrub

    path = DATA / "days" / f"{date}.json" if date else DATA / "latest.json"
    day = load_json(path, None)
    if not day:
        raise SystemExit(f"no edition at {path}")
    items = load_json(STATE / "runs" / day["date"] / "items.json", {}).get("items", [])
    day["viewing"] = generate(day["date"], items, DATA, STATE)
    scrub(day)
    write_json(DATA / "days" / f"{day['date']}.json", day, compact=True)
    if load_json(DATA / "index.json", {}).get("latest") == day["date"]:
        write_json(DATA / "latest.json", day, compact=True)
    v = day["viewing"]
    print(f"viewing for {day['date']}: {len(v['items'])} picked of {v['considered']} considered "
          f"(watch {v['watch']}, skim {v['skim']}, skip {v['skip']}; {v['lane'] or v['errors']})", file=sys.stderr)
