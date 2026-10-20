"""Small shared helpers: HTTP, time, text and URL canonicalization."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CENTRAL = ZoneInfo("America/Chicago")
UA = "Mozilla/5.0 (compatible; Magpie/0.1; +https://cameronsutcliff.com/magpie)"
LONG_DASH = chr(0x2014)
ELLIPSIS = chr(0x2026)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def edition_date() -> str:
    """The edition is named for the Central-time calendar day it is published on."""
    override = os.environ.get("MAGPIE_DATE")
    if override:
        return override
    return now_utc().astimezone(CENTRAL).date().isoformat()


def iso(t: dt.datetime | None) -> str | None:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        t = parsedate_to_datetime(s)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch(url: str, *, timeout: int = 25, headers: dict | None = None, retries: int = 2) -> bytes:
    h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404, 410):
                break
        except Exception as e:  # network errors, timeouts
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def fetch_json(url: str, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8"))


def github_headers() -> dict:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def clean(s: str) -> str:
    return s.replace(LONG_DASH, ", ")


def strip_html(s: str | None, limit: int | None = None) -> str:
    if not s:
        return ""
    s = html.unescape(TAG.sub(" ", s))
    s = clean(WS.sub(" ", s).strip())
    if limit and len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + ELLIPSIS
    return s


GH_REPO = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
HF_REPO = re.compile(r"huggingface\.co/(?!papers/|spaces/|datasets/|blog/|docs/|api/|learn/|chat/|posts/)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
HF_SPACE = re.compile(r"huggingface\.co/spaces/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/)(\d{4}\.\d{4,5})")
GH_SKIP_OWNERS = {"sponsors", "orgs", "topics", "trending", "features", "about", "login", "settings",
                  "marketplace", "apps", "collections", "search", "site", "readme", "enterprise", "pricing",
                  "customer-stories", "security", "team", "events", "github-copilot", "resources"}


def canonical_keys(url: str | None, text: str = "") -> list[str]:
    """Keys that identify one capability across sources: repo, model, space, paper."""
    keys: list[str] = []
    blob = f"{url or ''} {text}"
    for owner, repo in GH_REPO.findall(blob):
        repo = re.sub(r"(\.git|[.,;:)]+)$", "", repo)
        if owner.lower() in GH_SKIP_OWNERS or not repo or repo.lower() in {"issues", "pulls"}:
            continue
        keys.append(f"gh:{owner.lower()}/{repo.lower()}")
    for owner, name in HF_SPACE.findall(blob):
        keys.append(f"hfs:{owner.lower()}/{name.lower()}")
    for owner, name in HF_REPO.findall(blob):
        keys.append(f"hf:{owner.lower()}/{name.lower()}")
    for pid in ARXIV.findall(blob):
        keys.append(f"arxiv:{pid}")
    out, seen = [], set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def url_key(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return f"url:{host}{path}"


def short_hash(s: str, n: int = 10) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, indent=1, ensure_ascii=False)
    tmp.write_text(text + "\n")
    tmp.replace(path)
