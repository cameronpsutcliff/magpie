"""Pair judge: a panel of model families chooses which library capabilities each strong arrival combines with.

The library scorer (library.neighbours) finds what relates to an arrival by shared words, topics, makers and named
integrations. It cannot tell a partner that runs, trains or extends the arrival from one that only shares a platform
(an assistant that happens to run on Claude, for Claude's protein work) or from a rival doing the same job. Behind
MAGPIE_PAIR_JUDGE=1 this module asks a panel instead:

1. Recall: about RECALL candidates per strong arrival, from the scorer's best and the arrival's earlier ties (covered by
   the same post, or combined in a possibility, on an earlier day).
2. Judge: each lane (one model family each, run as a CLI on its subscription, in parallel; the panel waits no more
   than BUDGET seconds and each runner stops its own process then) picks
   up to PER_ARRIVAL partners best first, labels each relation (runs-on, trains, extends, complements) with a one-line
   reason, and rejects candidates that only share a maker or platform, duplicate the arrival or compete with it.
3. Aggregate: a partner at least MIN_FAMILIES families pick wins, ordered by families, then Borda points, then the
   scorer. Gaps are filled by what one family picked, then from the scorer, skipping what MIN_FAMILIES families
   rejected.

Fewer than MIN_FAMILIES readable answers (a lane absent, failing or past the budget; the cloud tier has only Claude)
falls back to the scorer unchanged. Answers are cached per date, lane, model and prompt. Kept pairs carry their relation
and reason to the possibilities editor, and the votes go in the pool receipt.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from . import library
from .util import ROOT, load_json, write_json

FLAG = "MAGPIE_PAIR_JUDGE"        # "1" turns the panel on
LANES_ENV = "MAGPIE_PAIR_LANES"   # comma-separated lanes; default DEFAULT_LANES
RECALL = 12          # candidates shown per arrival
FROM_SCORER = 8      # of which the scorer's best come first, then earlier ties, then more of the scorer
MIN_FAMILIES = 2     # families that must pick a partner (or reject a candidate) for it to count
BUDGET = 270         # seconds for the whole panel; a lane past it is cut
VERSION = 1          # bump when the prompt changes, so cached answers are not reused
RELATIONS = ("runs-on", "trains", "extends", "complements")
REJECTS = ("same-platform-only", "duplicate", "competes")
STATE = ROOT / "state" / "pair-judge"


def _claude(prompt: str, model: str, timeout: int) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise FileNotFoundError("claude")
    argv = [exe, "--print", "--model", model, "--output-format", "json", "--tools", "", "--strict-mcp-config",
            "--no-session-persistence"]
    if os.environ.get("MAGPIE_SAFE_MODE", "1") == "1":
        argv.insert(1, "--safe-mode")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(argv, input=prompt, cwd=tmp, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude rc {r.returncode}: {r.stderr[-300:]}")
    return json.loads(r.stdout).get("result", "")


def _codex(prompt: str, model: str, timeout: int) -> str:
    exe = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    if not Path(exe).exists():
        raise FileNotFoundError("codex")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "answer.md"
        r = subprocess.run([exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "-s", "read-only",
                            "-m", model, "-o", str(out), "-"], input=prompt, cwd=tmp, capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"codex rc {r.returncode}")
        return out.read_text()


def _agy(prompt: str, model: str, timeout: int) -> str:
    """agy prints its whole answer as one JSON object but can keep running long after, so the answer is read as soon as
    it is complete and the process is then stopped."""
    exe = shutil.which("agy")
    if not exe:
        raise FileNotFoundError("agy")
    with tempfile.TemporaryDirectory() as tmp:
        out_path, err_path = Path(tmp) / "out.json", Path(tmp) / "err.txt"
        with open(out_path, "w") as out, open(err_path, "w") as err:
            p = subprocess.Popen([exe, "-p", prompt, "--model", model, "--output-format", "json"], cwd=tmp, stdout=out,
                                 stderr=err, text=True)
            deadline = time.time() + timeout
            try:
                while True:
                    exited = p.poll() is not None
                    try:
                        answer = json.loads(out_path.read_text())
                    except json.JSONDecodeError:
                        answer = None
                    if isinstance(answer, dict):
                        if answer.get("status") != "SUCCESS":
                            raise RuntimeError(f"agy status {answer.get('status')}: {str(answer.get('error'))[:300]}")
                        return answer.get("response", "")
                    if exited:
                        raise RuntimeError(f"agy rc {p.returncode} with no answer: {err_path.read_text()[-300:]}")
                    if time.time() > deadline:
                        raise subprocess.TimeoutExpired(exe, timeout)
                    time.sleep(0.5)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(10)


# lane -> (family, default model, runner). One lane per family; a model can be overridden with MAGPIE_PAIR_MODEL_<LANE>.
PANEL = {"claude": ("anthropic", "fable", _claude),
         "codex": ("openai", "gpt-6-astra", _codex),
         "agy": ("google", "Gemini 4 Argon (High)", _agy)}
DEFAULT_LANES = ["claude", "codex", "agy"]
RUNNERS = {lane: run for lane, (_, _, run) in PANEL.items()}


def enabled() -> bool:
    return os.environ.get(FLAG) == "1"


def lanes() -> list[str]:
    named = [l.strip() for l in os.environ.get(LANES_ENV, "").split(",") if l.strip()]
    return [l for l in named if l in PANEL] or list(DEFAULT_LANES)


def _model(lane: str) -> str:
    return os.environ.get(f"MAGPIE_PAIR_MODEL_{lane.upper()}") or PANEL[lane][1]


def _ties_before(date: str, data_dir: Path, keys: set[str]) -> dict[str, list[str]]:
    """key -> the keys it was tied to in editions before `date`: covered by the same post that day, or combined in a
    new possibility (as library.build counts works-with), oldest first."""
    from .elements import _ties

    out: dict[str, list[str]] = defaultdict(list)
    for path in sorted(Path(data_dir, "days").glob("*.json")):
        if path.stem >= date:
            continue
        d = load_json(path, None)
        if not d:
            continue
        events = d.get("events") or []
        key_of = {e["id"]: e["key"] for e in events}
        tied = {k: [t["key"] for t in v] for k, v in _ties(events).items() if k in keys}
        for p in (d.get("possibilities") or {}).get("items") or []:
            ing = [key_of[i] for i in p.get("ingredients") or [] if i in key_of]
            for k in set(ing) & keys:
                tied.setdefault(k, []).extend(x for x in ing if x != k)
        for k, others in tied.items():
            out[k] += [o for o in others if o not in out[k]]
    return out


def recall(arrivals: list[dict], lib: dict[str, dict], exclude: set[str], date: str, data_dir: Path,
           near: dict[str, list[tuple[str, float]]]) -> dict[str, list[str]]:
    """arrival id -> up to RECALL library keys: the scorer's best FROM_SCORER, then the arrival's earlier ties, then more
    of the scorer's list. Never today's keys, never the same release under another key."""
    ties = _ties_before(date, data_dir, {a["key"] for a in arrivals})
    out = {}
    for a in arrivals:
        scored = [k for k, _ in near.get(a["id"], [])]
        tied = [k for k in ties.get(a["key"], []) if k in lib and k not in exclude and not library.same(a, lib[k]["event"])]
        keys = list(dict.fromkeys(scored[:FROM_SCORER] + tied + scored[FROM_SCORER:]))
        out[a["id"]] = keys[:RECALL]
    return out


def _card(e: dict, first_seen: str = "") -> str:
    b = e.get("brief") or {}
    clip = lambda s, n: re.sub(r"\s+", " ", (s or "").replace(chr(0x2014), ", ")).strip()[:n]  # noqa: E731
    s = f"{clip(b.get('headline') or e['title'], 140)} ({e.get('maker') or 'unknown maker'}"
    s += f"; first seen {first_seen})" if first_seen else ")"
    s += f": {clip(b.get('newly_possible') or e.get('summary'), 320)}"
    if b.get("why"):
        s += f" Why it matters: {clip(b['why'], 200)}"
    if b.get("limits"):
        s += f" Limits: {clip(b['limits'], 160)}"
    return s


PROMPT = """You are choosing partners for Magpie's New possibilities: what becomes newly possible when one of today's AI
capabilities is combined with a capability that already existed. Today is {date}.

Each arrival below comes with candidates from the library of earlier capabilities. For each arrival, pick up to {n}
candidates that would combine with it into something newly possible that neither does alone, best first. A good partner
runs the arrival or is what it runs on (runs-on), trains or adapts it (trains), adds a step it lacks (extends), or covers
a different step of the same job (complements).

Do not pick, and list under "reject" instead:
- a candidate that shares only a maker or a platform with the arrival (same-platform-only),
- the same capability again, or a near copy (duplicate),
- a rival that does the same job (competes).
Picking fewer than {n}, or none, is fine. Ground each reason in the text given and invent nothing. One short sentence
per reason. Never use the em-dash character.

Return only one JSON object, no prose:
{{"arrivals": [{{"id": "<arrival id>", "picks": [{{"id": "<candidate id>", "relation": "runs-on|trains|extends|complements", "reason": "<one sentence>"}}], "reject": [{{"id": "<candidate id>", "relation": "same-platform-only|duplicate|competes"}}]}}]}}

ARRIVALS AND CANDIDATES:
{body}
"""


def prompt(date: str, arrivals: list[dict], lib: dict[str, dict], cands: dict[str, list[str]]) -> str:
    parts = []
    for a in arrivals:
        lines = [f"### Arrival {a['id']}: {_card(a)}", "Candidates:"]
        lines += [f"- {lib[k]['event']['id']}: {_card(lib[k]['event'], lib[k]['first_seen'])}" for k in cands[a["id"]]]
        parts.append("\n".join(lines))
    return PROMPT.format(date=date, n=library.PER_ARRIVAL, body="\n\n".join(parts))


def parse(text: str, cands: dict[str, list[str]], lib: dict[str, dict]) -> dict[str, dict]:
    """arrival id -> {"picks": [(key, relation, reason)] best first, at most PER_ARRIVAL, "reject": {key: relation}}.
    Unknown arrivals or candidates, unknown relations, repeats and anything of the wrong shape are dropped. ValueError
    (and nothing else) when the answer holds no JSON object."""
    start = (text or "").find("{")
    if start < 0:
        raise ValueError("no JSON object in the answer")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    listed = lambda v: v if isinstance(v, list) else []  # noqa: E731
    out = {}
    for row in listed(data.get("arrivals")):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] not in cands:
            continue
        by_id = {lib[k]["event"]["id"]: k for k in cands[row["id"]]}
        picks, seen = [], set()
        for p in listed(row.get("picks")):
            k = by_id.get(p.get("id")) if isinstance(p, dict) and isinstance(p.get("id"), str) else None
            if k and k not in seen and p.get("relation") in RELATIONS and len(picks) < library.PER_ARRIVAL:
                seen.add(k)
                picks.append((k, p["relation"], re.sub(r"\s+", " ", str(p.get("reason") or "")).strip()[:240]))
        reject = {}
        for r in listed(row.get("reject")):
            k = by_id.get(r.get("id")) if isinstance(r, dict) and isinstance(r.get("id"), str) else None
            if k and k not in seen and r.get("relation") in REJECTS:
                reject[k] = r["relation"]
        out[row["id"]] = {"picks": picks, "reject": reject}
    return out


def _cached(path: Path) -> dict[str, dict[str, str]]:
    """The cache file as lane -> {"model:digest": answer}; anything of another shape is ignored."""
    raw = load_json(path, {})
    if not isinstance(raw, dict):
        return {}
    return {lane: {k: v for k, v in entries.items() if isinstance(v, str)} for lane, entries in raw.items()
            if isinstance(lane, str) and isinstance(entries, dict)}


def _remember(path: Path, new: dict[str, dict[str, str]]) -> None:
    """Add answers to the cache file under a lock, merging with what another run may have written meanwhile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = _cached(path)
        for lane, entries in new.items():
            cache.setdefault(lane, {}).update(entries)
        write_json(path, cache)


def _ask(text: str, date: str, names: list[str], runners: dict, budget: int, cache_dir: Path) -> dict[str, dict]:
    """lane -> {"status": ok|cached|timeout|error|absent, "seconds", "model", "text"}. Lanes run in parallel and the panel
    waits for them no longer than `budget`; each runner also stops its own process at `budget` (a subprocess timeout,
    or agy's kill), so a lane cut here ends soon after. Answers are cached by date, lane, model and the prompt itself.
    A failure is recorded by its type only, since the receipt is published; the detail goes to the local log."""
    path = Path(cache_dir) / f"{date}.json"
    cache = _cached(path)
    digest = hashlib.sha256(f"{VERSION}\n{text}".encode()).hexdigest()[:16]
    out, todo = {}, []
    for lane in names:
        model = _model(lane)
        hit = (cache.get(lane) or {}).get(f"{model}:{digest}")
        if hit:
            out[lane] = {"status": "cached", "seconds": 0.0, "model": model, "text": hit}
        elif lane not in runners:
            out[lane] = {"status": "absent", "seconds": 0.0, "model": model, "text": ""}
        else:
            todo.append((lane, model))
    if todo:
        pool = ThreadPoolExecutor(max_workers=len(todo))
        t0 = time.time()

        def call(lane, model):
            t = time.time()
            answer = runners[lane](text, model, budget)
            return answer, round(time.time() - t, 1)
        futures = {pool.submit(call, lane, model): (lane, model) for lane, model in todo}
        done, _ = wait(futures, timeout=budget)
        new: dict[str, dict[str, str]] = {}
        for f, (lane, model) in futures.items():
            if f not in done:
                out[lane] = {"status": "timeout", "seconds": round(time.time() - t0, 1), "model": model, "text": ""}
            elif f.exception() is not None:
                ex = f.exception()
                print(f"pair judge: {lane} failed: {str(ex)[:300]}", file=sys.stderr)
                out[lane] = {"status": "error", "seconds": round(time.time() - t0, 1), "model": model, "text": "",
                             "error": type(ex).__name__}
            else:
                answer, seconds = f.result()
                out[lane] = {"status": "ok", "seconds": seconds, "model": model, "text": answer}
                new[lane] = {f"{model}:{digest}": answer}
        pool.shutdown(wait=False, cancel_futures=True)
        if new:
            _remember(path, new)
    return out


def aggregate(arrivals: list[dict], cands: dict[str, list[str]], near: dict, answers: dict[str, dict]) -> tuple[dict, dict]:
    """(arrival id -> ordered library keys for library.pick, arrival id -> what the panel decided). `answers` maps a lane
    to its parsed answer. Winners first, then single-family picks, then the scorer's order, without what MIN_FAMILIES
    families rejected."""
    order, decided = {}, {}
    for a in arrivals:
        aid = a["id"]
        votes: dict[str, dict[str, int]] = defaultdict(dict)
        relations: dict[str, list[str]] = defaultdict(list)
        reasons: dict[str, tuple[int, str]] = {}
        rejects: dict[str, set[str]] = defaultdict(set)
        for lane, parsed in answers.items():
            row = parsed.get(aid) or {"picks": [], "reject": {}}
            for rank, (k, relation, reason) in enumerate(row["picks"]):
                votes[k][lane] = rank + 1
                relations[k].append(relation)
                if reason and (k not in reasons or rank < reasons[k][0]):
                    reasons[k] = (rank, reason)
            for k in row["reject"]:
                rejects[k].add(PANEL[lane][0])
        scorer = [k for k, _ in near.get(aid, [])]
        place = {k: i for i, k in enumerate(scorer)}
        families = {k: {PANEL[l][0] for l in v} for k, v in votes.items()}
        borda = {k: sum(library.PER_ARRIVAL + 1 - r for r in v.values()) for k, v in votes.items()}
        winners = sorted((k for k in votes if len(families[k]) >= MIN_FAMILIES),
                         key=lambda k: (-len(families[k]), -borda[k], place.get(k, len(place)), k))
        rejected = {k for k, f in rejects.items() if len(f) >= MIN_FAMILIES and k not in winners}
        # A gap is filled first by what one family picked (best Borda first), then by the scorer's order.
        singles = sorted((k for k in votes if k not in winners and k not in rejected),
                         key=lambda k: (-borda[k], place.get(k, len(place)), k))
        order[aid] = winners + singles + [k for k in scorer if k not in winners and k not in singles and k not in rejected]
        decided[aid] = {"candidates": len(cands.get(aid, [])),
                        "kept": [{"key": k, "votes": votes[k],
                                  "relation": max(relations[k], key=lambda r: (relations[k].count(r), -relations[k].index(r))),
                                  "reason": reasons.get(k, (0, ""))[1]} for k in winners],
                        "single": [{"key": k, "votes": votes[k]} for k in singles],
                        "rejected": sorted(rejected)}
    return order, decided


def pick(day: dict, arrivals: list[dict], lib: dict[str, dict], exclude: set[str], check=library.live, data_dir=None,
         cache_dir: Path | None = None, runners: dict | None = None, budget: int = BUDGET) -> tuple[list[str], dict, dict]:
    """The library keys for one pool, as library.pick returns them, the receipt with the panel's votes under
    "pair_judge", and key -> [{"with": arrival id, "relation", "reason"}] for the editor. Falls back to the scorer."""
    near = library.neighbours(arrivals, lib, exclude)
    cands = recall(arrivals, lib, exclude, day["date"], Path(data_dir), near)
    text = prompt(day["date"], arrivals, lib, cands)
    names = lanes()
    asked = _ask(text, day["date"], names, RUNNERS if runners is None else runners, budget, STATE if cache_dir is None else cache_dir)
    parsed = {}
    for lane, got in asked.items():
        if got["status"] in ("ok", "cached"):
            try:
                parsed[lane] = parse(got["text"], cands, lib)
            except ValueError:
                got["status"] = "unreadable"
    lane_receipt = {l: {k: v for k, v in g.items() if k != "text"} for l, g in asked.items()}
    families = {PANEL[l][0] for l in parsed}
    if len(families) < MIN_FAMILIES:
        keys, receipt = library.pick(arrivals, lib, exclude, check, near=near)
        return keys, {**receipt, "pair_judge": {"lanes": lane_receipt, "fallback": f"fewer than {MIN_FAMILIES} families answered"}}, {}
    order, decided = aggregate(arrivals, cands, near, parsed)
    chosen: dict[str, list[str]] = {}
    keys, receipt = library.pick(arrivals, lib, exclude, check, near={aid: [(k, 0.0) for k in ks] for aid, ks in order.items()},
                                 per_arrival=chosen)
    notes: dict[str, list[dict]] = defaultdict(list)
    for a in arrivals:
        decided[a["id"]]["chosen"] = chosen.get(a["id"], [])
        for k in decided[a["id"]]["kept"]:
            if k["key"] in chosen.get(a["id"], []):
                notes[k["key"]].append({"with": a["id"], "relation": k["relation"], "reason": k["reason"]})
    return keys, {**receipt, "pair_judge": {"lanes": lane_receipt, "fallback": None, "arrivals": decided}}, dict(notes)
