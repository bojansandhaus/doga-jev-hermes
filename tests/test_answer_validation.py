"""One validator must guard every route, and every value must be in range.

These cover three defects in the response contract:

- the shipped default route validated nothing, while the other two routes
  rejected the very same malformed payload;
- the hosted-led chain retried a failing local engine forever, because the
  documented cooldown was wired to the local-led direction only;
- build_contract trusted an out-of-range noul score.
"""
import urllib.request
from unittest.mock import patch

import pytest

from doga import response_contract

# ---------------------------------------------------------------------------
# Helpers: one garbage payload, fed to every route, so the routes can be
# compared against each other rather than each getting its own fixture.
# ---------------------------------------------------------------------------

def _garbage_answers():
    """Well-formed answers dict whose typed values are all wrong.

    Every choice is outside its own criteria and the probability is a string, so
    a route that checks nothing at all accepts this payload.
    """
    return {
        "goal": {"type": "choice", "choice": "NONSENSE"},
        "mode": {"type": "choice", "choice": 99},
        "stakes": {"type": "choice", "choice": "gibberish"},
        "clarification": {"type": "noul", "noul": "banana"},
        "scenario_need": {"type": "choice", "choice": 7},
    }


def _good_answers():
    return {
        "goal": {"type": "choice", "choice": "action"},
        "mode": {"type": "choice", "choice": "recommend"},
        "stakes": {"type": "choice", "choice": "medium"},
        "clarification": {"type": "noul", "noul": 0.1},
        "scenario_need": {"type": "choice", "choice": "none"},
    }


class _Response:
    """Minimal urlopen context manager returning a JSON body."""

    def __init__(self, payload):
        import json
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_failure_counter(monkeypatch):
    """Start every test from a clean local-failure counter."""
    monkeypatch.setattr(response_contract, "_laya_failure_count", 0, raising=False)


# ---------------------------------------------------------------------------
# Defect 1: the default route validated nothing.
# ---------------------------------------------------------------------------

def test_typesafe_route_rejects_the_same_garbage_the_other_routes_reject(monkeypatch):
    """Defect 1: Jev is the shipped default and used to accept garbage."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *args, **kwargs: _Response({"answers": _garbage_answers()}),
    )
    with pytest.raises(RuntimeError, match="invalid Jev response"):
        response_contract._request_typesafe({"user_request": "x"}, response_contract.QUESTIONS)


def test_openrouter_route_rejects_the_same_garbage_the_other_routes_reject():
    """Defect 1: the OpenRouter path carried the identical hole."""
    with patch.object(
        urllib.request, "urlopen",
        lambda *args, **kwargs: _Response({"answers": _garbage_answers()}),
    ), pytest.raises(RuntimeError, match="invalid Jev response"):
        response_contract._request_openrouter({"user_request": "x"}, response_contract.QUESTIONS, api_key="k")


def test_every_route_rejects_one_identical_garbage_payload(monkeypatch):
    """The three routes agree, which is the point of the shared validator."""
    payload = {"answers": _garbage_answers()}

    with pytest.raises(RuntimeError, match="invalid Jev response"):
        response_contract._validate_typed_answers(payload, response_contract.QUESTIONS, "Jev")
    with pytest.raises(RuntimeError, match="invalid Clef response"):
        response_contract._clef_result(payload)
    with pytest.raises(RuntimeError, match="invalid local Laya response"):
        response_contract._validate_typed_answers(payload, response_contract.QUESTIONS, "local Laya")


def test_jev_route_accepts_a_well_formed_payload():
    """The fix rejects malformed answers, not all answers."""
    data = {"answers": _good_answers()}
    assert response_contract._validate_typed_answers(
        data, response_contract.QUESTIONS, "Jev") == data


def test_validator_iterates_the_question_set_the_caller_passed():
    """A partial question set cannot be validated against a global instead.

    This is the divergence that caused the defect: one route iterated its own
    ``questions`` argument while another reached for the module global.
    """
    partial = {"goal": response_contract.QUESTIONS["goal"]}
    # 'goal' is checked, and the other four questions are not demanded.
    assert response_contract._validate_typed_answers(
        {"answers": {"goal": {"choice": "action"}}}, partial, "Jev")
    with pytest.raises(RuntimeError, match="unknown choice"):
        response_contract._validate_typed_answers(
            {"answers": {"goal": {"choice": "invent"}}}, partial, "Jev")


def test_validator_rejects_an_out_of_range_probability_on_the_jev_route():
    with pytest.raises(RuntimeError, match="invalid probability"):
        response_contract._validate_typed_answers(
            {"answers": {**_good_answers(), "clarification": {"type": "noul", "noul": 1.4}}},
            response_contract.QUESTIONS, "Jev")


# ---------------------------------------------------------------------------
# Defect 2: a failing local engine was retried forever.
# ---------------------------------------------------------------------------

def test_hosted_led_chain_stops_retrying_a_failing_local_engine():
    """Defect 2: the local slot is called at most the documented limit.

    Before the fix this produced six hosted calls, six local calls and a
    failure counter that never left zero.
    """
    calls = {"jev": 0, "laya": 0}

    def _boom(name):
        def _call(**kwargs):
            calls[name] += 1
            raise RuntimeError(f"{name} offline")
        return _call

    with patch.object(response_contract, "_request_jev", _boom("jev")), \
         patch.object(response_contract, "_request_laya", _boom("laya")):
        for _ in range(6):
            with pytest.raises(RuntimeError):
                response_contract.evaluate_contract("q", mode="api_with_local_fallback")

    limit = response_contract._LAYA_FALLBACK_FAILURE_LIMIT
    assert calls["laya"] <= limit, "the local engine was called past the documented cooldown"
    # The counter advances, so the cooldown is real rather than a no-op branch.
    assert response_contract._laya_failure_count > limit


def test_hosted_led_chain_still_uses_the_local_slot_under_the_limit():
    """The cooldown must not disable the fallback before the limit."""
    limit = response_contract._LAYA_FALLBACK_FAILURE_LIMIT

    def _hosted_fails(**kwargs):
        raise RuntimeError("hosted offline")

    with patch.object(response_contract, "_request_jev", _hosted_fails), \
         patch.object(response_contract, "_request_laya", return_value={"answers": _good_answers()}) as local:
        for index in range(limit):
            result = response_contract.evaluate_contract("q", mode="api_with_local_fallback")
            assert result["_doga_provider"] == "laya_fallback"
    assert local.call_count == limit


def test_the_hosted_error_is_still_reported_while_the_local_retry_is_suppressed():
    """Suppressing the retry must not turn a failure into a silent success.

    The cooldown stops the local call, but the hosted failure that triggered the
    chain is still raised, so the caller learns the contract was not produced.
    """
    limit = response_contract._LAYA_FALLBACK_FAILURE_LIMIT
    local_calls = {"n": 0}

    def _hosted_fails(**kwargs):
        raise RuntimeError("hosted offline")

    def _local(**kwargs):
        local_calls["n"] += 1
        raise RuntimeError("local offline")

    with patch.object(response_contract, "_request_jev", _hosted_fails), \
         patch.object(response_contract, "_request_laya", _local):
        messages = []
        for _ in range(limit + 3):
            with pytest.raises(RuntimeError) as caught:
                response_contract.evaluate_contract("q", mode="api_with_local_fallback")
            messages.append(str(caught.value))

    assert local_calls["n"] == limit
    # While the retry is still allowed, the local failure is what surfaced, which
    # is the pre-existing behaviour and is left alone.
    assert messages[:limit] == ["local offline"] * limit
    # Once the retry is suppressed the hosted failure is reported instead, so the
    # caller still learns that no contract was produced.
    assert messages[limit:] == ["hosted offline"] * 3
    # The counter keeps advancing past the limit, so a suppressed run is visible
    # in the diagnostics rather than looking like an idle counter.
    assert response_contract._laya_failure_count == limit + 3


def test_the_counter_clears_when_the_hosted_route_recovers():
    """The hosted success is the recovery signal for a cooled-down local slot.

    While the local slot is suppressed it cannot report its own recovery, so a
    successful hosted evaluation is what clears the counter and puts the local
    slot back in service.
    """
    limit = response_contract._LAYA_FALLBACK_FAILURE_LIMIT
    state = {"hosted_ok": False}

    def _hosted(**kwargs):
        if not state["hosted_ok"]:
            raise RuntimeError("hosted offline")
        return {"answers": _good_answers()}

    with patch.object(response_contract, "_request_jev", _hosted), \
         patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local offline")):
        for _ in range(limit):
            with pytest.raises(RuntimeError):
                response_contract.evaluate_contract("q", mode="api_with_local_fallback")
        assert response_contract._laya_failure_count >= limit

        state["hosted_ok"] = True
        response_contract.evaluate_contract("q", mode="api_with_local_fallback")
        assert response_contract._laya_failure_count == 0


def test_a_healthy_local_call_through_local_only_resets_the_cooldown():
    """A local-led success is the other recovery signal."""
    response_contract._laya_failure_count = response_contract._LAYA_FALLBACK_FAILURE_LIMIT
    with patch.object(response_contract, "_request_laya", return_value={"answers": _good_answers()}), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted was called")):
        response_contract.evaluate_contract("q", mode="local_only")
    assert response_contract._laya_failure_count == 0


def test_a_healthy_hosted_route_clears_the_local_failure_counter():
    """Both directions share the counter, and a hosted success clears it."""
    response_contract._laya_failure_count = 2
    with patch.object(response_contract, "_request_jev", return_value={"answers": _good_answers()}):
        response_contract.evaluate_contract("q", mode="api_with_local_fallback")
    assert response_contract._laya_failure_count == 0


def test_local_led_chain_cooldown_is_unchanged():
    """The existing direction keeps its documented behaviour."""
    limit = response_contract._LAYA_FALLBACK_FAILURE_LIMIT
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("offline")), \
         patch.object(response_contract, "_request_jev", return_value={"answers": _good_answers()}) as remote:
        for _ in range(limit):
            response_contract.evaluate_contract("q", mode="local_with_api_fallback")
        with pytest.raises(RuntimeError):
            response_contract.evaluate_contract("q", mode="local_with_api_fallback")
    assert remote.call_count == limit


# ---------------------------------------------------------------------------
# Defect 3: an out-of-range noul was trusted.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score", [99.0, -5.0, 1.4, -0.1, float("inf"), float("-inf"), float("nan")])
def test_out_of_range_noul_never_produces_a_confident_contract(score):
    """Defect 3: an impossible score reads as no signal at all.

    A score of 99 previously produced ``conditional_response=True``, which told
    the model the ambiguity signal was high. That is the opposite of safe: a
    broken provider would have looked like a confident one.
    """
    contract = response_contract.build_contract({
        "answers": {
            "goal": {"choice": "action"},
            "mode": {"choice": "recommend"},
            "clarification": {"noul": score},
        },
    })
    assert contract["ask_clarifying_question"] is False
    assert contract["conditional_response"] is False


@pytest.mark.parametrize("score", [99.0, -5.0])
def test_out_of_range_noul_does_not_trigger_a_clarifying_question(score):
    """The clarify path compares the same score and must reject it too."""
    contract = response_contract.build_contract({
        "answers": {
            "goal": {"choice": "action"},
            "mode": {"choice": "clarify"},
            "clarification": {"noul": score},
        },
    })
    assert contract["ask_clarifying_question"] is False


def test_in_range_noul_still_drives_the_conditional_response():
    """The range check must not flatten a real signal."""
    contract = response_contract.build_contract({
        "answers": {
            "goal": {"choice": "action"},
            "mode": {"choice": "recommend"},
            "clarification": {"noul": 0.85},
        },
    })
    assert contract["conditional_response"] is True


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_noul_boundaries_are_inclusive(score):
    """0 and 1 are valid probabilities, not out-of-range values."""
    contract = response_contract.build_contract({
        "answers": {"mode": {"choice": "clarify"}, "clarification": {"noul": score}},
    })
    # 1.0 clears the threshold and asks; 0.0 does not.
    assert contract["ask_clarifying_question"] is (score >= 1.0)


def test_boolean_noul_is_not_treated_as_a_probability():
    """True is an int in Python and must not read as 1.0."""
    contract = response_contract.build_contract({
        "answers": {"mode": {"choice": "clarify"}, "clarification": {"noul": True}},
    })
    assert contract["ask_clarifying_question"] is False


def test_clarification_threshold_is_a_single_named_constant():
    """The threshold is named once rather than repeated as two literals."""
    assert response_contract._CLARIFICATION_SIGNAL_THRESHOLD == 0.7
    source = (
        __import__("pathlib").Path(response_contract.__file__).read_text()
    )
    # Exactly two uses: the two comparisons in build_contract, plus the
    # definition of the constant itself is a different literal assignment.
    assert source.count("_CLARIFICATION_SIGNAL_THRESHOLD") >= 3
    assert ">= 0.7" not in source