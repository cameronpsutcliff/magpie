"""YouTube intel: the one job that finds, transcribes and briefs the day's videos.

It replaces the old youtube-intel-pipeline.sh (kept on disk for rollback) and writes the same files in
the same place (MAGPIE_YT_HOME), so every reader keeps working: the morning email, sharpen-self's
Chroma ingest, the afternoon collector, and Magpie itself.

  transcripts/<date>/<id>.json   raw `summarize --extract --json` output
  transcripts/<date>/<id>.md     title, channel, URL, duration, words, then the transcript
  transcripts/<date>/discovered.json
  index/processed.json           every video ever handled, so nothing repeats
  briefs/<date>-brief.md         the day's videos, channel priority first, then depth

What changed from the old job: it reads both channel lists (Magpie's curated config/youtube.json and the
subscription mirror config/channels.json, which only the subscription sync writes); it looks back 36 hours
against the processed index instead of 12, so uploads between runs are no longer missed; it skips Shorts;
it transcribes four videos at a time; and it falls back to YouTube captions when summarize fails.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .util import CENTRAL, ROOT, load_json, now_utc

NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
PRIO_RANK = {"high": 0, "medium": 1, "low": 2}
SUMMARIZE = shutil.which("summarize") or "/opt/homebrew/bin/summarize"


def home() -> Path:
    h = os.environ.get("MAGPIE_YT_HOME")
    if not h:
        raise SystemExit("MAGPIE_YT_HOME is not set; it names the folder that holds transcripts/, briefs/ and index/")
    return Path(h)


def roster(yt_home: Path | None = None) -> list[dict]:
    """Magpie's curated channels plus the subscription mirror, one row per channel.

    Subscription channels marked ignore or off stay out (the 2026-07-17 channel audit), unless Magpie
    curates them on purpose. Per-channel title filters from the mirror still apply.
    """
    yt_home = yt_home or (Path(os.environ["MAGPIE_YT_HOME"]) if os.environ.get("MAGPIE_YT_HOME") else None)
    subs = load_json(yt_home / "config/channels.json", []) if yt_home else []
    curated = load_json(ROOT / "config/youtube.json", {}).get("channels", [])
    out: dict[str, dict] = {}
    for c in curated:
        if c.get("channel_id"):
            out[c["channel_id"]] = {"channel_id": c["channel_id"], "name": c.get("name", ""), "priority": c.get("priority", "medium"),
                                    "title_filter": [], "from": "magpie"}
    for c in subs if isinstance(subs, list) else []:
        cid = c.get("channel_id")
        if not cid:
            continue
        tf = c.get("title_filter") or []
        tf = [tf] if isinstance(tf, str) else tf
        if cid in out:
            out[cid]["title_filter"] = [t.lower() for t in tf if t]
            out[cid]["from"] = "both"
            if c.get("priority") == "high":
                out[cid]["priority"] = "high"
            continue
        if c.get("priority") in ("ignore", "off"):
            continue
        out[cid] = {"channel_id": cid, "name": c.get("name", ""), "priority": c.get("priority", "medium"),
                    "title_filter": [t.lower() for t in tf if t], "from": "subscriptions"}
    return list(out.values())


def sync_roster(yt_home: Path) -> int:
    """Make config/channels.json the one roster every reader ranks by: append Magpie's curated channels it lacks.

    Append-only, like the subscription sync: existing rows (priorities, ignore marks, title filters) never change.
    """
    path = yt_home / "config/channels.json"
    rows = load_json(path, None)
    if not isinstance(rows, list):
        return 0
    have = {r.get("channel_id") for r in rows}
    today = now_utc().astimezone(CENTRAL).date().isoformat()
    new = [{"channel_id": c["channel_id"], "name": c.get("name", ""), "priority": c.get("priority", "medium"),
            "added": today, "added_by": "magpie"}
           for c in load_json(ROOT / "config/youtube.json", {}).get("channels", [])
           if c.get("channel_id") and c["channel_id"] not in have]
    if not new:
        return 0
    backup = path.with_name(f"channels.json.bak-{today}")
    if not backup.exists():
        backup.write_text(path.read_text())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows + new, indent=2))
    tmp.replace(path)
    return len(new)


def _feed(ch: dict, cutoff: dt.datetime, processed: set[str]) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['channel_id']}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        root = ET.fromstring(resp.read())
    found = []
    for entry in root.findall("atom:entry", NS)[:15]:
        vid = entry.findtext("yt:videoId", namespaces=NS)
        el = entry.find("atom:link", NS)  # an Element with no children is falsy, so test against None
        link = el.get("href", "") if el is not None else ""
        if not vid or vid in processed or "/shorts/" in link:
            continue
        published = entry.findtext("atom:published", namespaces=NS)
        if published and dt.datetime.fromisoformat(published.replace("Z", "+00:00")) < cutoff:
            continue
        title = entry.findtext("atom:title", default="Unknown", namespaces=NS)
        if ch["title_filter"] and not any(t in title.lower() for t in ch["title_filter"]):
            continue
        found.append({"video_id": vid, "title": title, "channel": ch["name"], "priority": ch["priority"],
                      "url": f"https://www.youtube.com/watch?v={vid}", "published": published})
    return found


def discover(channels: list[dict], processed: set[str], hours: int) -> tuple[list[dict], list[str]]:
    cutoff = now_utc() - dt.timedelta(hours=hours)
    found, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_feed, ch, cutoff, processed): ch for ch in channels}
        for fu in cf.as_completed(futs):
            try:
                found += fu.result()
            except Exception as e:  # one dead feed never stops the run
                errors.append(f"{futs[fu]['name']}: {str(e)[:80]}")
    seen, out = set(), []
    for v in sorted(found, key=lambda v: v.get("published") or ""):
        if v["video_id"] not in seen:
            seen.add(v["video_id"])
            out.append(v)
    return out, errors


def _vtt_text(vtt: str) -> str:
    lines, last = [], None
    for line in vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.isdigit():
            continue
        if line != last:  # auto captions repeat each line as it scrolls
            lines.append(line)
            last = line
    return " ".join(lines)


def _captions(vid: str, timeout: int = 90) -> str:
    """YouTube captions through yt-dlp, the fallback when summarize cannot extract a transcript."""
    exe = shutil.which("yt-dlp")
    if not exe:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        argv = [exe, "--skip-download", "--write-auto-subs", "--write-subs", "--sub-langs", "en.*,en", "--sub-format", "vtt",
                "--no-warnings", "--quiet", "-o", f"{tmp}/%(id)s.%(ext)s", f"https://www.youtube.com/watch?v={vid}"]
        try:
            subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ""
        files = sorted(Path(tmp).glob(f"{vid}*.vtt"))
        return _vtt_text(files[0].read_text(errors="ignore")) if files else ""


def _summarize(url: str, timeout: int) -> tuple[str, dict] | None:
    try:
        r = subprocess.run([SUMMARIZE, "--extract", "--json", url], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(r.stdout)
    except json.JSONDecodeError:
        return None
    return r.stdout, data


def extract(v: dict, folder: Path, timeout: int = 180) -> dict | None:
    """Transcript for one video, written as <id>.json and <id>.md. None when there is nothing usable."""
    got = _summarize(v["url"], timeout)
    if got:
        raw, data = got
        ext = data.get("extracted", {})
        content, words = ext.get("content", ""), ext.get("wordCount", 0)
        duration, title = ext.get("mediaDurationSeconds"), ext.get("title") or v["title"]
    else:
        text = _captions(v["video_id"])
        if not text:
            return None
        content, words, duration, title = f"Transcript: {text}", len(text.split()), None, v["title"]
        raw = json.dumps({"extracted": {"url": v["url"], "title": title, "content": content, "wordCount": words,
                                        "transcriptSource": "yt-dlp captions"}}, ensure_ascii=False)
    if isinstance(duration, (int, float)) and duration < 75:
        return {"skip": "short"}
    if not content or words < 50:
        return None
    duration_str = f"{int(duration) // 60}m {int(duration) % 60}s" if isinstance(duration, (int, float)) else "unknown"
    (folder / f"{v['video_id']}.json").write_text(raw)
    (folder / f"{v['video_id']}.md").write_text(
        f"# {title}\n\n- **Channel**: {v['channel']}\n- **URL**: {v['url']}\n- **Duration**: {duration_str}\n"
        f"- **Words**: {words}\n- **Extracted**: {now_utc().strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n---\n\n{content}")
    return {"title": title, "words": words}


def write_brief(folder: Path, brief: Path, today: str, priorities: dict[str, str]) -> int:
    """Same layout as the old brief, so the email and Magpie's watchlist parse it unchanged."""
    videos = []
    for f in sorted(folder.glob("*.md")):
        head, _, body = f.read_text(errors="ignore").partition("\n---\n")
        meta = {line.split("**:", 1)[0].strip("- *"): line.split("**:", 1)[1].strip()
                for line in head.splitlines() if line.startswith("- **") and "**:" in line}
        words = body.split()
        title = head.splitlines()[0].lstrip("# ").strip() if head else f.stem
        videos.append({"title": title, "channel": meta.get("Channel", ""), "url": meta.get("URL", ""),
                       "duration": meta.get("Duration", "unknown"), "words": meta.get("Words", ""),
                       "snippet": " ".join(words[:500]) + ("..." if len(words) > 500 else ""), "full": len(words)})
    videos.sort(key=lambda v: (PRIO_RANK.get(priorities.get(v["channel"], "medium"), 1), -v["full"]))
    dash = chr(0x2014)  # the old header's separator, kept byte-for-byte for anything that matches on it
    lines = [f"# YouTube Intel Brief {dash} {today}\n",
             f"**Videos analyzed**: {len(videos)} (ordered by impact: channel priority, then depth)",
             f"**Generated**: {dt.datetime.now(CENTRAL).strftime('%Y-%m-%d %H:%M CT')}\n", "---\n"]
    for i, v in enumerate(videos, 1):
        lines.append(f"## {i}. {v['title']}\n")
        lines.append(f"**{v['channel']}** | {v['duration']} | {v['words']} words" + (f" | [Watch]({v['url']})" if v["url"] else "") + "\n")
        lines.append(f"{v['snippet']}\n")
        if i < len(videos):
            lines.append("---\n")
    brief.write_text("\n".join(lines))
    return len(videos)


def run(hours: int | None = None, workers: int = 4) -> dict:
    yt = home()
    hours = hours or int(os.environ.get("MAGPIE_YT_LOOKBACK_HOURS", "36"))
    today = now_utc().astimezone(CENTRAL).date().isoformat()
    folder = yt / "transcripts" / today
    folder.mkdir(parents=True, exist_ok=True)
    (yt / "briefs").mkdir(exist_ok=True)
    index_file = yt / "index/processed.json"
    index = load_json(index_file, {"version": 1, "videos": {}})
    index.setdefault("videos", {})
    added = sync_roster(yt)
    channels = roster(yt)
    found, feed_errors = discover(channels, set(index["videos"]), hours)
    (folder / "discovered.json").write_text(json.dumps(found, indent=2))
    ok = shorts = failed = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for v, res in zip(found, ex.map(lambda v: extract(v, folder), found)):
            if res is None:
                failed += 1  # left out of the index, so the next run retries it
                continue
            if res.get("skip"):
                shorts += 1
                index["videos"][v["video_id"]] = {"title": v["title"], "channel": v["channel"],
                                                  "processed_at": now_utc().isoformat(), "skipped": res["skip"]}
                continue
            ok += 1
            index["videos"][v["video_id"]] = {"title": res["title"], "channel": v["channel"], "processed_at": now_utc().isoformat(),
                                              "transcript_path": f"transcripts/{today}/{v['video_id']}.md"}
    tmp = index_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(index_file)
    briefed = write_brief(folder, yt / "briefs" / f"{today}-brief.md", today, {c["name"]: c["priority"] for c in channels})
    receipt = {"date": today, "channels": len(channels), "roster_added": added, "lookback_hours": hours, "discovered": len(found),
               "transcribed": ok, "shorts_skipped": shorts, "failed": failed, "in_brief": briefed, "feed_errors": len(feed_errors)}
    print(json.dumps(receipt), file=sys.stderr)
    return receipt
