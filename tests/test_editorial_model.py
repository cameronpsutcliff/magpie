import subprocess
from unittest.mock import Mock

import pytest

from magpie import constraints, followups, threats, viewing

from magpie import editorial_model as model


@pytest.fixture
def lanes(monkeypatch):
    first, second = Mock(), Mock(return_value='{"ready": true}')
    monkeypatch.setattr(model, "LANES", {"claude": first, "codex": second})
    monkeypatch.setenv("MAGPIE_LANES", " claude, ,codex")
    return first, second


def test_first_lane_extracts_object_and_skips_fallback(lanes):
    first, second = lanes
    first.return_value = 'Here is the object:\n```json\n{"ready": true}\n```'
    result = model.request_json("prompt", model.Policy(42), lambda data: data["ready"])
    assert result == model.Outcome({"ready": True}, "claude", [])
    first.assert_called_once_with("prompt", 42)
    second.assert_not_called()


@pytest.mark.parametrize(("error", "reason"), [
    (RuntimeError("private stderr"), "invocation failed"),
    (subprocess.TimeoutExpired(["private argv"], 42), "timed out"),
])
def test_falls_back_after_invocation_failure(lanes, error, reason):
    first, second = lanes
    first.side_effect = error
    result = model.request_json("prompt", model.Policy(42), lambda data: True)
    assert result == model.Outcome({"ready": True}, "codex", [f"claude: {reason}"])
    second.assert_called_once_with("prompt", 42)


@pytest.mark.parametrize("reply", ["no object", '{"ready":', None])
def test_falls_back_after_malformed_json(lanes, reply):
    first, _ = lanes
    first.return_value = reply
    result = model.request_json("prompt", model.Policy(42), lambda data: True)
    assert result == model.Outcome({"ready": True}, "codex", ["claude: invalid JSON"])


def test_falls_back_when_domain_rejects_response(lanes):
    first, _ = lanes
    first.return_value = '{"ready": false}'
    result = model.request_json("prompt", model.Policy(42), lambda data: data["ready"])
    assert result == model.Outcome({"ready": True}, "codex", ["claude: rejected response"])


def test_failure_receipt_omits_raw_diagnostics(lanes):
    first, second = lanes
    first.side_effect = RuntimeError("private stderr")
    second.return_value = '{"secret": "private output"'
    policy = model.Policy(42, ("private lane setting", "claude", "codex"))
    result = model.request_json("private prompt", policy, lambda data: True)
    assert result == model.Outcome(None, None, [
        "unknown lane", "claude: invocation failed", "codex: invalid JSON",
    ])


@pytest.mark.parametrize("caller", [constraints, threats])
def test_classifiers_accept_empty_response_in_configured_order(caller, lanes, monkeypatch):
    first, second = lanes
    second.return_value = "{}"
    monkeypatch.setenv("MAGPIE_LANES", "codex,claude")

    assert caller.classify([], "2026-11-20") == (
        {}, [], {"lane": "codex", "classified": 0, "errors": []},
    )
    second.assert_called_once()
    first.assert_not_called()


def test_viewing_keeps_fixed_order_and_failure_receipt(lanes, monkeypatch, tmp_path):
    first, second = lanes
    first.side_effect = RuntimeError("editor unavailable")
    second.return_value = "{}"
    monkeypatch.setenv("MAGPIE_LANES", "codex")
    candidates = [{"id": "clip", "title": "Clip", "channel": "Channel", "transcript": "Transcript"}]

    assert viewing.vet(candidates, tmp_path, timeout=42) == ({}, "codex", ["claude: editor unavailable"])
    first.assert_called_once()
    second.assert_called_once_with(first.call_args.args[0], 42)


def test_followups_uses_requested_order_and_requires_complete_rewrite(lanes, capsys):
    first, second = lanes
    second.return_value = '{"edition": {"headline": "New tools", "dek": "A quiet day"}}'
    first.return_value = (
        '{"edition": {"headline": "New tools", "dek": "A quiet day"},'
        ' "follow_ups": [{"id": "old", "headline": "Earlier work", "newly_possible": "Continue testing"}]}'
    )

    result = followups.rewrite(
        "2026-11-20", [], [{"id": "old", "key": "old", "title": "Earlier work"}],
        follow_ups=True, lanes=["codex", "claude"], timeout=42,
    )

    assert result == {
        "edition": {"headline": "New tools", "dek": "A quiet day", "quiet": False},
        "follow_ups": {"old": {"headline": "Earlier work", "newly_possible": "Continue testing"}},
        "lane": "claude",
    }
    second.assert_called_once()
    first.assert_called_once_with(second.call_args.args[0], 42)
    assert capsys.readouterr().err == ""
