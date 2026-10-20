"""Two standing trackers that put the constraints in numbers.

The gap: how far open-weight models trail the closed frontier, computed from Epoch AI's Capabilities Index
(eci_scores.csv, CC BY 4.0). Rules that slow the next open release act on this number, not on weights people
already have. Lag is measured the way Epoch does: when the open frontier reaches a level, how long ago did a
closed model first reach it.

Local hardware: what it costs to run open weights at home. Specs come from the makers' spec pages; prices are
dated observations with a source page (config/hardware.json seeds them, the Scout adds a weekly check in
inbox/hardware-prices.json). Decode is bandwidth-bound, so each device shows the tokens-per-second ceiling for
a reference model: memory bandwidth divided by the model's size.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import sys
import time
import zipfile

from .util import ROOT, fetch, load_json, write_json

EPOCH_ZIP = "https://epoch.ai/data/benchmark_data.zip"
EPOCH_SOURCE = {"name": "Epoch AI, Capabilities Index", "url": "https://epoch.ai/benchmarks", "license": "CC BY 4.0"}
MONTH = 30.44
HISTORY_FROM = "2024-01-01"
USABLE = 0.8          # share of a device's memory left for weights once the runtime and context take theirs
GB_PER_B_4BIT = 0.55  # gigabytes per billion parameters at 4-bit, with quantization overhead
STALE_DAYS = 21


def _eci_rows(state_dir) -> list[dict]:
    cache = state_dir / "epoch" / "benchmark_data.zip"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 20 * 3600:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(fetch(EPOCH_ZIP, timeout=60))
    with zipfile.ZipFile(cache) as z:
        name = next(n for n in z.namelist() if n.endswith("eci_scores.csv"))
        text = z.read(name).decode("utf-8", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            rows.append({"model": r["Model"], "org": r.get("Organization") or "", "country": r.get("Country (of organization)") or "",
                         "group": r.get("Accessibility group") or "", "date": r["date"][:10], "eci": float(r["eci"])})
        except (KeyError, ValueError):
            continue
    return rows


def _records(rows: list[dict]) -> list[dict]:
    """Each model that set a new high for its group, in date order."""
    best, out = float("-inf"), []
    for r in sorted(rows, key=lambda r: r["date"]):
        if r["eci"] > best:
            best = r["eci"]
            out.append(r)
    return out


def _brief(r: dict | None) -> dict | None:
    return {k: r[k] for k in ("model", "org", "country", "date", "eci")} if r else None


def gap(date: str, state_dir) -> dict:
    rows = [r for r in _eci_rows(state_dir) if r["date"] <= date]
    closed = _records([r for r in rows if r["group"] == "Closed weights"])
    opened = _records([r for r in rows if r["group"] == "Open weights"])
    if not closed or not opened:
        raise RuntimeError("Epoch data has no open or closed records")

    def match(score: float) -> dict | None:
        return next((c for c in closed if c["eci"] >= score), None)

    def lag(r: dict) -> float:
        m = match(r["eci"])
        return round(max(0, (dt.date.fromisoformat(r["date"]) - dt.date.fromisoformat(m["date"])).days) / MONTH, 1) if m else 0.0

    lead = opened[-1]
    m = match(lead["eci"])
    today = round((dt.date.fromisoformat(date) - dt.date.fromisoformat(m["date"])).days / MONTH, 1) if m else 0.0
    china = _records([r for r in rows if r["group"] == "Open weights" and "China" in r["country"]])
    us = _records([r for r in rows if r["group"] == "Open weights" and "United States" in r["country"]])
    history = [dict(_brief(r), lag_months=lag(r)) for r in opened if r["date"] >= HISTORY_FROM]
    return {
        "as_of": date,
        "lag_months": lag(lead),
        "today_months": today,
        "points_behind": round(max(0.0, closed[-1]["eci"] - lead["eci"]), 1),
        "open_leads": lead["eci"] > closed[-1]["eci"],
        "open": _brief(lead),
        "closed_match": _brief(m),
        "closed_frontier": _brief(closed[-1]),
        "china": dict(_brief(china[-1]), lag_months=lag(china[-1])) if china else None,
        "us": dict(_brief(us[-1]), lag_months=lag(us[-1])) if us else None,
        "history": history,
        "source": dict(EPOCH_SOURCE, retrieved=date),
        "method": "Lag is how long before the open model's release a closed model first reached the same Capabilities Index score. "
                  "Today's figure measures from that closed model to today, so it grows until an open model moves the frontier.",
    }


def observations(data_dir) -> list[dict]:
    """Every dated price Magpie knows: config seeds, the published history and the Scout's inbox, deduplicated."""
    cfg = load_json(ROOT / "config/hardware.json", {})
    known = {d["id"] for d in cfg.get("devices", [])}
    pool = (cfg.get("observations", []) + load_json(data_dir / "hardware.json", {}).get("observations", [])
            + load_json(ROOT / "inbox/hardware-prices.json", {}).get("observations", []))
    seen, out = set(), []
    for o in pool:
        try:
            key = (o["device"], o["date"][:10], o.get("kind", "list"), float(o["price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if o["device"] not in known or key in seen or not str(o.get("url", "")).startswith("https://"):
            continue
        if o.get("currency", "USD") != "USD":
            continue  # every comparison here is in dollars
        seen.add(key)
        out.append({"device": o["device"], "date": o["date"][:10], "kind": o.get("kind", "list"), "price": float(o["price"]),
                    "currency": o.get("currency", "USD"), "url": o["url"], "by": o.get("by", "magpie"),
                    **({"note": o["note"]} if o.get("note") else {})})
    out.sort(key=lambda o: (o["device"], o["date"], o["kind"]))
    return out


def hardware(date: str, data_dir) -> dict:
    cfg = load_json(ROOT / "config/hardware.json", {})
    ref = cfg.get("reference", {"name": "a 27B model at 4-bit", "gb": 16})
    obs = [o for o in observations(data_dir) if o["date"] <= date]
    rows = []
    for d in cfg.get("devices", []):
        mine = [o for o in obs if o["device"] == d["id"]]
        lists = [o for o in mine if o["kind"] == "list"]
        streets = [o for o in mine if o["kind"] == "street"]
        now_list, first_list = (lists[-1], lists[0]) if lists else (None, None)
        now_street = streets[-1] if streets else None
        price = (now_street or now_list or {}).get("price")
        latest = max((o["date"] for o in mine), default=None)
        rows.append({
            "id": d["id"], "name": d["name"], "maker": d["maker"], "form": d.get("form"),
            "memory_gb": d["memory_gb"], "bandwidth_gbs": d["bandwidth_gbs"], "spec_url": d.get("spec_url"),
            "list": {k: now_list[k] for k in ("price", "date", "url")} if now_list else None,
            "street": {k: now_street[k] for k in ("price", "date", "url")} if now_street else None,
            "first_list": {k: first_list[k] for k in ("price", "date", "url")} if first_list else None,
            "list_change_pct": round(100 * (now_list["price"] / first_list["price"] - 1), 1)
            if now_list and first_list and first_list["date"] < now_list["date"] else None,
            "street_premium_pct": round(100 * (now_street["price"] / now_list["price"] - 1), 1) if now_street and now_list else None,
            "ceiling_tps": round(d["bandwidth_gbs"] / ref["gb"]),
            "fits_b": int(d["memory_gb"] * USABLE / GB_PER_B_4BIT),
            "dollars_per_gbs": round(price / d["bandwidth_gbs"], 2) if price else None,
            "stale": bool(latest and (dt.date.fromisoformat(date) - dt.date.fromisoformat(latest)).days > STALE_DAYS),
        })
    rows.sort(key=lambda r: -r["ceiling_tps"])
    return {"as_of": date, "reference": ref, "devices": rows, "finding": _finding(rows),
            "method": f"Ceiling = memory bandwidth / {ref['gb']} GB of weights; real decode runs below it. "
                      f"Fits = {int(USABLE * 100)}% of memory at {GB_PER_B_4BIT} GB per billion parameters (4-bit). "
                      "List prices are the maker's own store; street prices are dated checks of a major retailer."}


def _finding(rows: list[dict]) -> str:
    moved = [r for r in rows if r["list_change_pct"]]
    if moved:
        r = max(moved, key=lambda r: abs(r["list_change_pct"]))
        verb = "more" if r["list_change_pct"] > 0 else "less"
        return f"{r['name']} lists for {abs(r['list_change_pct']):g}% {verb} than when Magpie first checked on {r['first_list']['date']}"
    street = [r for r in rows if r["street_premium_pct"] and r["street_premium_pct"] >= 10]
    if street:
        r = max(street, key=lambda r: r["street_premium_pct"])
        return f"{r['name']} sells for {r['street_premium_pct']:g}% over list at retail"
    fastest = rows[0] if rows else None
    roomiest = max(rows, key=lambda r: r["fits_b"]) if rows else None
    if fastest and roomiest and fastest["id"] != roomiest["id"]:
        return (f"{fastest['name']} decodes fastest; {roomiest['name']} fits the biggest models")
    return "Local hardware prices are holding"


def build(date: str, data_dir, state_dir) -> dict:
    """Both trackers; a failure in one never blocks the other or the edition."""
    out, errors = {}, []
    for name, fn in (("gap", lambda: gap(date, state_dir)), ("hardware", lambda: hardware(date, data_dir))):
        try:
            out[name] = fn()
        except Exception as ex:
            errors.append(f"{name}: {type(ex).__name__}: {str(ex)[:160]}")
    if errors:
        out["errors"] = errors
        print(f"trackers: {errors}", file=sys.stderr)
    return out


def persist(date: str, data_dir) -> None:
    """Keep the full price history public, so inbox files can rotate without losing it."""
    obs = observations(data_dir)
    write_json(data_dir / "hardware.json", {"schema": "magpie.hardware/1", "updated": date,
                                             "devices": load_json(ROOT / "config/hardware.json", {}).get("devices", []),
                                             "observations": obs})
