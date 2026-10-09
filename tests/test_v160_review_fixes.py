"""Regression tests for the eight findings fixed for v1.6.0.

Each section names the defect it pins, so a future change that reintroduces it
fails with the reason attached rather than a bare assertion.

Nothing here reaches a network. The routes are patched, and the one test that
constructs a failing upstream builds the error object itself.
"""
import io
import json
import threading
import urllib.error
from unittest.mock import patch

import pytest

import doga.__init__ as plugin
from doga import response_contract


def _memory_call(monkeypatch, response_text):
    """Run the transform hook with memory capture, return the recorded goal."""
    recorded = {}
    monkeypatch.setattr(plugin, "MNEMOSYNE_AVAILABLE", True)

    def _remember(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(plugin, "remember", _remember, raising=False)
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", True)
    monkeypatch.setattr(plugin._state, "_current_user_message", "how do I fix this")
    plugin._on_transform_llm_output(response_text=response_text)
    return recorded.get("metadata", {}).get("goal_type")


# ---------------------------------------------------------------------------
# 1. The goal regex matched the taxonomy word anywhere in the cleaned text.
# ---------------------------------------------------------------------------

def test_a_taxonomy_word_outside_a_world_model_block_is_not_a_goal(monkeypatch):
    """The word in ordinary prose is not the model's stated goal.

    The previous pattern searched ``<world_model>.*?(Information|...)`` across
    the whole cleaned response, so a word in the final answer — or in an echoed
    question — was recorded as the goal and fed back into later turns.
    """
    text = (
        "Here is the Action plan. It needs Information about the budget.\n"
        "<world_model>Assessing the request.</world_model>\n"
        "The Understanding here matters."
    )
    assert _memory_call(monkeypatch, text) == "unknown"


def test_a_goal_statement_inside_the_world_model_block_is_recorded(monkeypatch):
    text = (
        "<world_model>The user is stuck on a decision.\n"
        "Goal: Action\n"
        "Stakes: low</world_model>\n"
        "Here is the Information you asked for."
    )
    assert _memory_call(monkeypatch, text) == "action"


def test_the_injected_guidance_block_never_supplies_the_goal(monkeypatch):
    """The guide is stripped first, so its own taxonomy lines cannot be read."""
    text = (
        "\n\n[world_model_guide]\n"
        "- **Information**: factual data.\n"
        "- **Understanding**: feel heard.\n"
        "- **Action**: a decision.\n"
        "[/world_model_guide]\n"
        "<world_model>Weighing options.</world_model>\n"
        "The answer is 42."
    )
    assert _memory_call(monkeypatch, text) == "unknown"


def test_an_unclosed_world_model_block_records_no_goal(monkeypatch):
    """A truncated block is not a place to read a goal out of.

    The formatter's unclosed-tag fallback keeps the text visible but is not a
    block, so nothing is attributed here either.
    """
    assert _memory_call(monkeypatch, "<world_model>Goal: Information") == "unknown"


def test_a_bare_taxonomy_word_alone_is_not_a_goal_statement(monkeypatch):
    """A word is a statement only when the model says it is the goal."""
    text = "<world_model>The Information and Understanding here are both real.</world_model>"
    assert _memory_call(monkeypatch, text) == "unknown"


# ---------------------------------------------------------------------------
# 2. The schema declared a minimum of 100 and nothing enforced it.
# ---------------------------------------------------------------------------

_SCENARIOS = [{"name": "ok", "variables": {"x": 0.5}}]


def test_a_single_iteration_is_refused_rather_than_answered():
    """One sample reports probability 1.0 for whatever that one sample picked."""
    result = json.loads(plugin._simulate_tool_handler({"scenarios": _SCENARIOS, "n_iterations": 1}))
    assert result["error"]
    assert "100" in result["error"]
    assert "n_iterations" in result["error"]


def test_a_negative_iteration_count_is_refused():
    result = json.loads(plugin._simulate_tool_handler({"scenarios": _SCENARIOS, "n_iterations": -5}))
    assert result["error"]
    assert "n_iterations" in result["error"]


@pytest.mark.parametrize("n", [0, 99, -1, -50000])
def test_every_value_below_the_declared_minimum_is_refused(n):
    result = json.loads(plugin._simulate_tool_handler({"scenarios": _SCENARIOS, "n_iterations": n}))
    assert "error" in result


@pytest.mark.parametrize("n", ["100", 100.9, True, None, [100]])
def test_a_non_integer_iteration_count_is_refused(n):
    result = json.loads(plugin._simulate_tool_handler({"scenarios": _SCENARIOS, "n_iterations": n}))
    assert result.get("error")


def test_the_declared_minimum_is_accepted():
    result = json.loads(plugin._simulate_tool_handler({"scenarios": _SCENARIOS, "n_iterations": 100}))
    assert "error" not in result
    assert result["summary"]["total_iterations"] == 100


def test_a_value_above_the_declared_maximum_is_clamped_not_refused():
    """A bigger request is the same request done more expensively, so it caps."""
    result = json.loads(plugin._simulate_tool_handler({"scenarios": _SCENARIOS, "n_iterations": 999999}))
    assert "error" not in result
    assert result["summary"]["total_iterations"] == 50000


# ---------------------------------------------------------------------------
# 3. A validation rejection fell through to a second billed call.
# ---------------------------------------------------------------------------

def test_a_rejected_answer_does_not_buy_a_second_paid_call(monkeypatch):
    """A 200 with unusable choices is already a completed, billed call.

    The old ``except Exception`` treated it as a transport failure and sent the
    same user payload to TypeSafe, paying twice for the same request.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")

    with patch.object(
        response_contract, "_request_openrouter",
        side_effect=response_contract.AnswerValidationError("invalid Jev response: unknown choice for 'goal'"),
    ), patch.object(
        response_contract, "_request_typesafe", return_value={"answers": {}},
    ) as typesafe:
        with pytest.raises(response_contract.AnswerValidationError):
            response_contract._request_jev({"user_request": "private"}, response_contract.QUESTIONS)

    typesafe.assert_not_called()


def test_a_transport_failure_still_reaches_the_second_route(monkeypatch):
    """The fallback exists for failures, and only for failures."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")

    with patch.object(
        response_contract, "_request_openrouter", side_effect=RuntimeError("connection refused"),
    ), patch.object(
        response_contract, "_request_typesafe", return_value={"answers": {}},
    ) as typesafe:
        result = response_contract._request_jev({"user_request": "private"}, response_contract.QUESTIONS)

    typesafe.assert_called_once()
    assert result == {"answers": {}}


def test_a_validation_rejection_escalates_out_of_the_hook(monkeypatch):
    """The hook must report the failure, not route around it."""
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    monkeypatch.setattr(plugin._state, "jev_fallback", False)
    monkeypatch.setattr(plugin._state, "_mode", "api_only")
    monkeypatch.setattr(plugin._state, "_mode_valid", True)
    monkeypatch.setattr(plugin._state, "_hosted", "jev")
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    with patch.object(
        response_contract, "_request_openrouter",
        side_effect=response_contract.AnswerValidationError("invalid Jev response"),
    ):
        plugin._on_pre_llm_call(user_message="should I take the offer?")

    assert plugin._state._last_jev_status == "error"


# ---------------------------------------------------------------------------
# 4. The TypeSafe error message interpolated the raw upstream body.
# ---------------------------------------------------------------------------

def test_a_typesafe_http_error_leaks_neither_the_body_nor_its_contents(monkeypatch, caplog):
    secret = "s3cret-dsn-value"

    def _fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone", 503, "unavailable", None,
            io.BytesIO(b'{"detail": "' + secret.encode() + b'"}'),
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")
    monkeypatch.setattr(response_contract.urllib.request, "urlopen", _fail)

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError) as excinfo:
            response_contract._request_typesafe({"user_request": "private"}, response_contract.QUESTIONS)

    assert "HTTP 503" in str(excinfo.value)
    assert secret not in str(excinfo.value)
    assert secret not in caplog.text


def test_a_typesafe_failure_does_not_leak_the_upstream_body_into_the_jev_chain(monkeypatch, caplog):
    """The same body must not reappear through the fallback's combined error."""
    secret = "s3cret-dsn-value"

    def _fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone", 500, "boom", None,
            io.BytesIO(b'{"detail": "' + secret.encode() + b'"}'),
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setattr(response_contract.urllib.request, "urlopen", _fail)

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError) as excinfo:
            response_contract._request_jev({"user_request": "private"}, response_contract.QUESTIONS)

    assert secret not in str(excinfo.value)
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 5. The settings singleton was written without a lock and keyed status by one slot.
# ---------------------------------------------------------------------------

def test_a_settings_write_is_committed_under_the_plugin_lock():
    """Two fields land as one locked change, not one field at a time."""
    state = plugin._PluginState()
    observed = []

    def _probe(self):
        return observed

    def _probe_set(self, value):
        observed.append(self._write_lock.locked())

    type(state).probe = property(_probe, _probe_set)
    try:
        state.set(probe="written", depth=5)
    finally:
        del type(state).probe
    assert observed == [True]


def test_a_thread_reports_its_own_contract_outcome():
    """One session's write cannot become another session's status."""
    state = plugin._PluginState()
    state._reset_turn_status()
    results = {}

    def _session(name, status):
        state._last_jev_status = status
        results[name] = state._last_jev_status

    a = threading.Thread(target=_session, args=("a", "ok"))
    b = threading.Thread(target=_session, args=("b", "error"))
    a.start(); a.join()
    b.start(); b.join()

    # Each thread read back its own write, and neither observed the other's.
    assert results == {"a": "ok", "b": "error"}

    # This thread ran a turn of its own before the two started, and reports
    # that turn's value rather than whichever of them wrote last.
    assert state._last_jev_status == "enabled"


def test_a_thread_with_no_turn_of_its_own_reports_the_last_attempt():
    """A status request that carries no turn reports the latest attempt."""
    state = plugin._PluginState()
    state._last_jev_status = "error"
    seen = []

    def _status():
        seen.append(state._last_jev_status)

    reader = threading.Thread(target=_status)
    reader.start(); reader.join()
    assert seen == ["error"]


def test_a_new_turn_does_not_report_the_previous_turns_outcome(monkeypatch):
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(plugin._state, "jev_enabled", False)
    plugin._state._last_jev_status = "error"

    plugin._on_pre_llm_call(user_message="a fresh turn", is_first_turn=True)

    assert plugin._state._last_jev_status == "enabled"


# ---------------------------------------------------------------------------
# 6. Raising max_recursion did not clear the stop latch it had already tripped.
# ---------------------------------------------------------------------------

def test_raising_the_recursion_limit_clears_the_stop_latch():
    """A raised limit has to take effect in the turn it was raised in."""
    plugin._state.max_recursion = 1
    plugin._state._recursion_depth = 0
    plugin._state._reasoning_stack = []
    plugin._on_post_tool_call(tool_name="reason_deeper", args={"focus": "a"})
    plugin._reason_deeper_handler({"focus": "b"})
    assert plugin._state._stop_sent is True

    message = plugin._handle_doga("max_recursion 5")
    assert "5" in message
    assert plugin._state._stop_sent is False
    assert plugin._state._stop_count == 0

    result = json.loads(plugin._reason_deeper_handler({"focus": "c"}))
    assert result.get("continue") is True


def test_the_mode_command_reports_the_resolved_provider():
    """``api_only`` is silent about the vendor, so the vendor is named."""
    message = plugin._handle_doga("provider clef")
    assert "api_only" in message
    assert "clef" in message
    assert plugin._state.decision_provider == "clef"


def test_a_mode_with_no_hosted_provider_clears_the_pin():
    """A pin from an API-led selection must not survive into a local mode.

    Left in place it came back silently on the next API-led selection, which is
    a route to a second paid vendor that no current mode asked for.
    """
    plugin._handle_doga("provider clef")
    assert plugin._state._hosted == "clef"

    plugin._handle_doga("mode local_only")
    assert plugin._state.decision_mode == "local_only"
    assert plugin._state.decision_provider == "laya"
    assert plugin._state._hosted == response_contract.DEFAULT_HOSTED

    plugin._handle_doga("mode api_only")
    assert plugin._state.decision_provider == response_contract.DEFAULT_HOSTED


def test_status_names_the_resolved_provider():
    plugin._handle_doga("provider clef")
    status = plugin._handle_doga("status")
    assert "provider: clef" in status


# ---------------------------------------------------------------------------
# 7. hard_break is advisory, and is documented as such.
# ---------------------------------------------------------------------------

def test_the_hard_break_wording_is_advisory():
    """The wording cannot close the loop; only the gates and Hermes can.

    ``_stop_sent`` gates the tools from the first ignored stop, which is the
    real backstop. ``hard_break`` strengthens the wording on the third ignored
    stop and does nothing else, which is what the docstring must say.
    """
    doc = plugin._reason_deeper_handler.__doc__
    assert "advisory" in doc
    assert "max_iterations" in doc
