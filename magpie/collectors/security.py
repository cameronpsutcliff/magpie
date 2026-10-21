"""Primary security sources for the threats lane: events that change what is safe to run, trust or depend on.

Keyless and primary only: the CISA Known Exploited Vulnerabilities catalog (kept to the AI and developer stack),
OSV.dev advisories for a watch list of AI and developer packages, Mozilla's CA incident bugs (kept to compromises,
breaches and mis-issuance), and an inbox (inbox/threats.json) for items the owner or the Scout add by hand. Watch
lists live in config/security.json. Every item is tagged source_kind "security", so the capability build leaves it
alone; threats.py reads them. Each collector fails alone, and inside one a failed lookup only loses that lookup.

What a source states about itself (CVE and advisory ids, severity, fixed versions, KEV membership, dates) travels in
`extra`, so the lane can set those fields from the source and never from a model.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import math
import re
import urllib.parse
import urllib.request

from ..items import Item
from ..util import ROOT, UA, edition_date, fetch, load_json, parse_time, strip_html, url_key

CONFIG = ROOT / "config/security.json"
INBOX = ROOT / "inbox/threats.json"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV_PAGE = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog?field_cve={}"
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
OSV_PAGE = "https://osv.dev/vulnerability/{}"
CVE_API = "https://cveawg.mitre.org/api/cve/{}"
CVE_PAGE = "https://www.cve.org/CVERecord?id={}"
BZ_API = "https://bugzilla.mozilla.org/rest"
BZ_PAGE = "https://bugzilla.mozilla.org/show_bug.cgi?id={}"

CVE_ID = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.I)
GHSA_ID = re.compile(r"\bGHSA(?:-[a-z0-9]{4}){3}\b", re.I)
OSV_ID = re.compile(r"\b(?:PYSEC|GO|RUSTSEC|MAL|GSD)-\d{4}-[A-Za-z0-9]+\b", re.I)
BZ_BUG = re.compile(r"bugzilla\.mozilla\.org/show_bug\.cgi\?(?:[^#\s]*&)?id=(\d+)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A CA bug counts only when its summary names a compromise, a breach or a mis-issuance; compliance paperwork does not.
TRUST_WORDS = re.compile(r"\b(?:compromis\w*|private keys?|key generation|unauthori[sz]ed|breach\w*|mis-?issu\w*|"
                         r"distrust\w*|intrusion|attack\w*|stolen|leak\w*|exposed|weak keys?)\b", re.I)


def config() -> dict:
    return load_json(CONFIG, {})


def _since(days: int) -> dt.date:
    return dt.date.fromisoformat(edition_date()) - dt.timedelta(days=days)


def redact(text: str) -> str:
    """Email addresses in a document never reach the editor or the page."""
    return EMAIL.sub("[email]", text or "")


def norm_id(s: str) -> str:
    """One spelling per advisory id: CVE, MAL, PYSEC and the like upper case; GHSA keeps its lower-case body."""
    s = s.strip()
    if s.upper().startswith("GHSA-"):
        return "GHSA-" + s[5:].lower()
    return s.upper()


def ids_in(url: str | None) -> set[str]:
    """The identities a URL carries: its page key, a Mozilla bug number, and any advisory id written in it."""
    if not url:
        return set()
    out = {url_key(url)}
    m = BZ_BUG.search(url)
    if m:
        out.add(f"bz:{m.group(1)}")
    decoded = urllib.parse.unquote(url)
    for rx in (CVE_ID, GHSA_ID, OSV_ID):
        out |= {norm_id(x) for x in rx.findall(decoded)}
    return out


def _post_json(url: str, body: dict, timeout: int = 40) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- severity, as the source states it ----------

def rating(score: float | None) -> str | None:
    """CVSS qualitative rating, lower case: critical, high, medium, low (none below 0.1)."""
    if score is None:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low" if score >= 0.1 else None


def _roundup(x: float) -> float:
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


CVSS3 = {"AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
         "UI": {"N": 0.85, "R": 0.62}, "CIA": {"H": 0.56, "L": 0.22, "N": 0.0}}


def cvss3_score(vector: str) -> float | None:
    """The CVSS 3.0 or 3.1 base score of a vector string, by the specification's formula; None if it is not one."""
    if not vector or not vector.upper().startswith("CVSS:3"):
        return None
    try:
        m = dict(p.split(":", 1) for p in vector.split("/")[1:])
        scope = m["S"]
        pr = {"N": 0.85, "L": 0.68 if scope == "C" else 0.62, "H": 0.5 if scope == "C" else 0.27}[m["PR"]]
        iss = 1 - (1 - CVSS3["CIA"][m["C"]]) * (1 - CVSS3["CIA"][m["I"]]) * (1 - CVSS3["CIA"][m["A"]])
        impact = 6.42 * iss if scope == "U" else 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
        expl = 8.22 * CVSS3["AV"][m["AV"]] * CVSS3["AC"][m["AC"]] * pr * CVSS3["UI"][m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min(impact + expl, 10)) if scope == "U" else _roundup(min(1.08 * (impact + expl), 10))


GHSA_RATING = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}


def osv_severity(v: dict) -> tuple[str | None, str | None]:
    """(severity, basis) from an OSV record: the advisory's own rating first, then a CVSS 3 vector's base score."""
    r = GHSA_RATING.get(str((v.get("database_specific") or {}).get("severity") or "").upper())
    if r:
        return r, "advisory rating"
    for s in v.get("severity") or []:
        score = cvss3_score(s.get("score") or "")
        if score is not None:
            return rating(score), f"CVSS {score:g}"
    return None, None


def cve_record(cve: str) -> dict | None:
    """The CVE Program's own record (keyless); None when it cannot be read."""
    try:
        return json.loads(fetch(CVE_API.format(cve), timeout=20, retries=1))
    except Exception:
        return None


def cve_severity(rec: dict | None) -> tuple[str | None, str | None]:
    """(severity, basis) from a CVE record: the assigning authority's metrics, then CISA's added metrics."""
    if not rec:
        return None, None
    containers = (rec.get("containers") or {})
    for who, cont in [("CNA", containers.get("cna") or {})] + [("ADP", a) for a in containers.get("adp") or []]:
        for m in cont.get("metrics") or []:
            for key in ("cvssV4_0", "cvssV3_1", "cvssV3_0"):
                c = m.get(key) or {}
                sev = str(c.get("baseSeverity") or "").lower()
                if sev in ("critical", "high", "medium", "low"):
                    return sev, f"CVSS {c.get('baseScore')} ({who})" if c.get("baseScore") is not None else f"CVE record ({who})"
    return None, None


def cve_text(rec: dict | None) -> str:
    if not rec:
        return ""
    cna = (rec.get("containers") or {}).get("cna") or {}
    return " ".join(d.get("value", "") for d in cna.get("descriptions") or [] if d.get("lang", "en").startswith("en"))


# ---------- CISA KEV ----------

def kev(days: int | None = None) -> list[Item]:
    """Entries added to CISA's Known Exploited Vulnerabilities catalog in the last few days, for the AI and developer stack."""
    cfg = config()
    since = _since(days or cfg.get("window_days", 4)).isoformat()
    words = [w for w in cfg.get("kev_watch", []) if w]
    if not words:
        return []
    watch = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)
    data = json.loads(fetch(KEV_URL, timeout=40))
    picked = [v for v in data.get("vulnerabilities", []) if (v.get("dateAdded") or "") >= since and v.get("cveID")
              and watch.search(" ".join(str(v.get(k) or "") for k in ("vendorProject", "product", "vulnerabilityName", "shortDescription")))]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda v: cve_record(v["cveID"]), picked))
    out = []
    for v, rec in zip(picked, records):
        cve = norm_id(v["cveID"])
        sev, basis = cve_severity(rec)
        notes = [u.strip() for u in re.split(r"\s*;\s*", v.get("notes") or "") if u.strip().startswith("https://")]
        text = "\n".join(filter(None, [
            v.get("vulnerabilityName"), v.get("shortDescription"),
            f"Required action: {v.get('requiredAction')}" if v.get("requiredAction") else "",
            f"Date added: {v.get('dateAdded')}. Due date: {v.get('dueDate')}." if v.get("dueDate") else f"Date added: {v.get('dateAdded')}.",
            f"Known ransomware campaign use: {v.get('knownRansomwareCampaignUse')}." if v.get("knownRansomwareCampaignUse") else "",
            cve_text(rec)]))
        url = KEV_PAGE.format(cve)
        out.append(Item(
            source_id="cisa-kev", source_kind="security", source_name="CISA Known Exploited Vulnerabilities",
            url=url, title=strip_html(v.get("vulnerabilityName") or cve, 300), summary=strip_html(v.get("shortDescription"), 900),
            published_at=f"{v['dateAdded']}T12:00:00+00:00", author="CISA", trust=0.95, keys=[f"threat:{cve}"],
            extra={"origin": "kev", "ids": sorted({cve} | ids_in(url)), "cves": [cve], "kev": True, "exploited": True,
                   "source_severity": sev, "severity_basis": basis, "status_hint": "active", "category": "exploited-vuln",
                   "affects": [{"name": strip_html(f"{v.get('vendorProject', '')} {v.get('product', '')}".strip(), 120),
                                "ecosystem": "product", "versions": ""}],
                   "fixed_in": [], "published": v["dateAdded"], "doc": url, "text": redact(strip_html(text, 6000)),
                   "refs": notes[:6] + [CVE_PAGE.format(cve)]},
        ))
    return out


# ---------- OSV.dev ----------

def _ranges(aff: dict) -> tuple[str, list[str]]:
    """(affected versions as one string, fixed versions) from an OSV `affected` entry."""
    spans, fixed = [], []
    for r in aff.get("ranges") or []:
        if r.get("type") not in ("ECOSYSTEM", "SEMVER"):
            continue
        lo = None
        for ev in r.get("events") or []:
            if "introduced" in ev:
                lo = ev["introduced"]
            elif "fixed" in ev or "last_affected" in ev:
                hi = f"< {ev['fixed']}" if "fixed" in ev else f"<= {ev['last_affected']}"
                spans.append(hi if lo in (None, "0") else f">= {lo}, {hi}")
                if "fixed" in ev:
                    fixed.append(ev["fixed"])
                lo = None
        if lo is not None:
            spans.append("all versions" if lo == "0" else f">= {lo}")
    if not spans and aff.get("versions"):
        vs = aff["versions"]
        spans.append(", ".join(vs[:5]) + (f" and {len(vs) - 5} more" if len(vs) > 5 else ""))
    return "; ".join(dict.fromkeys(spans)), list(dict.fromkeys(fixed))


def _osv_item(v: dict, group: list[dict]) -> Item:
    vid = norm_id(v["id"])
    ids = {norm_id(x["id"]) for x in group} | {norm_id(a) for x in group for a in x.get("aliases") or []}
    cves = sorted(i for i in ids if i.startswith("CVE-"))
    sev, basis = osv_severity(v)
    for x in group:
        if not sev:
            sev, basis = osv_severity(x)
    affects, fixed_in = [], []
    for a in v.get("affected") or []:
        pkg = a.get("package") or {}
        if not pkg.get("name"):
            continue
        versions, fixed = _ranges(a)
        affects.append({"name": pkg["name"], "ecosystem": pkg.get("ecosystem") or "", "versions": versions})
        if fixed:
            fixed_in.append({"name": pkg["name"], "version": ", ".join(fixed)})
    details = re.sub(r"[#*`>|]+", " ", v.get("details") or "")
    text = redact(strip_html(f"{v.get('summary') or ''}\n{details}", 6000))
    url = OSV_PAGE.format(vid)
    refs = [r["url"] for r in v.get("references") or [] if str(r.get("url", "")).startswith("https://")]
    malicious = vid.startswith("MAL-")
    return Item(
        source_id="osv", source_kind="security", source_name="OSV.dev", url=url,
        title=strip_html(v.get("summary") or vid, 300), summary=strip_html(details, 900),
        published_at=v.get("published"), trust=0.9, keys=[f"threat:{vid}"],
        extra={"origin": "osv", "ids": sorted(ids | ids_in(url)), "cves": cves, "kev": False, "exploited": None,
               "source_severity": sev, "severity_basis": basis,
               "status_hint": None if malicious else ("patched" if fixed_in else "active"),
               "category": "supply-chain" if malicious else None, "withdrawn": bool(v.get("withdrawn")),
               "affects": affects[:6], "fixed_in": fixed_in[:8], "published": (v.get("published") or "")[:10] or None,
               "modified": v.get("modified"), "doc": url, "text": text, "refs": refs[:8]},
    )


PREFER = ("GHSA-", "MAL-", "PYSEC-", "GO-", "RUSTSEC-", "CVE-")


def osv(days: int | None = None, packages: list[dict] | None = None) -> list[Item]:
    """Advisories for the watched packages published, or changed, in the recent window. One record per vulnerability."""
    cfg = config()
    window = days or cfg.get("window_days", 4)
    since = dt.datetime.combine(_since(window), dt.time(), dt.timezone.utc)
    young = since - dt.timedelta(days=30)  # an old advisory that was only re-edited is not news
    pkgs = packages if packages is not None else cfg.get("osv_packages", [])
    if not pkgs:
        return []
    res = _post_json(OSV_BATCH, {"queries": [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]}} for p in pkgs]})
    ids = []
    for r in res.get("results", []):
        for v in r.get("vulns") or []:
            t = parse_time(v.get("modified"))
            if v.get("id") and t and t >= since and v["id"] not in ids:
                ids.append(v["id"])

    def one(vid):
        try:
            return json.loads(fetch(OSV_VULN.format(urllib.parse.quote(vid)), timeout=20, retries=1))
        except Exception:
            return None  # one advisory that cannot be read never loses the rest

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        records = [v for v in pool.map(one, ids[:80]) if v]
    keep = []
    for v in records:
        pub, mod = parse_time(v.get("published")), parse_time(v.get("modified"))
        if v.get("withdrawn") or not pub:
            continue
        if pub >= since or (mod and mod >= since and pub >= young):
            keep.append(v)
    # One vulnerability, many ids (GHSA, PYSEC, CVE): group by alias and lead with the best-described record.
    groups: list[list[dict]] = []
    for v in keep:
        names = {norm_id(v["id"])} | {norm_id(a) for a in v.get("aliases") or []}
        hit = next((g for g in groups if names & {n for x in g for n in [norm_id(x["id"]), *(norm_id(a) for a in x.get("aliases") or [])]}), None)
        if hit is not None:
            hit.append(v)
        else:
            groups.append([v])
    out = []
    for g in groups:
        g.sort(key=lambda x: next((i for i, p in enumerate(PREFER) if norm_id(x["id"]).startswith(p)), len(PREFER)))
        lead = g[0]
        item = _osv_item(lead, g)
        if not item.extra["source_severity"]:
            cve = next(iter(item.extra["cves"]), None)
            if cve:
                sev, basis = cve_severity(cve_record(cve))
                item.extra.update(source_severity=sev, severity_basis=basis)
        out.append(item)
    return out


# ---------- Mozilla CA incidents ----------

def _first_comment(bug: int) -> str:
    try:
        data = json.loads(fetch(f"{BZ_API}/bug/{bug}/comment", timeout=20, retries=1))
        comments = (data.get("bugs") or {}).get(str(bug), {}).get("comments") or []
        return comments[0].get("text", "") if comments else ""
    except Exception:
        return ""


def mozilla_ca(days: int | None = None) -> list[Item]:
    """CA incident bugs filed in Mozilla's root program in the last few days that name a compromise, breach or mis-issuance."""
    cfg = config()
    mc = cfg.get("mozilla_ca") or {"product": "CA Program", "component": "CA Certificate Compliance"}
    since = _since(days or cfg.get("window_days", 4)).isoformat()
    q = urllib.parse.urlencode({"product": mc["product"], "component": mc["component"], "creation_time": since,
                                "include_fields": "id,summary,creation_time,last_change_time,status,resolution"})
    bugs = [b for b in json.loads(fetch(f"{BZ_API}/bug?{q}", timeout=30)).get("bugs", [])
            if b.get("id") and TRUST_WORDS.search(b.get("summary") or "")]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        firsts = list(pool.map(lambda b: _first_comment(b["id"]), bugs))
    out = []
    for b, first in zip(bugs, firsts):
        url = BZ_PAGE.format(b["id"])
        resolved = b.get("status") in ("RESOLVED", "VERIFIED") and b.get("resolution") == "FIXED"
        text = redact(strip_html(f"{b['summary']}\n{first}", 6000))
        out.append(Item(
            source_id="mozilla-ca-incidents", source_kind="security", source_name="Mozilla CA incident reports (Bugzilla)",
            url=url, title=strip_html(b["summary"], 300), summary=redact(strip_html(first, 900)),
            published_at=b.get("creation_time"), trust=0.9, keys=[f"threat:bz:{b['id']}"],
            extra={"origin": "bugzilla", "ids": sorted(ids_in(url)), "cves": [], "kev": False, "exploited": None,
                   "source_severity": None, "severity_basis": None, "status_hint": "mitigated" if resolved else None,
                   "category": "trust-infrastructure", "affects": [], "fixed_in": [],
                   "published": (b.get("creation_time") or "")[:10] or None, "doc": url, "text": text, "refs": []},
        ))
    return out


# ---------- the inbox ----------

def threats_inbox(days: int = 14) -> list[Item]:
    """Items the owner or the Scout added to inbox/threats.json: a primary URL, an optional note, optional more primary URLs.

    The note is context for the editor, never a source: only the documents ground what the page says. An entry
    older than `days` (by its `added` date) is left to the register.
    """
    data = load_json(INBOX, {})
    cutoff = _since(days).isoformat()
    out = []
    for x in data.get("items", []):
        url = str(x.get("url") or "")
        if not url.startswith("https://") or (x.get("added") and str(x["added"]) < cutoff):
            continue
        also = []
        for a in x.get("also") or []:
            a = {"url": a} if isinstance(a, str) else a
            if isinstance(a, dict) and str(a.get("url") or "").startswith("https://"):
                also.append({"url": a["url"], "name": a.get("name") or urllib.parse.urlsplit(a["url"]).netloc,
                             "official": bool(a.get("official"))})
        ids = ids_in(url)
        for a in also:
            ids |= ids_in(a["url"])
        out.append(Item(
            source_id="threats-inbox", source_kind="security",
            source_name=x.get("source") or urllib.parse.urlsplit(url).netloc, url=url,
            title=strip_html(x.get("title") or urllib.parse.urlsplit(url).netloc, 300), summary="",
            published_at=None, author=x.get("by"), trust=0.85, keys=[f"threat:{url_key(url)}"],
            extra={"origin": "inbox", "ids": sorted(ids), "cves": sorted(i for i in ids if i.startswith("CVE-")),
                   "kev": False, "exploited": None, "source_severity": None, "severity_basis": None, "status_hint": None,
                   "category": None, "affects": [], "fixed_in": [], "published": None, "doc": url,
                   "official": bool(x.get("official")), "also": also[:4], "note": strip_html(x.get("note") or "", 600)},
        ))
    return out


JOBS = {"cisa-kev": kev, "osv": osv, "mozilla-ca-incidents": mozilla_ca, "threats-inbox": threats_inbox}


def collect_all() -> tuple[list[dict], list[dict]]:
    """Every security source, each on its own: (items, health)."""
    items, health = [], []
    for sid, fn in JOBS.items():
        try:
            got = [i.to_dict() for i in fn()]
            items += got
            health.append({"id": sid, "ok": True, "count": len(got)})
        except Exception as ex:  # one source down never loses the others
            health.append({"id": sid, "ok": False, "count": 0, "error": f"{type(ex).__name__}: {str(ex)[:160]}"})
    return items, health
