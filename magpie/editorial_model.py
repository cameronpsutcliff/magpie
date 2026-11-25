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
    # Existing callers can opt into their original failure diagnostics.
    format_error: Callable[[str, Exception, str], str] | None = None


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


def _parse_agy_stream_json(raw: str) -> str:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict) and evt.get("event") == "result":
            res = evt.get("result") or {}
            if res.get("status") not in (None, "SUCCESS"):
                raise RuntimeError(f"agy stream status {res.get('status')}")
            return str(res.get("response", ""))
    raise RuntimeError("agy stream missing result event")


def _run_agy(prompt: str, timeout: int) -> str:
    exe = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    if not Path(exe).exists():
        raise FileNotFoundError("agy")
    model = os.environ.get("MAGPIE_AGY_MODEL", "gemini-4-neon-high").strip() or "gemini-4-neon-high"
    effort = os.environ.get("MAGPIE_AGY_EFFORT", "").strip()
    use_stdin = len(prompt.encode("utf-8")) > 100_000 or prompt.startswith("-")
    with tempfile.TemporaryDirectory() as tmp:
        if use_stdin:
            argv = [
                exe,
                "--disable-slash-commands",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--model",
                model,
                "--print-timeout",
                f"{timeout}s",
            ]
            if effort:
                argv.extend(["--effort", effort])
            argv.append("-p=")
            payload = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
            r = subprocess.run(argv, input=payload, cwd=tmp, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"agy rc {r.returncode}")
            return _parse_agy_stream_json(r.stdout)

        argv = [
            exe,
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--model",
            model,
            "--print-timeout",
            f"{timeout}s",
        ]
        if effort:
            argv.extend(["--effort", effort])
        argv.append(f"-p={prompt}")
        r = subprocess.run(argv, cwd=tmp, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"agy rc {r.returncode}")
    try:
        envelope = json.loads(r.stdout)
        if isinstance(envelope, dict) and "response" in envelope:
            if envelope.get("status") not in (None, "SUCCESS"):
                raise RuntimeError(f"agy status {envelope.get('status')}")
            return str(envelope["response"])
    except json.JSONDecodeError:
        pass
    return r.stdout


LANES = {"claude": _run_claude, "codex": _run_codex, "agy": _run_agy}


def legacy_error(lane: str, ex: Exception, raw: str) -> str:
    return f"{lane}: {str(ex)[:160]}"


def request_json(prompt: str, policy: Policy, accept: Callable[[dict], bool]) -> Outcome:
    """Try each configured lane in order. The predicate evaluates decoded objects."""
    errors = []
    for lane in configured_lanes() if policy.lanes is None else policy.lanes:
        if policy.format_error is None and LANES.get(lane) is None:
            errors.append("unknown lane")
            continue
        raw = ""
        parsing = False
        try:
            raw = LANES[lane](prompt, policy.timeout)
            parsing = True
            data = _extract_json(raw)
        except Exception as ex:
            if policy.format_error is not None:
                errors.append(policy.format_error(lane, ex, raw))
            elif isinstance(ex, json.JSONDecodeError) or (
                parsing and isinstance(ex, (AttributeError, TypeError, ValueError, RecursionError))
            ):
                errors.append(f"{lane}: invalid JSON")
            elif parsing:
                raise
            elif isinstance(ex, subprocess.TimeoutExpired):
                errors.append(f"{lane}: timed out")
            elif isinstance(ex, FileNotFoundError):
                errors.append(f"{lane}: unavailable")
            else:
                errors.append(f"{lane}: invocation failed")
            continue
        if accept(data):
            return Outcome(data, lane, errors)
        errors.append(f"{lane}: rejected response")
    return Outcome(None, None, errors)
