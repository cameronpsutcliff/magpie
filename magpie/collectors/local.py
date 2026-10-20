"""Collectors that only work on the owner's always-on machine.

YouTube: reads the existing transcript pipeline's daily list (channel RSS is unreliable
from cloud and home IPs alike, and transcripts need a residential route).
X: asks the Grok CLI, which has sanctioned live X search on the owner's subscription.
Both return [] quietly when their tool or folder is absent, so the cloud run is unaffected.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..items import Item
from ..util import CENTRAL, ROOT, clean, now_utc, strip_html

YT_HOME = Path(os.environ["MAGPIE_YT_HOME"]) if os.environ.get("MAGPIE_YT_HOME") else None
ENTRY = re.compile(r"^## \d+\. (?P<title>.+?)\n\n\*\*(?P<channel>.+?)\*\* \| (?P<dur>[^|]+)\|[^|]*\| \[Watch\]\((?P<url>[^)]+)\)\n\nTranscript: (?P<text>.*?)(?:\n\n---|\Z)", re.S | re.M)


def youtube(days: int = 1) -> list[Item]:
    if YT_HOME is None or not (YT_HOME / "briefs").is_dir():
        return []
    briefs = YT_HOME / "briefs"
    if not briefs.is_dir():
        return []
    channels = {}
    cfg = YT_HOME / "config/channels.json"
    if cfg.exists():
        for c in json.loads(cfg.read_text()):
            channels[c["name"]] = c.get("priority", "medium")
    today = now_utc().astimezone(CENTRAL).date()
    out, seen = [], set()
    for back in range(days + 1):
        f = briefs / f"{(today - dt.timedelta(days=back)).isoformat()}-brief.md"
        if not f.exists():
            continue
        for m in ENTRY.finditer(f.read_text()):
            url = m.group("url").strip()
            if url in seen:
                continue
            seen.add(url)
            prio = channels.get(m.group("channel").strip(), "medium")
            out.append(Item(
                source_id="youtube-watchlist",
                source_kind="video",
                source_name=f"YouTube: {m.group('channel').strip()}",
                url=url,
                title=clean(m.group("title").strip()),
                summary="",
                author=m.group("channel").strip(),
                trust=0.7 if prio == "high" else 0.55,
                metrics={"duration": m.group("dur").strip()},
                # The transcript opening stays private: it feeds synthesis, never the page.
                extra={"private_context": strip_html(m.group("text"), 1400), "channel_priority": prio},
            ).finalize(m.group("text")))
    return out


X_PROMPT = """Use your live X search. Today is {today}. You are covering one community for a daily brief on what
became newly possible in AI: {group_label}. Find the most important NEW capabilities announced, released,
demonstrated or measured on X in the last 30 hours: new models, open weights, repos, agent and coding-tool
features, techniques, evals. Read these accounts first, and anyone they amplify: {handles}.

Skip opinion, fundraising, hiring, politics and memes. Merge duplicates. Up to 18 items.

Return ONLY one JSON object: {{"items": [{{"title": "short neutral headline", "url": "https://x.com/<handle>/status/<id>",
"handle": "@...", "summary": "one or two plain sentences on what is newly possible", "links": ["any repo, model,
paper or product URL in the post"], "vendor": "company or project", "posted_at": "ISO date if known"}}]}}.
No prose outside the JSON. Do not use the em-dash character."""

GROUPS = {
    "labs": "frontier and open-model labs and the people who announce their releases",
    "devtools": "builders of agent and coding tools",
    "opensource": "open-source and local-model developers",
    "research": "researchers and paper curators",
    "builders": "independent developers and educators who ship and test new things hands-on",
    "applied": "AI engineering practice, evals, infrastructure, security and enterprise deployment",
}


def _sweep(grok: str, group: str, handles: list[str], timeout: int) -> list[dict]:
    work = ROOT / "state/.grok" / group
    work.mkdir(parents=True, exist_ok=True)
    prompt = X_PROMPT.format(today=now_utc().date().isoformat(), group_label=GROUPS.get(group, group),
                             handles=", ".join(handles))
    (work / "prompt.md").write_text(prompt)
    r = subprocess.run([grok, "--prompt-file", "prompt.md", "--output-format", "plain", "--no-alt-screen",
                        "--always-approve", "--max-turns", "40", "--cwd", str(work)],
                       cwd=work, capture_output=True, text=True, timeout=timeout)
    start = r.stdout.find("{")
    if start < 0:
        raise RuntimeError(f"grok returned no JSON for {group} (rc {r.returncode})")
    data, _ = json.JSONDecoder().raw_decode(r.stdout[start:])
    return [dict(x, group=group) for x in data.get("items", [])]


def x_pulse(timeout: int = 1500) -> list[Item]:
    """Dedicated Grok sweeps, one per community on the roster, two at a time."""
    grok = shutil.which("grok") or str(Path.home() / ".local/bin/grok")
    if not Path(grok).exists() or os.environ.get("MAGPIE_SKIP_X"):
        return []
    roster = json.loads((ROOT / "config/x-accounts.json").read_text())
    by_group: dict[str, list[str]] = {}
    legacy = {1: "labs", 2: "devtools", 3: "opensource", 4: "research", 5: "builders", 6: "applied", 7: "builders"}
    for a in roster["accounts"]:
        if a.get("signal_to_noise", 3) >= 3:
            group = a.get("group") or legacy.get(a.get("category"), "builders")
            by_group.setdefault(group, []).append(a["handle"])
    wanted = [g for g in os.environ.get("MAGPIE_X_SWEEPS", ",".join(GROUPS)).split(",") if g in by_group]
    raw: list[dict] = []
    errors = []
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("MAGPIE_X_PARALLEL", "2"))) as pool:
        futs = {pool.submit(_sweep, grok, g, by_group[g][:45], timeout): g for g in wanted}
        for fu in cf.as_completed(futs):
            try:
                raw += fu.result()
            except Exception as ex:
                errors.append(f"{futs[fu]}: {str(ex)[:120]}")
    if not raw and errors:
        raise RuntimeError("; ".join(errors))
    out, seen = [], set()
    for x in raw:
        url = x.get("url") or ""
        if not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        links = " ".join(x.get("links") or [])
        it = Item(
            source_id="x-grok-pulse",
            source_kind="social",
            source_name=f"X: {x.get('handle', '')}".strip(),
            url=url,
            title=clean(x.get("title", ""))[:200],
            summary=clean(x.get("summary", ""))[:400],
            published_at=x.get("posted_at"),
            author=x.get("handle"),
            vendor_hint=x.get("vendor"),
            trust=0.65,
            extra={"links": x.get("links") or [], "x_group": x.get("group")},
        ).finalize(links)
        # A post about a repo or model should cluster with it.
        if it.mentions:
            it.keys = it.mentions[:1] + it.keys
            it.mentions = it.mentions[1:]
        out.append(it)
    return out
