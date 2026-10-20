"""YouTube: Magpie's own channel list, read with yt-dlp (titles, dates and links only).

Channel RSS is unreliable (404s from home and cloud addresses alike), and the shared transcript
pipeline follows a different, business-heavy list. This collector lists the newest uploads of each
channel in config/youtube.json. It runs where yt-dlp is installed and quietly returns [] elsewhere.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import shutil
import subprocess

from ..items import Item
from ..util import ROOT, clean, load_json, now_utc


def _uploads(channel_id: str, n: int = 4, timeout: int = 60) -> list[dict]:
    exe = shutil.which("yt-dlp")
    argv = [exe, "--flat-playlist", "--playlist-end", str(n), "--extractor-args", "youtubetab:approximate_date",
            "--print", "%(id)s\t%(upload_date)s\t%(duration)s\t%(title)s", "--no-warnings", "--quiet",
            f"https://www.youtube.com/channel/{channel_id}/videos"]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4 and parts[0]:
            rows.append({"id": parts[0], "date": parts[1], "duration": parts[2], "title": parts[3]})
    return rows


def channels(hours: int = 40, workers: int = 6) -> list[Item]:
    if not shutil.which("yt-dlp"):
        return []
    roster = load_json(ROOT / "config/youtube.json", {}).get("channels", [])
    cutoff = (now_utc() - dt.timedelta(hours=hours)).date()
    out: list[Item] = []

    def one(ch: dict) -> list[Item]:
        got = []
        for v in _uploads(ch["channel_id"]):
            try:
                day = dt.datetime.strptime(v["date"], "%Y%m%d").date()
            except ValueError:
                continue
            if day < cutoff:
                continue
            got.append(Item(
                source_id="youtube-channels",
                source_kind="video",
                source_name=f"YouTube: {ch['name']}",
                url=f"https://www.youtube.com/watch?v={v['id']}",
                title=clean(v["title"])[:220],
                published_at=f"{day.isoformat()}T12:00:00Z",
                author=ch["name"],
                trust=0.7 if ch.get("priority") == "high" else 0.55,
                metrics={"duration_s": int(float(v["duration"])) if v["duration"] not in ("NA", "") else None},
                extra={"channel_class": ch.get("class"), "channel_priority": ch.get("priority")},
            ).finalize())
        return got

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(lambda c: _safe(one, c), roster):
            out += res
    return out


def _safe(fn, ch):
    try:
        return fn(ch)
    except Exception:
        return []
