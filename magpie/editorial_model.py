"""Request an editorial JSON object from the first lane that supplies an acceptable answer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    timeout: int
    lanes: tuple[str, ...] | None = None


@dataclass
class Outcome:
    data: dict | None
    lane: str | None
    errors: list[str]


def configured_lanes() -> tuple[str, ...]:
    return tuple(lane.strip() for lane in os.environ.get("MAGPIE_LANES", "claude,codex").split(",")
                 if lane.strip())


def _extract_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _run_claude(prompt: str, timeout: int) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise FileNotFoundError("claude")
    argv = [exe, "--print", "--model", os.environ.get("MAGPIE_MODEL", "opus"), "--output-format", "json",
            "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    if os.environ.get("MAGPIE_SAFE_MODE", "1") == "1":
        argv.insert(1, "--safe-mode")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(argv, input=prompt, cwd=tmp, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude rc {r.returncode}")
    return json.loads(r.stdout).get("result", "")


def _run_codex(prompt: str, timeout: int) -> str:
    exe = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    if not Path(exe).exists():
        raise FileNotFoundError("codex")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "answer.md"
        r = subprocess.run([exe, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                            "-s", "read-only", "-o", str(out), "-"], input=prompt, cwd=tmp,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"codex rc {r.returncode}")
        return out.read_text()


LANES = {"claude": _run_claude, "codex": _run_codex}


def request_json(prompt: str, policy: Policy, accept: Callable[[dict], bool]) -> Outcome:
    """Try each configured lane in order. The predicate evaluates decoded objects."""
    errors = []
    for lane in configured_lanes() if policy.lanes is None else policy.lanes:
        adapter = LANES.get(lane)
        if adapter is None:
            errors.append("unknown lane")
            continue
        try:
            raw = adapter(prompt, policy.timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"{lane}: timed out")
            continue
        except FileNotFoundError:
            errors.append(f"{lane}: unavailable")
            continue
        except json.JSONDecodeError:
            errors.append(f"{lane}: invalid JSON")
            continue
        except Exception:
            errors.append(f"{lane}: invocation failed")
            continue
        try:
            data = _extract_json(raw)
        except (AttributeError, TypeError, ValueError, RecursionError):
            errors.append(f"{lane}: invalid JSON")
            continue
        if accept(data):
            return Outcome(data, lane, errors)
        errors.append(f"{lane}: rejected response")
    return Outcome(None, None, errors)
