"""Deterministic Safe Repo-Scout for Magpie (<= 5 repos/day, strictly read-only).

Queries the public GitHub Search API for recently active AI/agent/developer
repositories, inspects README and package manifests via read-only HTTP calls
(never clones or executes code), runs a deterministic static red-flag scanner,
and emits a bounded daily digest of at most 5 candidates.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MAX_REPOS_PER_DAY = 5
DEFAULT_QUERIES = (
    "topic:ai-agents stars:>50 archived:false",
    "topic:mcp stars:>50 archived:false",
)

HIGH_RISK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("piped-remote-shell-install", re.compile(r"\b(curl|wget)\b[^\n|]{0,160}\|\s*(ba)?sh\b", re.I)),
    ("base64-shell-decode", re.compile(r"base64\s+(-d|--decode)\s*\|\s*(ba)?sh", re.I)),
    ("js-eval-atob", re.compile(r"\beval\s*\(\s*atob\s*\(", re.I)),
    ("powershell-encoded-cmd", re.compile(r"powershell[^\n]{0,60}-(enc|encodedcommand)\b", re.I)),
    ("npm-lifecycle-install-hook", re.compile(r'"(preinstall|postinstall)"\s*:\s*"[^"]*(curl|wget|node\s+-e|bash|sh\s+-c)', re.I)),
]

WARN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sudo-in-readme", re.compile(r"\bsudo\s+(chmod|chown|rm|bash|sh)\b", re.I)),
    ("credential-env-harvest", re.compile(r"\b(AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)\b[^\n]{0,60}(curl|fetch|requests\.post)", re.I)),
]

RESTRICTIVE_LICENSES = {"AGPL-3.0", "SSPL-1.0", "BSL-1.1", "NOASSERTION", "OTHER"}


def scan_repo_red_flags(
    meta: dict[str, Any],
    readme_text: str = "",
    manifest_text: str = "",
) -> dict[str, Any]:
    """Deterministically inspect repo metadata, README, and manifest without executing code."""
    high_flags: list[str] = []
    warnings: list[str] = []

    combined = f"{readme_text}\n{manifest_text}"
    for label, rx in HIGH_RISK_PATTERNS:
        if rx.search(combined):
            high_flags.append(label)

    for label, rx in WARN_PATTERNS:
        if rx.search(combined):
            warnings.append(label)

    if meta.get("archived"):
        warnings.append("repo-archived")

    stars = int(meta.get("stargazers_count") or 0)
    if stars < 25:
        warnings.append("low-star-count")

    lic_obj = meta.get("license") or {}
    spdx = (lic_obj.get("spdx_id") if isinstance(lic_obj, dict) else str(lic_obj or "")) or "NONE"
    if spdx == "NONE":
        warnings.append("missing-license")
    elif spdx in RESTRICTIVE_LICENSES:
        warnings.append(f"restrictive-license:{spdx}")

    if high_flags:
        verdict = "REJECT_RED_FLAG"
    elif warnings:
        verdict = "CAUTION"
    else:
        verdict = "SAFE_REVIEW"

    return {
        "verdict": verdict,
        "high_flags": sorted(set(high_flags)),
        "warnings": sorted(set(warnings)),
        "license": spdx,
        "stars": stars,
    }


def _gh_get_json(url: str, token: str | None = None, timeout: int = 12) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "magpie-safe-repo-scout/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_readme_and_manifest(full_name: str, token: str | None = None) -> tuple[str, str]:
    readme = ""
    manifest = ""
    try:
        r = _gh_get_json(f"https://api.github.com/repos/{full_name}/readme", token=token)
        if r.get("content"):
            readme = base64.b64decode(r["content"]).decode("utf-8", errors="ignore")[:12000]
    except Exception:
        pass
    for fname in ("package.json", "pyproject.toml"):
        try:
            m = _gh_get_json(f"https://api.github.com/repos/{full_name}/contents/{fname}", token=token)
            if m.get("content"):
                manifest = base64.b64decode(m["content"]).decode("utf-8", errors="ignore")[:8000]
                break
        except Exception:
            continue
    return readme, manifest


def scout_trending_repos(
    *,
    max_repos: int = MAX_REPOS_PER_DAY,
    queries: tuple[str, ...] = DEFAULT_QUERIES,
    lookback_days: int = 14,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch up to `max_repos` (capped at 5) trending repos via read-only GitHub API."""
    cap = max(1, min(int(max_repos), MAX_REPOS_PER_DAY))
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []

    for q in queries:
        if len(candidates) >= cap:
            break
        full_q = f"{q} pushed:>={since}"
        url = (
            "https://api.github.com/search/repositories?"
            + urllib.parse.urlencode({"q": full_q, "sort": "stars", "order": "desc", "per_page": cap})
        )
        try:
            data = _gh_get_json(url, token=token)
        except Exception:
            continue
        for item in data.get("items") or []:
            full_name = item.get("full_name")
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            readme, manifest = _fetch_readme_and_manifest(full_name, token=token)
            scan = scan_repo_red_flags(item, readme, manifest)
            candidates.append({
                "full_name": full_name,
                "url": item.get("html_url"),
                "description": (item.get("description") or "").replace("\u2014", " - ").strip(),
                "language": item.get("language") or "Unknown",
                "pushed_at": item.get("pushed_at"),
                **scan,
            })
            if len(candidates) >= cap:
                break

    return candidates[:cap]


def write_scout_report(results: list[dict[str, Any]], repo_root: Path) -> tuple[Path, Path]:
    today = dt.date.today().isoformat()
    state_dir = repo_root / "state" / "repo-scout"
    reports_dir = repo_root / "reports"
    state_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = state_dir / f"{today}.json"
    md_path = reports_dir / f"repo-scout-{today}.md"
    json_path.write_text(json.dumps({"date": today, "cap": MAX_REPOS_PER_DAY, "repos": results}, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# Magpie Safe Repo-Scout ({today})",
        "",
        f"Read-only GitHub API discovery (hard cap: {MAX_REPOS_PER_DAY} repos/day, zero cloning/execution).",
        "",
        "| Repo | Stars | License | Verdict | Flags |",
        "|---|---:|---|---|---|",
    ]
    for r in results:
        flags = ", ".join(r.get("high_flags", []) + r.get("warnings", [])) or "None"
        lines.append(f"| [{r['full_name']}]({r['url']}) | {r['stars']} | {r['license']} | `{r['verdict']}` | {flags} |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


if __name__ == "__main__":
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    root = Path(__file__).resolve().parent.parent
    repos = scout_trending_repos(token=tok)
    jp, mp = write_scout_report(repos, root)
    print(json.dumps({"ok": True, "count": len(repos), "json": str(jp), "report": str(mp)}))
