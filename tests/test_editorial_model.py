import subprocess
from unittest.mock import Mock

import pytest

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
