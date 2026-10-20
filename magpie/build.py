"""Turn a day's items into ranked capability events.

One capability (a repo, a model, a paper, a launch) is mentioned by many sources. Items that
share a subject key merge into one event; items that only mention the key in their body add
corroboration. Code computes every score; models only write words (see enrich.py).
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict

from .util import CENTRAL, ROOT, load_json, now_utc, parse_time, short_hash, url_key

ACTIONABLE = {"repo": 1.0, "model": 1.0, "space": 0.9, "changelog": 0.95, "paper": 0.5, "news": 0.6,
              "blog": 0.6, "newsletter": 0.45, "video": 0.5, "social": 0.6, "community": 0.5}
KIND_ORDER = ["repo", "model", "space", "changelog", "paper", "news", "blog", "social", "video", "community", "newsletter"]
AI_SOURCES = {"x-saved", "hf-trending-models", "hf-trending-spaces", "hf-daily-papers", "x-grok-pulse", "x-grok-bot", "youtube-watchlist"}
FRESH_BY_NATURE = {"x-grok-pulse", "x-grok-bot", "x-saved", "youtube-watchlist"}
WEIGHTS = {"novelty": 0.24, "actionability": 0.18, "momentum": 0.24, "corroboration": 0.16, "trust": 0.08, "ripple": 0.10}


def source_group(source_id: str) -> str:
    if source_id.startswith("github-"):
        return "github"
    if source_id.startswith("hf-"):
        return "huggingface"
    return source_id


def _kw_hits(text: str, words: list[str]) -> int:
    t = f" {text.lower()} "
    n = 0
    for w in words:
        w = w.lower()
        if len(w) <= 3 or w.endswith("-"):
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}", t):
                n += 1
        elif w in t:
            n += 1
    return n


class Taxonomy:
    def __init__(self) -> None:
        v = load_json(ROOT / "config/vendors.json", {})
        self.vendors = v.get("vendors", {})
        self.owners = v.get("owners", {})
        self.topics = load_json(ROOT / "config/topics.json", {}).get("topics", {})

    def vendors_for(self, text: str, keys: list[str], hints: list[str]) -> list[str]:
        found = [h for h in hints if h]
        for k in keys:
            if k.startswith(("gh:", "hf:", "hfs:")):
                owner = k.split(":", 1)[1].split("/")[0]
                if owner in self.owners:
                    found.append(self.owners[owner])
        scores = Counter()
        for name, words in self.vendors.items():
            hits = _kw_hits(text, words)
            if hits:
                scores[name] = hits
        found += [n for n, _ in scores.most_common(3)]
        out = []
        for f in found:
            if f not in out:
                out.append(f)
        return out[:3]

    def maker_for(self, lead: dict, keys: list[str], mentioned: list[str]) -> str:
        """Who made it. A repo that uses Claude Code is not an Anthropic release."""
        if lead.get("vendor_hint"):
            return lead["vendor_hint"]
        for k in keys:
            if k.startswith(("gh:", "hf:", "hfs:")):
                owner = k.split(":", 1)[1].split("/")[0]
                return self.owners.get(owner, "Open source")
        if lead["source_kind"] == "paper":
            org = lead["extra"].get("org") or ""
            hit = self.vendors_for(org, [], []) if org else []
            return hit[0] if hit else "Research"
        return mentioned[0] if mentioned else "Open source"

    def topics_for(self, text: str) -> list[str]:
        scores = Counter()
        for tid, t in self.topics.items():
            hits = _kw_hits(text, t["kw"])
            if hits:
                scores[tid] = hits
        return [t for t, _ in scores.most_common(3)]


def _central_date(iso_s: str | None) -> str | None:
    t = parse_time(iso_s)
    return t.astimezone(CENTRAL).date().isoformat() if t else None


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ranked = sorted((v, k) for k, v in values.items() if v)
    n = len(ranked)
    return {k: (i + 1) / n for i, (v, k) in enumerate(ranked)} if n else {}


def build(items: list[dict], history: dict, date: str, repo_meta: dict | None = None) -> dict:
    tax = Taxonomy()
    repo_meta = repo_meta or {}
    now = now_utc()
    fresh_cut = now - dt.timedelta(hours=40)

    # Policy documents belong to the constraints lane (constraints.py), not the capability list.
    items = [it for it in items if it["source_kind"] != "policy"]

    # 0. Re-key page URLs with the current rule (cached items may carry older keys).
    for it in items:
        it["keys"] = [url_key(it["url"]) if k.startswith("url:") else k for k in it["keys"]] or [url_key(it["url"])]

    # 1. Cluster on shared subject keys (union-find).
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for it in items:
        ks = it["keys"]
        for k in ks[1:]:
            parent[find(k)] = find(ks[0])
    clusters: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        clusters[find(it["keys"][0])].append(it)
    key_to_cluster = {k: find(k) for k in parent}

    # 2. Mentions corroborate clusters that exist on their own merits.
    mentions: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        for m in it.get("mentions", []):
            c = key_to_cluster.get(m)
            if c and all(x["id"] != it["id"] for x in clusters[c]):
                mentions[c].append(it)

    # 3. Momentum percentiles across the whole day.
    metric_vals: dict[str, dict[str, float]] = defaultdict(dict)
    for c, its in clusters.items():
        for it in its:
            m = it["metrics"]
            for name, val in (("stars_day", m.get("stars_day")), ("stars_week", (m.get("stars_week") or 0) / 7),
                              ("stars_month", (m.get("stars_month") or 0) / 30), ("hn", m.get("hn_points")),
                              ("hf", m.get("hf_trending")), ("paper", m.get("hf_upvotes"))):
                if val:
                    metric_vals[name][c] = max(metric_vals[name].get(c, 0), float(val))
    pct = {name: _percentiles(v) for name, v in metric_vals.items()}

    events = []
    for c, its in clusters.items():
        its.sort(key=lambda x: (KIND_ORDER.index(x["source_kind"]) if x["source_kind"] in KIND_ORDER else 99, -x["trust"]))
        lead = its[0]
        times = [parse_time(x.get("published_at")) for x in its]
        times = [t for t in times if t]
        newest = max(times) if times else None
        trending = any(x["source_id"].startswith(("github-trending", "hf-trending", "hacker-news", "hf-daily")) for x in its)
        is_fresh = trending or (newest is not None and newest >= fresh_cut) or lead["source_id"] in FRESH_BY_NATURE
        if not is_fresh:
            continue
        text = " ".join(f"{x['title']} {x['summary']} {' '.join(x['extra'].get('topics') or [])} {' '.join(x['extra'].get('keywords') or [])}" for x in its)
        keys = sorted({k for x in its for k in x["keys"]})
        mentioned = tax.vendors_for(text, keys, [x.get("vendor_hint") for x in its])
        maker = tax.maker_for(lead, keys, mentioned)
        vendors = [maker] + [v for v in mentioned if v != maker][:2]
        topics = tax.topics_for(text)
        ai = bool(topics or mentioned) or any(x["extra"].get("ai") for x in its) or any(
            x["source_id"] in AI_SOURCES or x["source_kind"] in ("newsletter", "changelog") for x in its)
        if not ai:
            continue

        groups = {source_group(x["source_id"]) for x in its}
        ment_groups = {source_group(x["source_id"]) for x in mentions.get(c, [])} - groups
        n_groups = len(groups) + len(ment_groups)

        momentum = max([pct[name].get(c, 0) for name in pct] or [0])
        corroboration = min(1.0, (n_groups - 1) * 0.34 + len(mentions.get(c, [])) * 0.08)
        trust = max(x["trust"] for x in its)
        seen = history.get("keys", {}).get(c)
        if not seen or seen.get("first_seen") == date:
            novelty = 1.0
        else:
            age = (dt.date.fromisoformat(date) - dt.date.fromisoformat(seen["first_seen"])).days
            novelty = 0.65 if age <= 3 else 0.3
        meta = repo_meta.get(c.split(":", 1)[1]) if c.startswith("gh:") else None
        created = parse_time((meta or {}).get("created_at") or lead["extra"].get("created_at"))
        if created and (now - created).days <= 21:
            novelty = max(novelty, 0.9)
        actionability = max(ACTIONABLE.get(x["source_kind"], 0.5) for x in its)
        if lead["source_kind"] == "paper" and lead["extra"].get("github"):
            actionability = 0.8
        ripple = min(1.0, max(0, len(topics) - 1) / 2 * 0.7 + max(0, len(vendors) - 1) * 0.15 + min(len(mentions.get(c, [])), 4) * 0.05)

        parts = {"novelty": novelty, "actionability": actionability, "momentum": momentum,
                 "corroboration": corroboration, "trust": trust, "ripple": ripple}
        signal = round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
        featured = (seen or {}).get("featured")
        if featured and featured != date:
            days_since = (dt.date.fromisoformat(date) - dt.date.fromisoformat(featured)).days
            if days_since <= 7:
                signal = round(signal - 15, 1)

        # Doubt from X (a Community Note or credible pushback) marks the event down and travels with it.
        flags = [d for d in ((x.get("extra") or {}).get("doubt") for x in its + mentions.get(c, [])) if d and d.get("level", 0) >= 2]
        doubt = max(flags, key=lambda d: d["level"]) if flags else None
        if doubt:
            signal = round(signal - 6 * doubt["level"], 1)
        elif any((x.get("extra") or {}).get("saved") for x in its + mentions.get(c, [])):
            signal = round(signal + 5, 1)  # the owner saved it while scrolling: a small taste boost, never shown

        runnable = any(x["source_kind"] in ("repo", "model", "space") for x in its) or any(
            k.startswith(("gh:", "hf:", "hfs:")) for k in keys) or bool(lead["extra"].get("github"))
        if n_groups >= 3:
            evidence = "corroborated"
        elif runnable and n_groups >= 2:
            evidence = "reproduced"
        else:
            evidence = "reported"

        metrics = {}
        for x in its:
            for k, v in x["metrics"].items():
                if isinstance(v, (int, float)) and v is not None:
                    metrics[k] = max(metrics.get(k, v), v)
        title = lead["title"]
        summary = next((x["summary"] for x in its if x["summary"]), "")
        if lead["source_kind"] == "repo" and meta and not summary:
            summary = meta.get("description") or ""
        events.append({
            "id": short_hash(c, 12),
            "key": c,
            "kind": lead["source_kind"],
            "title": title,
            "summary": summary,
            "doubt": doubt,
            "url": lead["url"],
            "date": date,
            "published_at": newest.isoformat() if newest else None,
            "vendors": vendors,
            "maker": maker,
            "topics": topics,
            "evidence": evidence,
            "signal": signal,
            "parts": {k: round(v, 3) for k, v in parts.items()},
            "metrics": metrics,
            "language": lead["extra"].get("language"),
            "license": (meta or {}).get("license") or lead["extra"].get("license"),
            "created_at": iso_or_none(created),
            "sources": [{"name": x["source_name"], "kind": x["source_kind"], "url": x["url"], "title": x["title"],
                         **({"discussion": x["extra"]["discussion"]} if x["extra"].get("discussion") else {})} for x in its][:8],
            "mentions": [{"name": x["source_name"], "url": x["url"], "title": x["title"]} for x in mentions.get(c, [])][:8],
            "source_groups": n_groups,
            "_context": [x["extra"].get("private_context", "") for x in its if x["extra"].get("private_context")][:1],
        })

    events.sort(key=lambda e: -e["signal"])
    return {"events": events}


def iso_or_none(t):
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") if t else None


def rank_and_select(events: list[dict], top_n: int = 5, per_vendor: int = 2) -> list[str]:
    """One formula for every event: the code's signal blended with an impact (ripple) weight.

    The editor's impact rating wins when it exists; otherwise a deterministic estimate stands in.
    When the editor ran, only edited events may lead the page.
    """
    edited = any(e.get("brief") for e in events)
    for e in events:
        b = e.get("brief") or {}
        est = max(1, min(5, round(1 + e["parts"]["ripple"] * 2 + e["parts"]["momentum"] * 2)))
        e["impact"] = b.get("impact") or est
        e["rank_score"] = round(0.55 * e["signal"] + 0.45 * e["impact"] * 20 - (20 if b.get("hype") else 0), 1)
    events.sort(key=lambda e: -e["rank_score"])
    top, per = [], Counter()
    for e in events:
        if e["kind"] == "constraint" and not (e.get("constraint") or {}).get("access_now"):
            continue  # constraints have their own section; one leads only when it changes access today
        if edited and not e.get("brief"):
            continue
        if e["kind"] in ("newsletter", "community") and not e.get("brief"):
            continue
        if e["kind"] == "paper" and per["kind:paper"] >= 2:
            continue
        v = e.get("maker") or (e["vendors"] or ["open"])[0]
        if per[v] >= per_vendor and v not in ("Open source", "Research"):
            continue
        if per[v] >= per_vendor + 1:
            continue
        top.append(e["id"])
        per[v] += 1
        per[f"kind:{e['kind']}"] += 1
        if len(top) >= top_n:
            break
    return top


def updates_calendar(items: list[dict], tax: Taxonomy | None = None) -> list[dict]:
    tax = tax or Taxonomy()
    out, seen = [], set()
    for it in items:
        if it["source_kind"] not in ("changelog", "news"):
            continue
        d = _central_date(it.get("published_at"))
        if not d or it["url"] in seen:
            continue
        seen.add(it["url"])
        vendor = it.get("vendor_hint") or (tax.vendors_for(it["title"], it["keys"], []) or ["Other"])[0]
        out.append({"date": d, "vendor": vendor, "product": it["extra"].get("product") or it["source_name"],
                    "title": it["title"], "url": it["url"], "kind": it["source_kind"],
                    "summary": it["summary"][:240]})
    out.sort(key=lambda u: (u["date"], u["vendor"]), reverse=True)
    return out


def ripple_graph(events: list[dict], topics_cfg: dict, limit: int = 14) -> dict:
    nodes, links, used = [], [], Counter()
    for e in events[:limit]:
        b = e.get("brief") or {}
        targets = [r.get("topic") for r in b.get("ripples", []) if r.get("topic") in topics_cfg] or e["topics"]
        nodes.append({"id": e["id"], "type": "event", "label": e.get("brief", {}).get("headline") or e["title"],
                      "impact": e["impact"], "vendor": e.get("maker"), "evidence": e["evidence"]})
        for t in dict.fromkeys(targets):
            effect = next((r.get("effect") for r in b.get("ripples", []) if r.get("topic") == t), None)
            links.append({"source": e["id"], "target": f"topic:{t}", "effect": effect})
            used[t] += 1
    for t, n in used.items():
        nodes.append({"id": f"topic:{t}", "type": "topic", "label": topics_cfg[t]["label"], "weight": n})
    return {"nodes": nodes, "links": links}
