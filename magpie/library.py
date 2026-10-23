"""The library: every capability Magpie has edited before a given day, with how far it has taken hold.

New possibilities combine today's developments with each other and with what already exists. This module supplies the
second pool: for each strong new arrival, the existing capabilities it relates to most, whatever their age and whether
they were ever featured. Retrieval is stdlib only (shared words weighted by rarity, shared topics, a named integration
either way, the same maker) so the cloud tier runs it as the home tier does. No model is called.

A library row carries the same fields as a row from today, from its latest edited appearance, plus its history before the
day: first and last seen, days seen, days featured, peak signal, stars gained, and works-with neighbours (what it was
combined with in a new possibility or covered with in the same post, as on its element page). All of it comes from the
editions before the day, so re-running a past day sees what that day saw.
`adoption` turns that history into one 0 to 1 score; the possibility ranking uses it as the amplification term.

A named integration is read from works_with lists. With MAGPIE_TEXT_NAMED=1 it is also read from what either side's text
states ("deploys to vLLM", "configures ... OpenCode"); see `Integrations`. Off by default.
"""
from __future__ import annotations

import math
import os
import re
import urllib.error
from collections import Counter, defaultdict
from pathlib import Path

from .util import fetch_json, github_headers, load_json

PER_ARRIVAL = 3      # existing capabilities kept for each strong new arrival
STRONG = 8           # today's best edited arrivals that look for partners
MAX_ROWS = 20        # library rows in one pool at most
MIN_SCORE = 0.12     # below this relatedness a pairing is noise
MAX_CHECKS = 40      # live checks per pool at most
TEXT_NAMED = "MAGPIE_TEXT_NAMED"  # "1" also reads named integrations from text (see Integrations)

STOP = set("""a an and are as at be been but by can could do does for from has have how in into is it its just more most
new no not now of on one or our out over so than that the their them then there these they this to up use used uses using
via was were what when which while who will with without would you your also any all about after before only other some
such each per across more less than very much many make makes made lets let model models tool tools ai open source""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.+-]*[a-z0-9]|[a-z]", (text or "").lower()) if len(w) > 2 and w not in STOP]


def _name(key: str) -> str:
    """The short name a key goes by: a repo or model name, the last part of a path."""
    rest = key.split(":", 1)[-1].rstrip("/")
    return rest.rsplit("/", 1)[-1].lower()


def _named(brief: dict) -> list[str]:
    return [str(w.get("name", "")).lower() for w in (brief.get("works_with") or []) if isinstance(w, dict) and w.get("name")]


def build(date: str, data_dir: Path) -> dict[str, dict]:
    """key -> library entry, from every edition before `date`. An entry is {"event", "date", "first_seen", "last_seen",
    "days_seen", "featured", "peak", "stars_gained", "works_with"}; "event" is its latest edited appearance, from "date"."""
    from .elements import _ties

    lib: dict[str, dict] = {}
    totals: dict[str, list[int]] = {}
    partners: dict[str, set[str]] = defaultdict(set)
    key_of: dict[str, str] = {}  # event id -> key; ids are a hash of the key, the same on every day
    for path in sorted(Path(data_dir, "days").glob("*.json")):
        if path.stem >= date:
            continue
        day = load_json(path, None)
        if not day:
            continue
        top = set(day.get("top") or [])
        for e in day.get("events") or []:
            k = e["key"]
            s = lib.setdefault(k, {"event": None, "date": None, "first_seen": day["date"], "last_seen": day["date"],
                                   "days_seen": 0, "featured": 0, "peak": 0.0, "stars_gained": 0, "works_with": 0})
            s["last_seen"] = day["date"]
            s["days_seen"] += 1
            s["featured"] += e["id"] in top
            s["peak"] = max(s["peak"], float(e.get("signal") or 0))
            m = e.get("metrics") or {}
            s["stars_gained"] = max([s["stars_gained"]] + [int(m.get(f) or 0) for f in ("stars_day", "stars_week", "stars_month")])
            if m.get("stars_total"):
                totals.setdefault(k, []).append(int(m["stars_total"]))
            if e.get("brief"):
                s["event"], s["date"] = e, day["date"]
            key_of[e["id"]] = k
        # Works with, as element pages count it: covered by the same post that day, or combined in a new possibility.
        for k, ties in _ties(day.get("events") or []).items():
            partners[k] |= {t["key"] for t in ties}
        for p in (day.get("possibilities") or {}).get("items") or []:
            keys = {key_of[i] for i in p.get("ingredients") or [] if i in key_of}
            for k in keys:
                partners[k] |= keys - {k}
    for k, t in totals.items():
        lib[k]["stars_gained"] = max(lib[k]["stars_gained"], max(t) - min(t))
    for k, s in lib.items():
        s["works_with"] = len(partners[k])
    return {k: s for k, s in lib.items() if s["event"]}


def adoption(s: dict | None) -> float:
    """How far a capability had taken hold before the day, 0 to 1: days seen, peak signal, stars gained and works-with
    neighbours, each capped and weighted equally. A capability first seen today scores 0."""
    if not s:
        return 0.0
    parts = [min(1.0, s["days_seen"] / 7), min(1.0, s["peak"] / 100), min(1.0, math.log10(1 + s["stars_gained"]) / 4),
             min(1.0, s["works_with"] / 6)]
    return round(sum(parts) / len(parts), 2)


def signals(s: dict) -> dict:
    return {k: s[k] for k in ("first_seen", "days_seen", "featured", "peak", "stars_gained", "works_with")}


def _doc(e: dict) -> list[str]:
    b = e.get("brief") or {}
    return _words(" ".join([b.get("headline") or e.get("title", ""), e.get("title", ""), b.get("newly_possible") or "",
                            b.get("why") or "", e.get("summary", "")[:400], _name(e["key"]), " ".join(_named(b))]))


def same(a: dict, b: dict) -> bool:
    """The same release under two keys (a model page and its launch post): the same short name, or headlines that share
    most of their words. A capability does not combine with itself."""
    if len(_name(a["key"])) > 3 and _name(a["key"]) == _name(b["key"]):
        return True
    wa, wb = (set(_words((e.get("brief") or {}).get("headline") or e.get("title", ""))) for e in (a, b))
    return bool(wa | wb) and len(wa & wb) / len(wa | wb) >= 0.5


# Stated integrations: a named integration read from text as well as works_with. Behind MAGPIE_TEXT_NAMED; tested blind
# against the works_with rule alone on 208 arrivals (Sep 26 to Oct 21, 2026): it changed 14, and where both judges agreed
# they preferred it 7 to 3.

CUES = re.compile(r"\b(?:deploy(?:s|ed|ing)?\s+(?:to|on|onto|into|with)|run(?:s|ning)?\s+(?:on|in|inside|under|with)"
                  r"|configur(?:e|es|ed|ing)|integrat(?:e|es|ed|ing|ion|ions)\s+(?:with|into|for)|works?\s+with"
                  r"|working\s+with|compatib(?:le|ility)\s+with|support(?:s|ed|ing)?(?:\s+for)?|plug(?:s|ged|ging)?\s+into"
                  r"|built\s+(?:on|with|for)|on\s+top\s+of|from\s+inside|via|connect(?:s|ed|ing)?\s+(?:to|with)"
                  r"|serv(?:e|es|ed|ing)\s+(?:with|via|through|on)|export(?:s|ed|ing)?\s+to)\b", re.I)
WINDOW = 16          # tokens after a cue, punctuation marks included, in which a product name counts as stated
NAME_MAX_DF = 0.15   # a product name in more of the library than this is too common to say anything
ARTIFACTS = ("repo", "model", "changelog", "blog", "paper")  # kinds one integrates with; posts and videos are about things
NOT_NAMES = set("""ai ml llm llms api apis gpu gpus cpu cpus rl ui ux cli sdk sdks os pdf pdfs html json yaml url urls us eu
uk kv rag moe oss ios gui ide ides qa ci cd sql csv faq tts asr ocr vlm vlms mllm sota bpw fp fp8 fp16 int4 int8 nvfp4 gb tb
mb ms new the this that these it its they we you our your a an and or but for with from into on in of to by at as is are was
open source model models tool tools agent agents video videos paper research official free local cloud community news
there what when where which who how why can could will would should may might also just today now here first last next
mon tue wed thu fri sat sun jan feb mar apr may jun jul aug sep oct nov dec ttft tpot tps
flash pro mini max ultra lite plus large small medium base instruct next preview beta alpha turbo spark""".split())
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*[A-Za-z0-9+]|[A-Za-z0-9]")
_WORD_OR_MARK = re.compile(_WORD.pattern + r"|[,;:()/&]")


def _tok(w: str) -> str:
    """Lowercase, a version suffix dropped (qwen3.8, gpt-6, deepseek-v4.1-flash) but not a digit inside a name
    (code2skill); a bare version is ""."""
    w = w.lower().strip(".-")
    return "" if re.match(r"v?\d", w) else (re.sub(r"[-.]?v?\d+(?:\.\d+)*(?:[-.][a-z0-9.+-]+)?$", "", w) or w)


def _shaped(w: str) -> bool:
    """A name on its own: inner capitals, a letter then a digit, or an acronym of three to six letters. A hyphen alone
    does not make one (open-sources, on-policy)."""
    return bool(re.search(r"[a-z][A-Z]|[A-Za-z]\d", w) or re.fullmatch(r"[A-Z]{3,6}", w))


def _phrases(name: str, repos: set[str]) -> set[tuple[str, ...]]:
    """A name as token tuples. A slug also counts by its first part when that part is itself a repo in the library, so
    sglang-omni is also SGLang."""
    toks = tuple(t for t in (_tok(w) for w in _WORD.findall(name)) if t)
    out = {toks} if toks else set()
    if len(toks) == 1 and "-" in toks[0] and toks[0].split("-")[0] in repos:
        out.add((toks[0].split("-")[0],))
    return {p for p in out if not (len(p) == 1 and (p[0] in NOT_NAMES or p[0] in STOP or len(p[0]) < 3))}


def _subject(headline: str, skip: set[str], proper: set[str]) -> str:
    """The product a headline is about: its opening name (a name-shaped word, a run of two or more capitalised words, or
    a known proper noun), or when it opens with the maker, the first name-shaped word after it."""
    words = _WORD.findall(headline or "")
    if not words:
        return ""
    w, t = words[0], _tok(words[0])
    if t in skip or t.split("-")[0] in skip:
        return next((x for x in words[1:] if _shaped(x) and _tok(x) not in skip), "")
    if _shaped(w):
        return w
    run = [w]
    for nxt in words[1:]:
        if nxt[0].isupper() and _tok(nxt) and _tok(nxt) not in NOT_NAMES:
            run.append(nxt)
        else:
            break
    if w[0].isupper() and t not in NOT_NAMES and (len(run) > 1 or t in proper):
        return " ".join(run[:3])
    return ""


def _makers(e: dict) -> set[str]:
    """The maker and the repo or model owner as names, so a headline that opens with them moves on to the product."""
    m = (e.get("maker") or "").strip()
    out = set()
    if m and m not in ("Open source", "Research", "Other"):
        out |= {_tok(w) for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", m.split("(")[0]) if len(w) > 1 and _tok(w) not in NOT_NAMES}
    kind, _, rest = e["key"].partition(":")
    if kind in ("gh", "hf", "hfs") and "/" in rest:
        owner = rest.split("/")[0].lower()
        o = _tok(re.sub(r"-(ai|ml|labs?|inc|org|io|dev|hq|team)$", "", owner))
        out |= {owner} | ({o} if len(o) > 2 and o not in NOT_NAMES else set())
    return out


def products(e: dict, proper: set[str], repos: set[str]) -> set[tuple[str, ...]]:
    """The names a capability goes by: its repo or model name, and for an artifact (not a post about something) the
    subject of its headline."""
    names = {_name(e["key"])} if e["key"].split(":", 1)[0] in ("gh", "hf", "hfs") else set()
    if e.get("kind") in ARTIFACTS:
        names.add(_subject((e.get("brief") or {}).get("headline") or "", _makers(e), proper))
    return {p for n in names if n for p in _phrases(n, repos)}


def _windows(e: dict) -> list[list[str]]:
    """Where a capability states an integration: each works_with name, and the words after each cue in its text."""
    b = e.get("brief") or {}
    ww = [w for w in (b.get("works_with") or []) if isinstance(w, dict)]
    text = " ".join([e.get("title", ""), b.get("headline") or "", b.get("newly_possible") or "", b.get("why") or "",
                     (e.get("summary") or "")[:600], " ".join(str(w.get("how", "")) for w in ww)])
    out = [_WORD.findall(str(w.get("name", ""))) for w in ww]
    for sent in _SENTENCE.split(text):
        out += [_WORD_OR_MARK.findall(sent[m.end():])[:WINDOW] for m in CUES.finditer(sent)]  # marks keep lists apart
    return out


def _states(windows: list[list[str]], phrases: set[tuple[str, ...]]) -> bool:
    """Whether a window holds one of the phrases whole and written as a name: not an all-lowercase plain word ("hindsight"
    is not Hindsight) and not continued by a capitalised word (Claude is not Claude Code)."""
    for win in windows:
        low = [_tok(w) for w in win]
        for p in phrases:
            for i in range(len(low) - len(p) + 1):
                if tuple(low[i:i + len(p)]) != p or all(w.isalpha() and w.islower() for w in win[i:i + len(p)]):
                    continue
                nxt = win[i + len(p)] if i + len(p) < len(win) else ""
                if not (nxt and nxt[0].isupper() and _tok(nxt) and _tok(nxt) not in NOT_NAMES):
                    return True
    return False


class Integrations:
    """Product names and stated-integration windows for one pool, built once. `stated(a, e)` is true when either side's
    windows hold the other's product name; a product name (the arrival's or a library capability's) found in more than
    NAME_MAX_DF of the library does not count."""

    def __init__(self, lib: dict[str, dict], keys: list[str], docs: dict[str, list[str]]):
        cap, low = Counter(), Counter()
        for k in keys:  # a single word is a name if the library writes it capitalised mid-sentence 4x as often as not
            b = lib[k]["event"].get("brief") or {}
            text = " ".join([b.get("newly_possible") or "", b.get("why") or "", (lib[k]["event"].get("summary") or "")[:600]])
            for sent in _SENTENCE.split(text):
                for i, w in enumerate(_WORD.findall(sent)):
                    if w.isalpha() and w[0].isupper() and i:
                        cap[w.lower()] += 1
                    elif w.isalpha() and w.islower():
                        low[w] += 1
        self.proper = {w for w, c in cap.items() if low[w] * 4 <= c}
        self.repos = {_tok(_name(k)) for k in keys if k.split(":", 1)[0] in ("gh", "hf", "hfs")}
        self._joined = [f" {' '.join(_tok(w) for w in _WORD.findall(' '.join(docs[k])))} " for k in keys]
        self._limit = NAME_MAX_DF * len(keys)
        self._common: dict[tuple[str, ...], bool] = {}
        self.products = {k: self._usable(products(lib[k]["event"], self.proper, self.repos)) for k in keys}
        self.windows = {k: _windows(lib[k]["event"]) for k in keys}
        self._arrivals: dict[str, tuple] = {}

    def _usable(self, phrases: set[tuple[str, ...]]) -> set[tuple[str, ...]]:
        """The phrases not too common in the library to say anything."""
        for p in phrases - self._common.keys():
            self._common[p] = sum(f" {' '.join(p)} " in j for j in self._joined) > self._limit
        return {p for p in phrases if not self._common[p]}

    def stated(self, arrival: dict, event: dict) -> bool:
        if arrival["key"] not in self._arrivals:
            self._arrivals[arrival["key"]] = (self._usable(products(arrival, self.proper, self.repos)), _windows(arrival))
        prod, win = self._arrivals[arrival["key"]]
        k = event["key"]
        return _states(self.windows[k], prod) or _states(win, self.products[k])


def related(arrival: dict, entry: dict, vec, integrations: Integrations | None = None) -> float:
    """How strongly an existing capability relates to a new arrival: shared rare words, shared topics, either naming the
    other as an integration (in works_with, or with `integrations` also in its text), the same maker."""
    a, b = vec(arrival), vec(entry["event"])
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    norm = math.sqrt(sum(w * w for w in a.values())) * math.sqrt(sum(w * w for w in b.values()))
    cos = dot / norm if norm else 0.0
    ta, tb = set(arrival.get("topics") or []), set(entry["event"].get("topics") or [])
    topics = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    na, nb = _name(arrival["key"]), _name(entry["event"]["key"])
    named = any(nb == n or (len(nb) > 3 and nb in n) for n in _named(arrival.get("brief") or {})) or \
        any(na == n or (len(na) > 3 and na in n) for n in _named(entry["event"].get("brief") or {})) or \
        bool(integrations and integrations.stated(arrival, entry["event"]))
    maker = bool(arrival.get("maker")) and arrival.get("maker") == entry["event"].get("maker") and \
        arrival.get("maker") not in ("Open source", "Research", "Other")
    return round(cos + 0.25 * topics + 0.5 * named + 0.1 * maker, 3)


def neighbours(arrivals: list[dict], lib: dict[str, dict], exclude: set[str],
               text_named: bool | None = None) -> dict[str, list[tuple[str, float]]]:
    """arrival id -> [(library key, relatedness)], best first: every candidate at or above MIN_SCORE, or when none is, every
    candidate that relates at all, so a lone arrival on a quiet day still has partners to try. Relatedness is weighted by
    the candidate's adoption, so of two equally related capabilities the one that has taken hold comes first.
    `text_named` (default: MAGPIE_TEXT_NAMED=1) also reads named integrations from text."""
    keys = [k for k in lib if k not in exclude]
    docs = {k: _doc(lib[k]["event"]) for k in keys}
    df = Counter(t for d in docs.values() for t in set(d))
    n = len(docs) + 1
    idf = {t: math.log(n / (1 + c)) + 1 for t, c in df.items()}
    cache: dict[str, dict[str, float]] = {}

    def vec(e: dict) -> dict[str, float]:
        if e["key"] not in cache:
            tf = Counter(docs.get(e["key"]) or _doc(e))
            cache[e["key"]] = {t: c * idf.get(t, math.log(n) + 1) for t, c in tf.items()}
        return cache[e["key"]]

    if text_named is None:
        text_named = os.environ.get(TEXT_NAMED) == "1"
    ints = Integrations(lib, keys, docs) if text_named else None
    out = {}
    for a in arrivals:
        scored = [(k, related(a, lib[k], vec, ints)) for k in keys if not same(a, lib[k]["event"])]
        scored = [(k, s) for k, s in scored if s >= MIN_SCORE] or [(k, s) for k, s in scored if s > 0]
        scored.sort(key=lambda ks: (-(ks[1] * (1 + adoption(lib[ks[0]]))), ks[0]))
        out[a["id"]] = scored
    return out


def live(key: str) -> bool | None:
    """Whether a repo, model or space still exists (and a repo is not archived): True, False, or None when the check
    could not run. Papers, posts and documents are not checked."""
    kind, _, rest = key.partition(":")
    if kind not in ("gh", "hf", "hfs"):
        return True
    url = {"gh": "https://api.github.com/repos/", "hf": "https://huggingface.co/api/models/",
           "hfs": "https://huggingface.co/api/spaces/"}[kind] + rest
    try:
        r = fetch_json(url, headers=github_headers() if kind == "gh" else None, retries=0, timeout=15)
    except Exception as ex:
        cause = ex.__cause__
        if isinstance(cause, urllib.error.HTTPError) and cause.code in (404, 410):
            return False
        return None
    return not r.get("archived") and not r.get("disabled")


def pick(arrivals: list[dict], lib: dict[str, dict], exclude: set[str], check=live, near: dict | None = None,
         per_arrival: dict[str, list[str]] | None = None) -> tuple[list[str], dict]:
    """The library keys for one pool, live ones only: up to PER_ARRIVAL for each arrival, best first, at most MAX_ROWS.
    Returns the keys and a receipt of what was checked. `near` replaces the scorer's ordering (the pair judge passes its
    own); `per_arrival`, when given, is filled with each arrival's keys."""
    near = neighbours(arrivals, lib, exclude) if near is None else near
    chosen: list[str] = []
    state: dict[str, bool | None] = {}
    for a in arrivals:
        got = 0
        for k, _ in near.get(a["id"], []):
            if got >= PER_ARRIVAL or len(chosen) >= MAX_ROWS:
                break
            if k in chosen:
                got += 1
                if per_arrival is not None:
                    per_arrival.setdefault(a["id"], []).append(k)
                continue
            if k not in state:
                if len(state) >= MAX_CHECKS:
                    break
                state[k] = check(k)
            if state[k]:
                chosen.append(k)
                got += 1
                if per_arrival is not None:
                    per_arrival.setdefault(a["id"], []).append(k)
    return chosen, {"checked": len(state), "not_live": sorted(k for k, v in state.items() if v is False),
                    "unchecked": sorted(k for k, v in state.items() if v is None)}
