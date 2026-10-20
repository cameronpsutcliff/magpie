"""Element pages: one file per thing Magpie keeps meeting (a repo, model, paper, tool or official action).

An edition is one day. An element is one thing across days: what it is, where to find it, what it works
with and connects to, and every edition it appeared in. Everything here is computed from published
editions and the constraints register; no model is called.

A page exists for an element seen on two or more days, one that fed a new possibility, an official action,
one that carried an action's document or reported it, or one whose maker an action already on record names.
Everything else stays in the editions.

Bounded: a page keeps its newest 60 appearances in full (words, sources, ties) and every appearance as one
short timeline row (date, signal, place, impact), so counts and peaks cover its whole history.

The update is incremental: a day's contributions replace that day's entries in each element file, so
publishing a day twice never counts it twice. Pages start on a date (`since` in the index): the first daily
update starts them that day, or a rebuild seeds them from the last seven editions. Nothing before `since` is read
for appearances. `rebuild` regenerates every page from `since`; the tests check that it agrees with a run of daily
updates from that date.

    data/v1/elements/<slug>.json   one element (magpie.element/1)
    data/v1/elements/index.json    every element page (magpie.elements/1)
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .util import load_json, short_hash, write_json

SCHEMA = "magpie.element/1"
INDEX_SCHEMA = "magpie.elements/1"
GENERIC_MAKERS = {"Open source", "Research", "Other"}
FULL_APPEARANCES = 60   # the newest appearances kept in full
TIMELINE_ROWS = 730     # two years of short rows; older ones fold into one summary, so a page is bounded
SEED_EDITIONS = 7       # a first rebuild reaches back this many editions at most; after that, pages grow day by day
POSSIBILITIES_KEPT = 60 # the newest possibilities kept in full; older ones fold into a count
CROWD = 6               # a post that ties more than this many items in a day is a roundup, not a connection
PER_APPEARANCE = 8      # sources, mentions and ties kept per appearance
METRICS = ("stars_total", "stars_day", "stars_week", "stars_month", "forks", "hn_points", "hn_comments", "hf_likes",
           "hf_downloads", "hf_upvotes")
ACTION_FIELDS = ("status", "instrument", "layers", "jurisdiction", "issuer", "effective", "comment_deadline", "access_now",
                 "doc_url", "who", "quote", "harder")
LINK_LABEL = {"gh": "GitHub", "hf": "Hugging Face", "hfs": "Hugging Face Space", "arxiv": "arXiv", "policy": "The document"}


# ---------- names ----------

_SIMPLE = re.compile(r"^[a-z0-9._-]+$")


def slug_for(key: str) -> str:
    """The page's slug, a pure function of the key, so a URL never depends on which element came first.

    Code and weights read as owner--name (gh-deepseek-ai--deepinfer; neither GitHub nor Hugging Face allows
    "--" in an owner, so the split is unambiguous). Papers and Federal Register documents keep their ids.
    Anything else gets a readable stem and a hash of the key.
    """
    prefix, _, rest = key.partition(":")
    if prefix in ("gh", "hf", "hfs"):
        owner, _, name = rest.partition("/")
        if owner and name and "--" not in owner and _SIMPLE.match(owner) and _SIMPLE.match(name) and len(rest) <= 160:
            return f"{prefix}-{owner}--{name}"
    elif prefix == "arxiv" and re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", rest):
        return f"arxiv-{rest}"
    elif prefix == "policy" and re.fullmatch(r"fr:[0-9]+(-[0-9]+)*", rest):
        return f"policy-fr-{rest[3:]}"
    body = re.sub(r"[^a-z0-9]+", "-", rest.lower()).strip("-")[:48].rstrip("-")
    return f"{prefix}-{body}-{short_hash(key, 8)}" if body else f"{prefix}-{short_hash(key, 8)}"


def canonical_link(key: str, url: str | None) -> dict:
    prefix, _, rest = key.partition(":")
    if prefix == "gh":
        return {"url": f"https://github.com/{_case(rest, url, 'github.com/')}", "label": "GitHub"}
    if prefix == "hf":
        return {"url": f"https://huggingface.co/{_case(rest, url, 'huggingface.co/')}", "label": "Hugging Face"}
    if prefix == "hfs":
        return {"url": f"https://huggingface.co/spaces/{_case(rest, url, 'huggingface.co/spaces/')}", "label": "Hugging Face Space"}
    if prefix == "arxiv":
        return {"url": f"https://arxiv.org/abs/{rest}", "label": "arXiv"}
    host = re.sub(r"^www\.", "", (url or "").split("//", 1)[-1].split("/", 1)[0]) or rest.split("/", 1)[0]
    return {"url": url or f"https://{rest}", "label": LINK_LABEL.get(prefix) or host}


def _case(rest: str, url: str | None, marker: str) -> str:
    """Keep the owner's capitalization from the source URL when it names the same thing (keys are lowercase)."""
    if url and marker in url:
        tail = url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0]
        parts = tail.split("/")[:2]
        if "/".join(parts).lower() == rest:
            return "/".join(parts)
    return rest


# ---------- what one edition says about each element ----------

def _src(s: dict) -> dict:
    out = {"name": s.get("name") or "", "url": s["url"], "title": s.get("title") or ""}
    if s.get("kind"):
        out["kind"] = s["kind"]
    if s.get("discussion"):
        out["discussion"] = s["discussion"]
    return out


def _ties(events: list[dict]) -> dict[str, list[dict]]:
    """Items tied by a post, video or thread that covered both on the same day (and no more than CROWD)."""
    by_url: dict[str, dict[str, dict]] = defaultdict(dict)
    for e in events:
        for s in e.get("sources", []) + e.get("mentions", []):
            by_url[s["url"]].setdefault(e["key"], s)
    ties: dict[str, dict[str, dict]] = defaultdict(dict)
    titles = {e["key"]: (e.get("brief") or {}).get("headline") or e["title"] for e in events}
    for url, keys in by_url.items():
        if not 2 <= len(keys) <= CROWD:
            continue
        for k in keys:
            for other, s in keys.items():
                if other != k and other not in ties[k]:
                    ties[k][other] = {"key": other, "title": titles[other], "via": s.get("name") or "", "item": s.get("title") or "", "url": url}
    return {k: sorted(v.values(), key=lambda t: t["key"])[:PER_APPEARANCE] for k, v in ties.items()}


def appearance(e: dict, day: dict, ties: list[dict], read: dict[str, str]) -> dict:
    b = e.get("brief") or {}
    top = day.get("top") or []
    a: dict = {"date": day["date"], "id": e["id"], "kind": e["kind"], "title": e["title"]}
    if e.get("maker"):
        a["maker"] = e["maker"]
    if b.get("headline"):
        a["headline"] = b["headline"]
    if e["kind"] == "constraint":
        if b.get("harder") or e.get("summary"):
            a["newly_constrained"] = b.get("harder") or e["summary"]
        c = e.get("constraint") or {}
        for k in ("status", "jurisdiction", "instrument"):
            if c.get(k):
                a[k] = c[k]
    elif b.get("newly_possible"):
        a["newly_possible"] = b["newly_possible"]
    elif e.get("summary"):
        a["summary"] = e["summary"][:400]
    for k in ("why", "limits"):
        if b.get(k):
            a[k] = b[k]
    a["signal"] = e["signal"]
    if b.get("impact"):
        a["impact"] = b["impact"]   # only the editor's rating; code's estimate stays in the edition
    a["evidence"] = e["evidence"]
    if e["id"] in top:
        a["rank"] = top.index(e["id"]) + 1
    if e.get("topics"):
        a["topics"] = list(e["topics"])
    ripples = [{"topic": r["topic"], **({"effect": r["effect"]} if r.get("effect") else {})}
               for r in b.get("ripples") or [] if isinstance(r, dict) and r.get("topic")]
    if ripples:
        a["ripples"] = ripples
    m = {k: e["metrics"][k] for k in METRICS if (e.get("metrics") or {}).get(k) is not None}
    if m:
        a["metrics"] = m
    if (e.get("doubt") or {}).get("note"):
        a["doubt"] = e["doubt"]["note"]
    if b.get("hype"):
        a["hype"] = True
    if e["id"] in read:
        a["read"] = read[e["id"]]
    a["sources"] = [_src(s) for s in e.get("sources", [])[:PER_APPEARANCE]]
    if e.get("mentions"):
        a["mentions"] = [_src(s) for s in e["mentions"][:PER_APPEARANCE]]
    if ties:
        a["with"] = ties
    return a


def contributions(day: dict, load_day) -> dict[str, dict]:
    """key -> {"appearance": ... or None, "possibilities": [...]} for one published edition.

    `load_day(date)` returns another edition (for possibility ingredients from the past week) or None.
    """
    events = day.get("events") or []
    out: dict[str, dict] = {}
    ties = _ties(events)
    read = {}
    for p in (day.get("read") or {}).get("items") or []:
        for i in p.get("ids") or []:
            read.setdefault(i, p.get("lead") or "")
    for e in events:
        out[e["key"]] = {"appearance": appearance(e, day, ties.get(e["key"], []), read), "possibilities": []}

    by_date = {day["date"]: {e["id"]: e for e in events}}
    for rank, p in enumerate((day.get("possibilities") or {}).get("items") or [], 1):
        refs = {r["id"]: r for r in p.get("ingredient_refs") or [] if r.get("id")}
        found = []
        for i in p.get("ingredients") or []:
            d = (refs.get(i) or {}).get("date") or day["date"]
            if d not in by_date:
                other = load_day(d)
                by_date[d] = {e["id"]: e for e in (other or {}).get("events", [])}
            e = by_date[d].get(i)
            if e:
                found.append((e["key"], (refs.get(i) or {}).get("title") or (e.get("brief") or {}).get("headline") or e["title"]))
        for key, _ in found:
            rec = {"date": day["date"], "rank": rank, "title": p.get("title") or "",
                   "with": [{"key": k, "title": t} for k, t in found if k != key]}
            for k in ("connection", "effort"):
                if p.get(k):
                    rec[k] = p[k]
            out.setdefault(key, {"appearance": None, "possibilities": []})["possibilities"].append(rec)
    return out


# ---------- one element from all its contributions ----------

def _names(text: str, maker: str | None) -> bool:
    if not maker or maker in GENERIC_MAKERS or len(maker) < 3:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(maker.lower())}(?![a-z0-9])", text.lower()) is not None


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").split("#", 1)[0]).rstrip("/").lower()


def _action_text(c: dict) -> str:
    return " ".join(str(c.get(k) or "") for k in ("headline", "title", "harder", "who", "quote"))


def url_matches(key: str, urls: set[str], register: list[dict]) -> dict[str, str]:
    """Actions whose document this element links to ("document"), or that cite it as a source ("reported")."""
    urls = {_norm_url(u) for u in urls if u}
    out: dict[str, str] = {}
    for c in register:
        if c.get("key") == key:
            continue
        docs = {_norm_url(c.get("doc_url") or ""), _norm_url(c.get("url") or "")} - {""}
        reports = {_norm_url(s.get("url", "")) for s in c.get("sources") or []} - {""} - docs
        if urls & docs:
            out[c["key"]] = "document"
        elif urls & reports:
            out[c["key"]] = "reported"
    return out


def _merge_matches(a: dict[str, str], b: dict[str, str]) -> dict[str, str]:
    out = dict(a)
    for k, m in b.items():
        if out.get(k) != "document":
            out[k] = m
    return dict(sorted(out.items()))


def constraint_links(key: str, maker: str | None, urls: set[str], register: list[dict], carried: dict[str, str] | None = None) -> list[dict]:
    """Official actions that rest on a document this element links to, cite it, or name its maker. `carried` holds the
    URL matches of appearances that already left the full window (their URLs are no longer on the page)."""
    matched = _merge_matches(carried or {}, url_matches(key, urls, register))
    out = []
    for c in register:
        if c.get("key") == key:
            continue
        why = matched.get(c["key"])
        if not why and _names(_action_text(c), maker):
            why = "maker"
        if why:
            out.append({"key": c["key"], "id": c.get("id"), "headline": c.get("headline") or c.get("title") or "",
                        "status": c.get("status"), "jurisdiction": c.get("jurisdiction"), "issuer": c.get("issuer"),
                        "url": c.get("doc_url") or c.get("url"), "first_seen": c.get("first_seen"),
                        "why": {"document": "It rests on a document this element links to.",
                                "reported": "This element is one of the sources that reported it."}.get(why, f"It names {maker}."),
                        "match": why})
    out.sort(key=lambda c: (["document", "reported", "maker"].index(c["match"]), c["key"]))
    return out


def qualifies(contribs: dict[str, dict], key: str, register: list[dict], older: dict | None = None) -> bool:
    """Seen on two or more days, fed a possibility, is an official action, or an action rests on a document it
    linked to, cites it, or names its maker."""
    seen = sorted((c["appearance"] for c in contribs.values() if c.get("appearance")), key=lambda a: a["date"], reverse=True)
    if not seen:
        return False
    o = older or {}
    if len(seen) + o.get("days", 0) >= 2 or o.get("possibilities") or any(c.get("possibilities") for c in contribs.values()):
        return True
    if seen[0].get("kind") == "constraint" or key.startswith("policy:"):
        return True
    maker = next((a["maker"] for a in seen if a.get("maker")), None)
    return bool(constraint_links(key, maker, _urls(seen), register, o.get("links")))


def _urls(apps: list[dict]) -> set[str]:
    return {u for a in apps for s in a.get("sources", []) + a.get("mentions", []) for u in (s["url"], s.get("discussion")) if u}


def assemble(key: str, slug: str, contribs: dict[str, dict], register: list[dict], topic_labels: dict[str, str],
             names: list[dict] | None = None, older: dict | None = None) -> dict:
    """The element page from every edition's contribution (date -> {"appearance", "possibilities"}), plus the
    summary of rows that already left the timeline window (`older`, from the existing page)."""
    every = sorted((c["appearance"] for c in contribs.values() if c.get("appearance")), key=lambda a: a["date"], reverse=True)
    rows = [row(a) for a in every]
    timeline, older = rows[:TIMELINE_ROWS], fold(rows[TIMELINE_ROWS:], older)
    # Words, sources and ties come from the newest appearances kept in full; counts and peaks from every one.
    full = [a for a in every if not a.get("row")]
    keep = min(FULL_APPEARANCES, TIMELINE_ROWS)  # a full appearance is always also a timeline row
    apps = full[:keep]
    # An appearance leaving the full window takes its URLs with it, so keep what they matched in the register.
    carried = _merge_matches((older or {}).get("links") or {}, url_matches(key, _urls(full[keep:]), register))
    every_poss = sorted((p for c in contribs.values() for p in c.get("possibilities") or []), key=lambda p: (p["date"], -p["rank"]), reverse=True)
    poss = every_poss[:POSSIBILITIES_KEPT]
    if carried or len(every_poss) > POSSIBILITIES_KEPT:
        older = dict(older or {})
        if carried:
            older["links"] = carried
        if len(every_poss) > POSSIBILITIES_KEPT:
            older["possibilities"] = older.get("possibilities", 0) + len(every_poss) - POSSIBILITIES_KEPT
    latest = apps[0]
    first_seen, last_seen = (older or {}).get("first_seen") or timeline[-1]["date"], timeline[0]["date"]
    headline = next((a["headline"] for a in apps if a.get("headline")), None)
    lede = next((a.get("newly_possible") or a.get("newly_constrained") for a in apps if a.get("newly_possible") or a.get("newly_constrained")), None)
    summary = next((a["summary"] for a in apps if a.get("summary")), None)
    maker = next((a["maker"] for a in apps if a.get("maker")), None)
    topics = [t for t, _ in Counter(t for a in apps for t in a.get("topics", [])).most_common(4)]
    if key.startswith(("gh:", "hf:", "hfs:")):
        link = canonical_link(key, next((s["url"] for a in apps for s in a.get("sources", [])
                                         if _norm_url(s["url"]).startswith(("github.com", "huggingface.co"))), None))
    else:
        link = _own_url(key, apps, register)

    # Where to find it: the canonical link, then every source, discussion and mention, most frequent first.
    find: dict[str, dict] = {}

    def add(url: str, name: str, title: str, role: str, date: str, kind: str | None = None) -> None:
        if not url or _norm_url(url) == _norm_url(link["url"]):
            return
        f = find.setdefault(_norm_url(url), {"url": url, "name": name, "title": title, "role": role, "first_seen": date, "last_seen": date, "days": 0})
        if kind and "kind" not in f:
            f["kind"] = kind
        f["first_seen"], f["last_seen"] = min(f["first_seen"], date), max(f["last_seen"], date)
        f["days"] += 1

    for a in reversed(apps):
        for s in a.get("sources", []):
            add(s["url"], s["name"], s["title"], "source", a["date"], s.get("kind"))
            add(s.get("discussion", ""), s["name"], f"Discussion: {s['title']}", "discussion", a["date"], "community")
        for s in a.get("mentions", []):
            add(s["url"], s["name"], s["title"], "mention", a["date"], s.get("kind"))
    # Who spotted it: every source and mention by name, including those that only point at the canonical link.
    spotted: dict[str, dict] = {}
    for a in reversed(apps):
        by_name = {s["name"]: "source" for s in a.get("sources", []) if s.get("name")}
        for s in a.get("mentions", []):
            if s.get("name"):
                by_name.setdefault(s["name"], "mention")
        for name, role in by_name.items():
            b = spotted.setdefault(name, {"name": name, "role": role, "days": 0, "first_seen": a["date"], "last_seen": a["date"]})
            b["days"] += 1
            b["last_seen"] = a["date"]
            if role == "source":
                b["role"] = "source"
    spotted_by = sorted(sorted(spotted.values(), key=lambda b: b["name"]), key=lambda b: (b["role"] != "source", -b["days"]))[:30]
    order = {"source": 0, "discussion": 1, "mention": 2}
    where = sorted(find.values(), key=lambda f: (f["last_seen"], f["url"]), reverse=True)
    where = sorted(where, key=lambda f: (order[f["role"]], -f["days"]))[:40]

    # Knock-on areas: the editor's ripples, grouped by area.
    knock: dict[str, dict] = {}
    for a in apps:
        for r in a.get("ripples", []):
            k = knock.setdefault(r["topic"], {"topic": r["topic"], "label": topic_labels.get(r["topic"], r["topic"]), "days": 0, "effects": []})
            k["days"] += 1
            if r.get("effect") and all(x["effect"] != r["effect"] for x in k["effects"]):
                k["effects"].append({"effect": r["effect"], "date": a["date"]})
    knock_on = sorted(knock.values(), key=lambda k: (-k["days"], k["topic"]))
    for k in knock_on:
        k["effects"] = k["effects"][:4]

    # Works with: combined with it in a new possibility, or covered with it in the same post on the same day.
    partners: dict[str, dict] = {}
    for p in poss:
        for w in p["with"]:
            q = partners.setdefault(w["key"], {"key": w["key"], "title": w["title"], "possibilities": 0, "together": 0, "last": p["date"], "via": []})
            q["possibilities"] += 1
            q["last"] = max(q["last"], p["date"])
            if len(q["via"]) < 3 and p["title"] not in q["via"]:
                q["via"].append(p["title"])
    for a in apps:
        for w in a.get("with", []):
            q = partners.setdefault(w["key"], {"key": w["key"], "title": w["title"], "possibilities": 0, "together": 0, "last": a["date"], "via": []})
            q["together"] += 1
            q["last"] = max(q["last"], a["date"])
            via = f"{w['via']}: {w['item']}" if w.get("item") else w["via"]
            if len(q["via"]) < 3 and via and via not in q["via"]:
                q["via"].append(via)
    works_with = sorted(partners.values(), key=lambda q: (q["possibilities"], q["together"], q["last"], q["key"]), reverse=True)[:24]

    urls = {link["url"]} | {f["url"] for f in where}
    constraints = constraint_links(key, maker, urls, register, carried)
    o = older or {}
    ranks = [a["rank"] for a in timeline if a.get("rank")] + ([o["best_rank"]] if o.get("best_rank") else [])
    impacts = [a["impact"] for a in timeline if a.get("impact")] + ([o["best_impact"]] if o.get("best_impact") else [])
    peak = max(timeline + ([o["peak"]] if o.get("peak") else []), key=lambda a: (a["signal"], a["date"]))
    page = {
        "schema": SCHEMA,
        "slug": slug,
        "key": key,
        "kind": latest["kind"],
        "name": display_name(key, link, apps),
        "title": latest["title"],
        **({"headline": headline} if headline else {}),
        **({"lede": lede} if lede else {}),
        **({"summary": summary} if summary and not lede else {}),
        **({"maker": maker} if maker else {}),
        "topics": topics,
        "link": link,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "updated": max([last_seen] + [p["date"] for p in poss]),
        "possibilities_total": len(poss) + (older or {}).get("possibilities", 0),
        "days_seen": len(timeline) + o.get("days", 0),
        "days_featured": len([a for a in timeline if a.get("rank")]) + o.get("days_featured", 0),
        **({"best_rank": min(ranks)} if ranks else {}),
        **({"best_impact": max(impacts)} if impacts else {}),
        "peak": {"signal": peak["signal"], "date": peak["date"]},
        "find": where,
        "spotted_by": spotted_by,
        "knock_on": knock_on,
        "possibilities": poss,
        "works_with": works_with,
        "constraints": constraints,
        "appearances": apps,
        "timeline": timeline,
        **({"older": older} if older else {}),
    }
    if key.startswith("policy:") or latest["kind"] == "constraint":
        rec = next((c for c in register if c.get("key") == key), None)
        if rec:
            page["constraint"] = {k: rec.get(k) for k in ACTION_FIELDS if rec.get(k) not in (None, "", [])}
        page["names"] = names or []
    return page


def _own_url(key: str, apps: list[dict], register: list[dict]) -> dict:
    """For a page, paper or action: the address the editions used for it, else the key's own."""
    from .util import url_key

    prefix, _, rest = key.partition(":")
    if prefix == "arxiv":
        return canonical_link(key, None)
    if prefix == "policy":
        rec = next((c for c in register if c.get("key") == key), {})
        doc = rec.get("doc_url") or rec.get("url") or next((s["url"] for a in apps for s in a["sources"]), None)
        return {"url": doc or f"https://{rest}", "label": "The document"}
    own = next((s["url"] for a in apps for s in a["sources"] if url_key(s["url"]) == key), None)
    return canonical_link(key, own or f"https://{rest}")


def display_name(key: str, link: dict, apps: list[dict]) -> str:
    """What to call it: owner/repo for code and weights, the feed's name for a changelog, else its title."""
    latest = apps[0]
    if key.startswith(("gh:", "hf:", "hfs:")):
        path = link["url"].split("huggingface.co/spaces/", 1)[-1] if key.startswith("hfs:") else link["url"].split("/", 3)[-1]
        if latest["kind"] == "changelog":
            return next((s["name"] for s in latest["sources"] if s.get("name")), path)
        return path
    if latest["kind"] == "changelog":
        return next((s["name"] for s in latest["sources"] if s.get("name")), latest["title"])
    if latest["kind"] == "constraint":
        return next((a["headline"] for a in apps if a.get("headline")), latest["title"])
    return latest["title"]


def row(a: dict) -> dict:
    """An appearance as one timeline row: enough for counts, peaks and the strip, nothing that grows."""
    out = {"date": a["date"], "id": a["id"], "signal": a["signal"], "evidence": a["evidence"]}
    for k in ("rank", "impact"):
        if a.get(k):
            out[k] = a[k]
    return out


def fold(rows: list[dict], older: dict | None = None) -> dict | None:
    """Rows past the timeline window, folded into the summary the page already carries. Every part of it is a
    count, a minimum or a maximum, so folding a row at a time (daily) equals folding them all (rebuild)."""
    if not rows:
        return dict(older) if older else None
    out = dict(older or {})
    for r in rows:
        out["days"] = out.get("days", 0) + 1
        out["first_seen"] = min(out.get("first_seen") or r["date"], r["date"])
        if r.get("rank"):
            out["days_featured"] = out.get("days_featured", 0) + 1
            out["best_rank"] = min(out.get("best_rank", r["rank"]), r["rank"])
        if r.get("impact"):
            out["best_impact"] = max(out.get("best_impact", r["impact"]), r["impact"])
        pk = out.get("peak")
        if not pk or (r["signal"], r["date"]) > (pk["signal"], pk["date"]):
            out["peak"] = {"signal": r["signal"], "date": r["date"]}
    return out


def page_contribs(page: dict) -> dict[str, dict]:
    """Back from a page to its per-edition contributions, so one edition's entries can be replaced.
    Editions older than the full window come back as timeline rows, which is all the page keeps of them."""
    out: dict[str, dict] = {}
    for r in page.get("timeline", []):
        out.setdefault(r["date"], {"appearance": None, "possibilities": []})["appearance"] = dict(r, row=True)
    for a in page.get("appearances", []):
        out.setdefault(a["date"], {"appearance": None, "possibilities": []})["appearance"] = a
    for p in page.get("possibilities", []):
        out.setdefault(p["date"], {"appearance": None, "possibilities": []})["possibilities"].append(p)
    return out


def index_entry(page: dict) -> dict:
    dates = sorted({r["date"] for r in page["timeline"]} | {p["date"] for p in page["possibilities"]}, reverse=True)
    return {
        "slug": page["slug"], "key": page["key"], "kind": page["kind"], "name": page["name"],
        **({"headline": page["headline"]} if page.get("headline") else {}),
        **({"maker": page["maker"]} if page.get("maker") else {}),
        "topics": page["topics"][:3],
        "first_seen": page["first_seen"], "last_seen": page["last_seen"],
        "days_seen": page["days_seen"], "days_featured": page["days_featured"],
        **({"best_impact": page["best_impact"]} if page.get("best_impact") else {}),
        "possibilities": page.get("possibilities_total", len(page["possibilities"])),
        "constraints": len(page["constraints"]),
        "dates": dates,
    }


# ---------- reading and writing ----------

class Store:
    """Element files under data/v1/elements, with the day files they come from."""

    def __init__(self, data_dir: Path, state_dir: Path | None = None) -> None:
        self.data = Path(data_dir)
        self.dir = self.data / "elements"
        self.state = Path(state_dir) if state_dir else None
        self._days: dict[str, dict | None] = {}
        self._contribs: dict[str, dict] = {}
        self.register = load_json(self.data / "constraints.json", {}).get("items", [])
        from .util import ROOT
        topics = load_json(ROOT / "config/topics.json", {}).get("topics", {})
        self.topic_labels = {k: v.get("label", k) for k, v in topics.items()}
        self.index = load_json(self.dir / "index.json", None) or {"schema": INDEX_SCHEMA, "since": None, "updated": None, "elements": []}
        self.by_key = {e["key"]: e for e in self.index["elements"]}
        # Element pages start on `since`: no edition before it is ever read for appearances.
        self.since: str | None = self.index.get("since")

    def day(self, date: str) -> dict | None:
        if date not in self._days:
            self._days[date] = load_json(self.data / "days" / f"{date}.json", None)
        return self._days[date]

    def contribs(self, date: str) -> dict[str, dict]:
        if date not in self._contribs:
            day = self.day(date)
            self._contribs[date] = contributions(day, self.day) if day else {}
        return self._contribs[date]

    def all_dates(self) -> list[str]:
        return sorted(p.stem for p in (self.data / "days").glob("*.json"))

    def dates(self) -> list[str]:
        """The editions element pages draw on: from `since` onwards."""
        return [d for d in self.all_dates() if not self.since or d >= self.since]

    def load(self, slug: str) -> dict | None:
        return load_json(self.dir / f"{slug}.json", None)

    def write(self, page: dict) -> bool:
        """Write one page through the redaction gate. A page the gate blocks is skipped, never the edition."""
        from .publish import scrub
        try:
            scrub(page)
        except SystemExit as ex:
            print(f"element {page['slug']} not written, and any older copy removed: {ex}", file=sys.stderr)
            self.drop(page["key"])
            (self.dir / f"{page['slug']}.json").unlink(missing_ok=True)
            return False
        write_json(self.dir / f"{page['slug']}.json", page, compact=True)
        self.by_key[page["key"]] = index_entry(page)
        return True

    def drop(self, key: str) -> None:
        entry = self.by_key.pop(key, None)
        if entry:
            (self.dir / f"{entry['slug']}.json").unlink(missing_ok=True)

    def names_for(self, key: str) -> list[dict]:
        """For an official action: the element pages whose maker it names."""
        rec = next((c for c in self.register if c.get("key") == key), None)
        if not rec:
            return []
        text = " ".join(str(rec.get(k) or "") for k in ("headline", "title", "harder", "who", "quote"))
        hits = [e for e in self.by_key.values() if e["key"] != key and _names(text, e.get("maker"))]
        hits.sort(key=lambda e: (-e["days_seen"], e["key"]))
        return [{"key": e["key"], "title": e["name"], "maker": e["maker"]} for e in hits[:24]]

    def save_index(self, date: str | None) -> None:
        elements = sorted(self.by_key.values(), key=lambda e: (e["last_seen"], e["days_seen"], e["key"]), reverse=True)
        updated = max([date or ""] + [e["last_seen"] for e in elements]) or None
        self.index = {"schema": INDEX_SCHEMA, "since": self.since, "updated": updated, "actions": self.action_digests(), "elements": elements}
        write_json(self.dir / "index.json", self.index, compact=True)

    def history_first_seen(self, key: str) -> str | None:
        if self.state is None:
            return None
        if not hasattr(self, "_history"):
            self._history = load_json(self.state / "history.json", {"keys": {}}).get("keys", {})
        return (self._history.get(key) or {}).get("first_seen")

    def action_digests(self) -> dict[str, str]:
        import json
        return {c["key"]: short_hash(json.dumps(c, sort_keys=True, ensure_ascii=False), 12) for c in self.register if c.get("key")}

    def changed_actions(self) -> set[str]:
        """Actions added, changed or removed since the pages were last written (by fingerprint)."""
        now, before = self.action_digests(), self.index.get("actions") or {}
        return {k for k in now if before.get(k) != now[k]} | {k for k in before if k not in now}

    def refresh_actions(self) -> None:
        """An action's own page follows the register (status, document, who it hits) and lists the pages whose
        maker it names; both change without the action appearing again, so recompute them every run."""
        for e in list(self.by_key.values()):
            if not e["key"].startswith("policy:") and e["kind"] != "constraint":
                continue
            page = self.load(e["slug"])
            if not page:
                continue
            rec = next((c for c in self.register if c.get("key") == e["key"]), None)
            fresh = dict(page, names=self.names_for(e["key"]))
            if rec:
                fresh["constraint"] = {k: rec.get(k) for k in ACTION_FIELDS if rec.get(k) not in (None, "", [])}
            else:
                fresh.pop("constraint", None)  # the register no longer holds it: publish none of its old details
            if fresh != page:
                self.write(fresh)


def update(date: str, data_dir: Path, state_dir: Path | None = None) -> dict:
    """Fold one published edition into the element pages. Safe to run again for the same date."""
    store = Store(data_dir, state_dir)
    if store.day(date) is None:
        raise FileNotFoundError(f"no edition for {date}")
    if not store.since:
        store.since = date  # the first update with no seed starts the pages today
    if date < store.since:
        print(f"elements: {date} is before the pages start ({store.since}); nothing to fold", file=sys.stderr)
        return {"date": date, "written": 0, "dropped": 0, "elements": len(store.by_key)}
    today = store.contribs(date)
    changed = store.changed_actions()
    touched = set(today) | {e["key"] for e in store.by_key.values() if date in e["dates"]}
    counts = {"written": 0, "dropped": 0}

    def put(key: str, contribs: dict[str, dict], older: dict | None) -> None:
        if not qualifies(contribs, key, store.register, older):
            if key in store.by_key:
                store.drop(key)
                counts["dropped"] += 1
            return
        names = store.names_for(key) if key.startswith("policy:") else None
        counts["written"] += store.write(assemble(key, slug_for(key), contribs, store.register, store.topic_labels, names, older))

    for key in sorted(touched):
        page = store.load(store.by_key[key]["slug"]) if key in store.by_key else None
        if key in store.by_key and (page is None or not patchable(page, date)):
            put(key, from_editions(store, key), None)  # a lost file, or a day the page no longer holds in full
            continue
        if page:
            contribs, older = page_contribs(page), page.get("older")
        else:
            contribs, older = earlier(store, key, date), None
        contribs.pop(date, None)
        if key in today:
            contribs[date] = today[key]
        put(key, contribs, older)

    # A new, corrected or removed action can link or unlink any element since the pages started: recompute those
    # from the editions, exactly as a rebuild would.
    for key in sorted(affected_by(store, changed)):
        put(key, from_editions(store, key), None)
    store.refresh_actions()
    store.save_index(date)
    result = {"date": date, **counts, "elements": len(store.by_key), "actions_changed": len(changed)}
    print(f"elements {date}: {counts['written']} written, {counts['dropped']} dropped, {len(store.by_key)} pages, "
          f"{len(changed)} actions changed", file=sys.stderr)
    return result


def patchable(page: dict, date: str) -> bool:
    """Whether replacing `date` in the page is exact: the page must still hold that day in full."""
    older = page.get("older") or {}
    oldest_row = min((r["date"] for r in page.get("timeline", [])), default=date)
    oldest_full = min((a["date"] for a in page.get("appearances", [])), default=date)
    oldest_idea = min((p["date"] for p in page.get("possibilities", [])), default=date)
    if older.get("days") and date < oldest_row:
        return False
    if older.get("links") and date < oldest_full:
        return False
    if older.get("possibilities") and date <= oldest_idea:  # folded ideas may share the oldest kept date
        return False
    return True


def from_editions(store: Store, key: str) -> dict[str, dict]:
    """Every contribution to `key` in the editions since the pages started."""
    out = {}
    for d in store.dates():
        c = store.contribs(d).get(key)
        if c:
            out[d] = c
    return out


def affected_by(store: Store, changed: set[str]) -> set[str]:
    """Elements a changed action could link or unlink: pages that link it now, and every element in the editions since
    the pages started whose URLs match its document or sources, or whose maker it names."""
    if not changed:
        return set()
    keys = set()
    for e in store.by_key.values():
        if e.get("constraints"):
            page = store.load(e["slug"])
            if page and ({c["key"] for c in page.get("constraints", [])} | set((page.get("older") or {}).get("links", {}))) & changed:
                keys.add(e["key"])
    actions = [c for c in store.register if c.get("key") in changed]
    texts = [_action_text(c) for c in actions]
    for d in store.dates():
        for key, c in store.contribs(d).items():
            a = c.get("appearance")
            if not a or key in keys:
                continue
            if url_matches(key, _urls([a]), actions) or any(_names(t, a.get("maker")) for t in texts):
                keys.add(key)
    return keys


def earlier(store: Store, key: str, date: str) -> dict[str, dict]:
    """A new page's past: every earlier edition that carried the key, from its first sighting on."""
    first = store.history_first_seen(key)
    if first == date:
        return {}
    out = {}
    for d in store.dates():
        if d >= date or (first and d < first):
            continue
        c = store.contribs(d).get(key)
        if c:
            out[d] = c
    return out


def rebuild(data_dir: Path, state_dir: Path | None = None, since: str | None = None) -> dict:
    """Every page from the editions since `since` (default: the pages' current start, else the last
    SEED_EDITIONS editions). Agrees byte for byte with a run of daily updates from that date."""
    store = Store(data_dir, state_dir)
    every = store.all_dates()
    seed = every[-SEED_EDITIONS] if len(every) >= SEED_EDITIONS else (every[0] if every else None)
    # Never a backfill: a rebuild starts exactly where the pages already start, or within the last SEED_EDITIONS editions.
    if since and since != store.since and seed and since < seed:
        raise ValueError(f"element pages start where they already start ({store.since}) or within the last {SEED_EDITIONS} "
                         f"editions (from {seed}); {since} would backfill the archive")
    store.since = since or store.since or seed
    store.index = {"schema": INDEX_SCHEMA, "since": store.since, "updated": None, "elements": []}
    store.by_key = {}
    if store.dir.exists():
        for p in store.dir.glob("*.json"):
            p.unlink()
    by_key: dict[str, dict[str, dict]] = defaultdict(dict)
    for d in store.dates():
        for key, c in store.contribs(d).items():
            by_key[key][d] = c
    pages = [(key, slug_for(key), contribs) for key, contribs in sorted(by_key.items()) if qualifies(contribs, key, store.register)]
    for key, slug, contribs in pages:
        if not key.startswith("policy:"):
            store.write(assemble(key, slug, contribs, store.register, store.topic_labels))
    for key, slug, contribs in pages:
        if key.startswith("policy:"):
            store.write(assemble(key, slug, contribs, store.register, store.topic_labels, store.names_for(key)))
    last = store.dates()[-1] if store.dates() else None
    store.save_index(last)
    print(f"elements rebuilt: {len(store.by_key)} pages from {len(store.dates())} editions since {store.since}", file=sys.stderr)
    return {"elements": len(store.by_key), "editions": len(store.dates()), "since": store.since}


def run(date: str | None = None, *, full: bool = False, since: str | None = None) -> None:
    from .publish import DATA, STATE

    if full:
        try:
            rebuild(DATA, STATE, since)
        except ValueError as ex:
            raise SystemExit(str(ex))
        return
    if not date:
        date = load_json(DATA / "index.json", {}).get("latest")
    if not date:
        raise SystemExit("no edition to fold into the element pages")
    update(date, DATA, STATE)
