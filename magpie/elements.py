"""Element pages: one file per thing Magpie keeps meeting (a repo, model, paper, tool or official action).

An edition is one day. An element is one thing across days: what it is, where to find it, what it works with and
connects to, and every edition it appeared in. Pages are computed from the published editions and the constraints
register; no model is called.

Every publish rebuilds every page from scratch, from the editions since the pages started (`since` in the index; the
first build starts at the last seven editions). Nothing is patched in place, so a page always says exactly what the
editions say: re-publishing a day, a new or corrected official action, or a removed one all show up on the next build.

A page exists for an element seen on two or more days, an ingredient of a new possibility, an official action, or one
an action rests on (its document), cites (as a source) or names the maker of.

    data/v1/elements/<slug>.json   one element (magpie.element/1)
    data/v1/elements/index.json    every element page (magpie.elements/1)
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .util import ROOT, load_json, short_hash, write_json

SCHEMA = "magpie.element/1"
INDEX_SCHEMA = "magpie.elements/1"
GENERIC_MAKERS = {"Open source", "Research", "Other"}
SEED_EDITIONS = 7   # a first build starts this many editions back; a later start may only be the current one or among these
KEEP = 60           # newest appearances and possibilities kept in full on a page; counts and the timeline cover all
CROWD = 6           # a post that ties more than this many items in a day is a roundup, not a connection
PER_APPEARANCE = 8  # sources, mentions and ties kept per appearance
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
    ww = b.get("works_with") if isinstance(b.get("works_with"), list) else []
    named = [{"name": w["name"], **({"how": w["how"]} if isinstance(w.get("how"), str) and w["how"] else {})}
             for w in ww if isinstance(w, dict) and isinstance(w.get("name"), str) and w["name"]]
    if named:
        a["named_integrations"] = named  # checked against that day's sources by the editor step
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


# ---------- one element ----------

def _names(text: str, maker: str | None) -> bool:
    if not maker or maker in GENERIC_MAKERS or len(maker) < 3:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(maker.lower())}(?![a-z0-9])", text.lower()) is not None


def _name_key(name: str) -> str:
    """One integration under its spellings: "Llama-3.1" and "llama 3.1" are the same name."""
    return re.sub(r"[^a-z0-9]", "", name.lower()) or name


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").split("#", 1)[0]).rstrip("/").lower()


def _action_text(c: dict) -> str:
    return " ".join(str(c.get(k) or "") for k in ("headline", "title", "harder", "who", "quote"))


def action_links(key: str, maker: str | None, urls: set[str], register: list[dict]) -> list[dict]:
    """Official actions that rest on a document this element links to, cite it as a source, or name its maker."""
    urls = {_norm_url(u) for u in urls if u}
    out = []
    for c in register:
        if c.get("key") == key:
            continue
        docs = {_norm_url(c.get("doc_url") or ""), _norm_url(c.get("url") or "")} - {""}
        reports = {_norm_url(s.get("url", "")) for s in c.get("sources") or []} - {""} - docs
        why = ("document" if urls & docs else "reported" if urls & reports
               else "maker" if _names(_action_text(c), maker) else None)
        if why:
            out.append({"key": c["key"], "id": c.get("id"), "headline": c.get("headline") or c.get("title") or "",
                        "status": c.get("status"), "jurisdiction": c.get("jurisdiction"), "issuer": c.get("issuer"),
                        "url": c.get("doc_url") or c.get("url"), "first_seen": c.get("first_seen"),
                        "why": {"document": "It rests on a document this element links to.",
                                "reported": "This element is one of the sources that reported it."}.get(why, f"It names {maker}."),
                        "match": why})
    out.sort(key=lambda c: (["document", "reported", "maker"].index(c["match"]), c["key"]))
    return out


def element(key: str, apps: list[dict], poss: list[dict], register: list[dict], topic_labels: dict[str, str]) -> dict | None:
    """The page for one key from all its appearances and the possibilities it fed (both newest first), or None when it
    does not get a page."""
    if not apps:
        return None
    latest = apps[0]
    maker = next((a["maker"] for a in apps if a.get("maker")), None)
    if key.startswith(("gh:", "hf:", "hfs:")):
        link = canonical_link(key, next((s["url"] for a in apps for s in a.get("sources", [])
                                         if _norm_url(s["url"]).startswith(("github.com", "huggingface.co"))), None))
    else:
        link = _own_url(key, apps, register)
    urls = {link["url"]} | {u for a in apps for s in a.get("sources", []) + a.get("mentions", [])
                            for u in (s["url"], s.get("discussion")) if u}
    constraints = action_links(key, maker, urls, register)
    is_action = key.startswith("policy:") or latest["kind"] == "constraint"
    if len(apps) < 2 and not poss and not is_action and not constraints:
        return None

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

    # Named integrations: tools, models or platforms the editor named that the sources' own words spell, by how often.
    integrations: dict[str, dict] = {}
    for a in apps:  # newest first: the latest spelling and clauses lead
        for k, w in {_name_key(w["name"]): w for w in reversed(a.get("named_integrations", []))}.items():
            g = integrations.setdefault(k, {"name": w["name"], "days": 0, "first_seen": a["date"], "last_seen": a["date"], "how": []})
            g["days"] += 1
            g["first_seen"] = a["date"]
            if w.get("how") and len(g["how"]) < 3 and w["how"] not in g["how"]:
                g["how"].append(w["how"])
    named = sorted(integrations.values(), key=lambda g: g["name"].lower())
    named = sorted(named, key=lambda g: (g["days"], g["last_seen"]), reverse=True)[:24]

    headline = next((a["headline"] for a in apps if a.get("headline")), None)
    lede = next((a.get("newly_possible") or a.get("newly_constrained") for a in apps if a.get("newly_possible") or a.get("newly_constrained")), None)
    summary = next((a["summary"] for a in apps if a.get("summary")), None)
    topics = [t for t, _ in Counter(t for a in apps for t in a.get("topics", [])).most_common(4)]
    ranks = [a["rank"] for a in apps if a.get("rank")]
    impacts = [a["impact"] for a in apps if a.get("impact")]
    peak = max(apps, key=lambda a: (a["signal"], a["date"]))
    page = {
        "schema": SCHEMA,
        "slug": slug_for(key),
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
        "first_seen": apps[-1]["date"],
        "last_seen": latest["date"],
        "updated": max([latest["date"]] + [p["date"] for p in poss]),
        "possibilities_total": len(poss),
        "days_seen": len(apps),
        "days_featured": len(ranks),
        **({"best_rank": min(ranks)} if ranks else {}),
        **({"best_impact": max(impacts)} if impacts else {}),
        "peak": {"signal": peak["signal"], "date": peak["date"]},
        "find": where,
        "spotted_by": spotted_by,
        "knock_on": knock_on,
        "possibilities": poss[:KEEP],
        "works_with": works_with,
        **({"named_integrations": named} if named else {}),
        "constraints": constraints,
        "appearances": apps[:KEEP],
        "timeline": [{"date": a["date"], "id": a["id"], "signal": a["signal"], "evidence": a["evidence"],
                      **{k: a[k] for k in ("rank", "impact") if a.get(k)}} for a in apps],
    }
    if is_action:
        rec = next((c for c in register if c.get("key") == key), None)
        if rec:  # a record the register no longer holds publishes none of its old details
            page["constraint"] = {k: rec.get(k) for k in ACTION_FIELDS if rec.get(k) not in (None, "", [])}
        page["names"] = []
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


def index_entry(page: dict) -> dict:
    return {
        "slug": page["slug"], "key": page["key"], "kind": page["kind"], "name": page["name"],
        **({"headline": page["headline"]} if page.get("headline") else {}),
        **({"maker": page["maker"]} if page.get("maker") else {}),
        "topics": page["topics"][:3],
        "first_seen": page["first_seen"], "last_seen": page["last_seen"],
        "days_seen": page["days_seen"], "days_featured": page["days_featured"],
        **({"best_impact": page["best_impact"]} if page.get("best_impact") else {}),
        "possibilities": page["possibilities_total"],
        "constraints": len(page["constraints"]),
    }


# ---------- the build ----------

def build(data_dir: Path, state_dir: Path | None = None, since: str | None = None) -> dict:
    """Every element page from the editions since the pages started. `since` may only keep the current start or move
    it to one of the last SEED_EDITIONS editions: the pages never reach back into the archive."""
    from .publish import scrub

    data, out_dir = Path(data_dir), Path(data_dir) / "elements"
    every = sorted(p.stem for p in (data / "days").glob("*.json"))
    current = load_json(out_dir / "index.json", {}).get("since")
    recent = every[-SEED_EDITIONS:]
    if since and since != current and since not in recent:
        raise ValueError(f"element pages start where they already start ({current}) or on one of the last {SEED_EDITIONS} "
                         f"editions ({', '.join(recent)}); {since} is neither")
    start = since or current or (recent[0] if recent else None)

    days: dict[str, dict | None] = {}

    def day(d: str) -> dict | None:
        if d not in days:
            days[d] = load_json(data / "days" / f"{d}.json", None)
        return days[d]

    apps: dict[str, list[dict]] = defaultdict(list)
    poss: dict[str, list[dict]] = defaultdict(list)
    for d in (d for d in every if start and d >= start):
        if not day(d):
            continue
        for key, c in contributions(day(d), day).items():
            if c["appearance"]:
                apps[key].append(c["appearance"])
            poss[key] += c["possibilities"]

    register = load_json(data / "constraints.json", {}).get("items", [])
    labels = {k: v.get("label", k) for k, v in load_json(ROOT / "config/topics.json", {}).get("topics", {}).items()}
    pages = {}
    for key in sorted(set(apps) | set(poss)):
        page = element(key, apps[key][::-1], sorted(poss[key], key=lambda p: (p["date"], -p["rank"]), reverse=True), register, labels)
        if page:
            pages[key] = page
    # An action's page lists the pages whose maker it names.
    for key, page in pages.items():
        rec = next((c for c in register if c.get("key") == key), None)
        if "names" in page and rec:
            hits = [p for k, p in pages.items() if k != key and _names(_action_text(rec), p.get("maker"))]
            hits.sort(key=lambda p: (-p["days_seen"], p["key"]))
            page["names"] = [{"key": p["key"], "title": p["name"], "maker": p["maker"]} for p in hits[:24]]

    out_dir.mkdir(parents=True, exist_ok=True)
    written, entries = {"index.json"}, []
    for page in pages.values():
        try:
            scrub(page)
        except SystemExit as ex:  # the redaction gate: skip the page, never the edition
            print(f"element {page['slug']} not written: {ex}", file=sys.stderr)
            continue
        write_json(out_dir / f"{page['slug']}.json", page, compact=True)
        written.add(f"{page['slug']}.json")
        entries.append(index_entry(page))
    for f in out_dir.glob("*.json"):
        if f.name not in written:
            f.unlink()
    entries.sort(key=lambda e: (e["last_seen"], e["days_seen"], e["key"]), reverse=True)
    updated = max([d for d in every if start and d >= start] or [None], key=lambda d: d or "")
    write_json(out_dir / "index.json", {"schema": INDEX_SCHEMA, "since": start, "updated": updated, "elements": entries}, compact=True)
    print(f"elements: {len(entries)} pages from the editions since {start}", file=sys.stderr)
    return {"since": start, "elements": len(entries)}


def run(since: str | None = None) -> None:
    from .publish import DATA, STATE

    try:
        build(DATA, STATE, since)
    except ValueError as ex:
        raise SystemExit(str(ex))
