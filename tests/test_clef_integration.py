"""Clef via Cloudflare Workers AI must behave as a distinct third route.

Clef is a hosted provider, so the tests here never assert that a request stays
local. What they do assert is that selecting Clef sends the request to Cloudflare
and to nothing else, that a Clef failure never silently reaches Jev or Laya, and
that Clef's typed answers map onto the same response contract the other routes
build.
"""
import copy
import json
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from doga import response_contract
import doga.__init__ as plugin


CLEF_ANSWERS = {
    "goal": {"type": "choice", "choice": "action", "probabilities": {"action": 0.8, "information": 0.1, "understanding": 0.1}, "confidence": 0.7},
    "mode": {"type": "choice", "choice": "recommend", "probabilities": {"recommend": 0.7, "answer": 0.2, "explain": 0.05, "clarify": 0.05}, "confidence": 0.6},
    "stakes": {"type": "choice", "choice": "medium", "probabilities": {"medium": 0.8}, "confidence": 0.7},
    "clarification": {"type": "noul", "noul": 0.85},
    "scenario_need": {"type": "choice", "choice": "compare_options", "probabilities": {"compare_options": 0.6}, "confidence": 0.5},
}


def _raising(message):
    """A replacement callable that always raises, for monkeypatch targets."""
    def _call(*args, **kwargs):
        raise RuntimeError(message)
    return _call


def _bare_response():
    # A fresh copy per call, so a test that corrupts one answer cannot leak that
    # corruption into the next test through the shared module level answers.
    return {
        "model": "clef",
        "answers": copy.deepcopy(CLEF_ANSWERS),
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def _envelope_response():
    return {"success": True, "errors": [], "messages": [], "result": _bare_response()}


def _error_response():
    return {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}], "messages": [], "result": None}


@pytest.fixture(autouse=True)
def _no_accidental_network(monkeypatch):
    """Fail loudly if any provider tries a real socket call."""
    def _blocked(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


def test_clef_is_a_valid_provider_name():
    assert "clef" in response_contract.PROVIDERS


def test_clef_mode_is_selectable_and_reported(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    # clef_api is a kept-working alias. It selects the mode and is reported
    # canonically as api_only, because an alias must not reach observable output.
    message = plugin._handle_doga("mode clef_api")
    assert "api_only" in message
    assert "clef_api" not in message
    assert plugin._state.decision_provider == "clef"
    assert plugin._state.jev_fallback is False
    assert plugin._state.decision_mode == "api_only"
    assert "api_only" in plugin._handle_doga("status")
    assert "api_only" in plugin._handle_doga("help")


def test_clef_sends_only_to_cloudflare(monkeypatch):
    """Cloudflare sees the state; Jev and Laya see nothing."""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        seen["auth"] = request.headers.get("Authorization")
        return _Response(_bare_response())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("Jev was called")):
        result = response_contract.evaluate_contract("Recommend one path", provider="clef")
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acct-123/ai/run/@cf/cloudflare/clef"
    assert seen["body"]["model"] == "clef"
    assert seen["body"]["state"] == {"user_request": "Recommend one path"}
    assert set(seen["body"]["questions"]) == set(response_contract.QUESTIONS)
    assert seen["auth"] == "Bearer cf-token-value"
    assert result["answers"]["clarification"]["noul"] == 0.85


def test_clef_flash_checkpoint_routes_to_flash_endpoint(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        return _Response(_bare_response())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    monkeypatch.setenv("DOGA_CLEF_MODEL", "clef-flash")
    result = response_contract.evaluate_contract("Recommend one path", provider="clef")
    assert seen["url"].endswith("/ai/run/@cf/cloudflare/clef-flash")
    assert seen["body"]["model"] == "clef-flash"
    assert result["model"] == "clef"


def test_clef_reads_cloudflare_rest_envelope(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(_envelope_response()))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    result = response_contract.evaluate_contract("Recommend one path", provider="clef")
    assert result["answers"] == CLEF_ANSWERS


def test_clef_rejects_cloudflare_error_envelope(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(_error_response()))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    with pytest.raises(RuntimeError, match="10000"):
        response_contract.evaluate_contract("private message", provider="clef")


def test_clef_rejects_unknown_choice(monkeypatch):
    broken = _bare_response()
    broken["answers"]["mode"] = {"type": "choice", "choice": "invent_mode", "probabilities": {}, "confidence": 0.4}
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(broken))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    with pytest.raises(RuntimeError, match="unknown choice"):
        response_contract.evaluate_contract("private message", provider="clef")


def test_clef_rejects_out_of_range_noul_probability(monkeypatch):
    broken = _bare_response()
    broken["answers"]["clarification"] = {"type": "noul", "noul": 1.4}
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(broken))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    with pytest.raises(RuntimeError, match="probability"):
        response_contract.evaluate_contract("private message", provider="clef")


def test_missing_cloudflare_token_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    with pytest.raises(RuntimeError, match="CLOUDFLARE_API_TOKEN"):
        response_contract.evaluate_contract("private message", provider="clef")


def test_missing_cloudflare_account_fails_before_any_request(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    with pytest.raises(RuntimeError, match="CLOUDFLARE_ACCOUNT_ID"):
        response_contract.evaluate_contract("private message", provider="clef")


def test_clef_failure_never_reaches_jev_or_laya(monkeypatch):
    """A hosted Clef failure keeps DOGA on ordinary guidance, with no other call."""
    monkeypatch.setattr(plugin._state, "decision_provider", "clef")
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    with patch.object(response_contract, "_request_laya", side_effect=AssertionError("Laya was called")), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("Jev was called")):
        result = plugin._on_pre_llm_call(user_message="private request")
    assert "[DOGA response contract]" not in result["context"]
    assert result["context"]
    assert plugin._state._last_jev_status == "error"
    assert "api_only" in plugin._handle_doga("status")


def test_clef_builds_the_same_contract_shape(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(_bare_response()))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    judged = response_contract.evaluate_contract("Recommend one path", provider="clef")
    contract = response_contract.build_contract(judged)
    assert contract["goal"] == "action"
    assert contract["mode"] == "recommend"
    assert contract["stakes"] == "medium"
    assert contract["scenario_need"] == "compare_options"
    # High ambiguity with a non-clarify mode keeps the mode and requires a
    # conditional answer rather than a clarifying question.
    assert contract["ask_clarifying_question"] is False
    assert contract["conditional_response"] is True
    rendered = response_contract.render_contract(contract)
    assert "[DOGA response contract]" in rendered
    assert "make the answer conditional" in rendered


def test_clef_hook_injects_the_contract(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "clef")
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(_bare_response()))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    result = plugin._on_pre_llm_call(user_message="Recommend one path")
    assert "[DOGA response contract]" in result["context"]
    assert "make the answer conditional" in result["context"]
    assert plugin._state._last_jev_status == "ok"


def test_clef_http_error_surfaces_status(monkeypatch):
    def _raise(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    with pytest.raises(RuntimeError, match="403"):
        response_contract.evaluate_contract("private message", provider="clef")


def test_clef_credentials_never_appear_in_the_log(monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response(_error_response()))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-secret-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-secret")
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            response_contract.evaluate_contract("private message text", provider="clef")
    assert "cf-secret-token-value" not in caplog.text
    assert "acct-secret" not in caplog.text
    assert "private message text" not in caplog.text


def test_clef_legacy_provider_alias_still_works(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    message = plugin._handle_doga("provider clef")
    assert "api_only" in message
    assert plugin._state.decision_provider == "clef"


def test_invalid_mode_is_rejected_without_changing_state(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "clef")
    message = plugin._handle_doga("mode something-else")
    assert "api_only" in message
    assert plugin._state.decision_provider == "clef"


def test_startup_mode_selects_clef(monkeypatch):
    monkeypatch.setenv("DOGA_DECISION_MODE", "clef_api")
    monkeypatch.delenv("DOGA_DECISION_PROVIDER", raising=False)
    monkeypatch.delenv("DOGA_LAYA_JEV_FALLBACK", raising=False)
    state = plugin._PluginState()
    assert state.decision_provider == "clef"
    # The alias is accepted and reported as the canonical name it denotes.
    assert state.decision_mode == "api_only"


def test_clef_is_never_implicitly_fallback_for_laya(monkeypatch):
    """Clef is not wired into the Laya error fallback; that stays Jev only."""
    monkeypatch.setattr(plugin._state, "decision_provider", "laya")
    monkeypatch.setattr(plugin._state, "jev_fallback", True)
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token-value")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-123")
    monkeypatch.setattr(response_contract, "_request_laya", _raising("local failure"))
    with patch.object(response_contract, "_request_clef", side_effect=AssertionError("Clef was called")) as clef:
        with patch.object(response_contract, "_request_jev", return_value=_bare_response()):
            response_contract.evaluate_contract("public test request", provider="laya", fallback_to_jev=True)
    clef.assert_not_called()


class _Response:
    """Minimal urlopen context manager returning a JSON body."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload
