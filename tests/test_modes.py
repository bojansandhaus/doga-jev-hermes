"""The four-mode provider contract and the interchangeable local model slot.

Two things are covered here. First the mode surface: each of the four canonical
modes resolves to the provider order it promises, every alias in the spec table
still selects the mode it denotes, and no alias string ever reaches observable
output. Second the local slot: ``DOGA_LOCAL_MODEL`` selects which local engine
answers, so any System One decision model speaking the shared ``/v1/systemone``
contract can be used by configuration alone.

Nothing here performs a live call. Every provider is either a fake module object
or a monkeypatched function, and a socket fixture fails loudly if a route tries a
real one.
"""
import json
import sys
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from doga import response_contract
from doga.response_contract import resolve_mode
import doga.__init__ as plugin


def _answers(model="local-engine"):
    return {"answers": {
        "goal": {"type": "choice", "choice": "action"},
        "mode": {"type": "choice", "choice": "recommend"},
        "stakes": {"type": "choice", "choice": "medium"},
        "clarification": {"type": "noul", "noul": 0.85},
        "scenario_need": {"type": "choice", "choice": "compare_options"},
    }, "model": model}


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    """No route in this file may open a real connection."""
    def _blocked(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture(autouse=True)
def _clean_local_state(monkeypatch):
    """Reset the cached agent and breaker around every case.

    Both are module level by design, so a test that leaves them dirty would
    change the next test's routing.
    """
    monkeypatch.setattr(response_contract, "_laya_agent", None)
    monkeypatch.setattr(response_contract, "_laya_model", None)
    monkeypatch.setattr(response_contract, "_laya_failure_count", 0)
    monkeypatch.delenv(response_contract.LOCAL_MODEL_ENV, raising=False)


# ---------------------------------------------------------------------------
# The four canonical modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,order", [
    ("api_with_local_fallback", ("jev", "laya")),
    ("api_only", ("jev",)),
    ("local_only", ("laya",)),
    ("local_with_api_fallback", ("laya", "jev")),
])
def test_each_canonical_mode_resolves_to_its_provider_order(mode, order):
    resolved = resolve_mode(mode)
    assert resolved.name == mode
    assert resolved.providers == order


def test_four_canonical_modes_are_exactly_the_contract_set():
    assert set(response_contract.MODES) == {
        "api_with_local_fallback", "api_only", "local_only", "local_with_api_fallback",
    }


def test_api_modes_resolve_to_whichever_hosted_provider_is_configured():
    assert resolve_mode("api_only", hosted="clef").providers == ("clef",)
    assert resolve_mode("api_with_local_fallback", hosted="clef").providers == ("clef", "laya")
    assert resolve_mode("local_with_api_fallback", hosted="clef").providers == ("laya", "clef")


def test_local_with_api_fallback_names_the_hosted_provider_it_falls_back_to():
    assert resolve_mode("local_with_api_fallback", hosted="clef").fallback == "clef"
    assert resolve_mode("api_with_local_fallback", hosted="clef").fallback == "laya"
    assert resolve_mode("api_only").fallback == "none"
    assert resolve_mode("local_only").fallback == "none"


def test_leads_and_fallbacks_match_the_contract_table():
    leads = {m: resolve_mode(m).providers[0] for m in response_contract.MODES}
    assert leads == {
        "api_with_local_fallback": "jev",
        "api_only": "jev",
        "local_only": "laya",
        "local_with_api_fallback": "laya",
    }
    assert {m: len(resolve_mode(m).providers) for m in response_contract.MODES} == {
        "api_with_local_fallback": 2,
        "api_only": 1,
        "local_only": 1,
        "local_with_api_fallback": 2,
    }


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias,canonical", [
    ("jev_api", "api_only"),
    ("typesafe", "api_only"),
    ("openrouter", "api_only"),
    ("clef", "api_only"),
    ("clef_api", "api_only"),
    ("laya", "local_only"),
    ("laya_local", "local_only"),
    ("laya_then_hosted", "local_with_api_fallback"),
    ("laya_with_jev_fallback", "local_with_api_fallback"),
    ("clef_with_local_fallback", "api_with_local_fallback"),
])
def test_every_spec_alias_resolves_to_its_canonical_mode(alias, canonical):
    assert resolve_mode(alias).name == canonical


@pytest.mark.parametrize("alias", sorted(response_contract.MODE_ALIASES))
def test_no_alias_ever_reaches_observable_output(alias):
    """Resolved output is canonical, so no alias leaks into a chain or a log."""
    resolved = resolve_mode(alias)
    assert resolved.name in response_contract.MODES
    assert alias not in resolved.providers or alias in response_contract.PROVIDERS
    for provider in resolved.providers:
        assert provider in response_contract.PROVIDERS


@pytest.mark.parametrize("alias,hosted,order", [
    ("jev_api", "jev", ("jev",)),
    ("typesafe", "jev", ("jev",)),
    ("openrouter", "jev", ("jev",)),
    ("clef_api", "clef", ("clef",)),
    ("clef_with_local_fallback", "clef", ("clef", "laya")),
    ("laya_local", "clef", ("laya",)),
    ("laya_with_jev_fallback", "clef", ("laya", "clef")),
])
def test_each_alias_produces_the_routing_decision_its_old_name_meant(alias, hosted, order):
    """A configuration that worked before still makes the same routing decision."""
    assert resolve_mode(alias, hosted=hosted).providers == order


@pytest.mark.parametrize("alias", ["AUTO", "Laya_Local", "JEV_API", "CLEF_api"])
def test_mode_names_are_case_insensitive(alias):
    assert resolve_mode(alias).name in response_contract.MODES


def test_auto_is_api_with_local_fallback_when_a_local_model_is_usable(monkeypatch):
    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: object()))
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "convaiinnovations/laya")
    assert resolve_mode("auto").name == "api_with_local_fallback"


def test_auto_is_api_only_when_no_local_model_is_usable(monkeypatch):
    # laya is not installed on the test machine, so no local model is usable.
    monkeypatch.delitem(sys.modules, "laya", raising=False)
    monkeypatch.setitem(sys.modules, "laya", None)
    assert resolve_mode("auto").name == "api_only"


@pytest.mark.parametrize("name", ["", "   ", "not-a-mode", "api-only", "laya_local_extra"])
def test_unknown_mode_raises_and_names_every_accepted_mode(name):
    with pytest.raises(response_contract.ModeError) as excinfo:
        resolve_mode(name)
    message = str(excinfo.value)
    for accepted in (*response_contract.MODES, *response_contract.MODE_ALIASES):
        assert accepted in message


# ---------------------------------------------------------------------------
# Routing behaviour per mode
# ---------------------------------------------------------------------------

def test_api_only_calls_the_hosted_provider_and_never_the_local_slot():
    with patch.object(response_contract, "_request_jev", return_value=_answers()) as hosted, \
         patch.object(response_contract, "_request_laya", side_effect=AssertionError("local called")):
        response_contract.evaluate_contract("request", mode="api_only")
    assert hosted.call_count == 1


def test_api_only_reports_a_hosted_failure_and_never_reroutes():
    with patch.object(response_contract, "_request_jev", side_effect=RuntimeError("hosted down")), \
         patch.object(response_contract, "_request_laya", side_effect=AssertionError("local called")):
        with pytest.raises(RuntimeError, match="hosted down"):
            response_contract.evaluate_contract("private message", mode="api_only")


def test_local_only_calls_the_local_slot_and_never_the_hosted_provider():
    with patch.object(response_contract, "_request_laya", return_value=_answers()) as local, \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")):
        response_contract.evaluate_contract("request", mode="local_only")
    assert local.call_count == 1


def test_local_only_reports_a_local_failure_and_never_reroutes():
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local down")), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")):
        with pytest.raises(RuntimeError, match="local down"):
            response_contract.evaluate_contract("private message", mode="local_only")


def test_api_with_local_fallback_calls_the_api_first():
    with patch.object(response_contract, "_request_jev", return_value=_answers()) as hosted, \
         patch.object(response_contract, "_request_laya", side_effect=AssertionError("local called")):
        result = response_contract.evaluate_contract("request", mode="api_with_local_fallback")
    assert hosted.call_count == 1
    assert "_doga_provider" not in result


def test_api_with_local_fallback_falls_back_to_the_local_slot_on_a_hosted_failure():
    with patch.object(response_contract, "_request_jev", side_effect=RuntimeError("hosted down")), \
         patch.object(response_contract, "_request_laya", return_value=_answers()) as local:
        result = response_contract.evaluate_contract("private message", mode="api_with_local_fallback")
    assert local.call_count == 1
    assert result["_doga_provider"] == "laya_fallback"
    assert result["answers"] == _answers()["answers"]


def test_api_with_local_fallback_reports_when_both_sides_fail():
    with patch.object(response_contract, "_request_jev", side_effect=RuntimeError("hosted down")), \
         patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local down")):
        with pytest.raises(RuntimeError, match="local down"):
            response_contract.evaluate_contract("private message", mode="api_with_local_fallback")


def test_local_with_api_fallback_calls_the_local_slot_first():
    with patch.object(response_contract, "_request_laya", return_value=_answers()) as local, \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")):
        response_contract.evaluate_contract("request", mode="local_with_api_fallback")
    assert local.call_count == 1


def test_local_with_api_fallback_reaches_the_api_on_a_local_failure():
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local down")), \
         patch.object(response_contract, "_request_jev", return_value=_answers()) as hosted:
        result = response_contract.evaluate_contract("private message", mode="local_with_api_fallback")
    assert hosted.call_count == 1
    assert result["_doga_provider"] == "jev_fallback"


def test_local_with_api_fallback_keeps_the_breaker_after_repeated_local_failures():
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local down")), \
         patch.object(response_contract, "_request_jev", return_value=_answers()) as hosted:
        for _ in range(3):
            response_contract.evaluate_contract("sample", mode="local_with_api_fallback")
        with pytest.raises(RuntimeError, match="local down"):
            response_contract.evaluate_contract("sample", mode="local_with_api_fallback")
    assert hosted.call_count == 3


def test_api_with_local_fallback_on_clef_never_reaches_jev():
    """The API side is one hosted route, so its fallback is local, not Jev."""
    with patch.object(response_contract, "_request_clef", side_effect=RuntimeError("cloudflare down")), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("Jev was called")), \
         patch.object(response_contract, "_request_laya", return_value=_answers()):
        result = response_contract.evaluate_contract("request", mode="api_with_local_fallback", hosted="clef")
    assert result["_doga_provider"] == "laya_fallback"


def test_legacy_provider_and_fallback_pair_routes_exactly_as_before():
    """The pre-existing call signature keeps its old routing decision."""
    with patch.object(response_contract, "_request_laya", return_value=_answers()), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")):
        response_contract.evaluate_contract("request", provider="laya", fallback_to_jev=True)
    with patch.object(response_contract, "_request_laya", side_effect=AssertionError("local called")), \
         patch.object(response_contract, "_request_jev", return_value=_answers()) as hosted:
        response_contract.evaluate_contract("request", provider="jev", fallback_to_jev=False)
    assert hosted.call_count == 1


def test_chain_failure_logs_carry_no_request_text_no_credential_no_answer(caplog):
    secret_state = "private message text"
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError(secret_state)), \
         patch.object(response_contract, "_request_jev", return_value=_answers("hosted-answer")):
        with caplog.at_level("WARNING"):
            response_contract.evaluate_contract(secret_state, mode="local_with_api_fallback")
    assert "RuntimeError" in caplog.text
    assert secret_state not in caplog.text
    assert "hosted-answer" not in caplog.text


# ---------------------------------------------------------------------------
# The interchangeable local model slot
# ---------------------------------------------------------------------------

def test_local_model_defaults_to_laya(monkeypatch):
    monkeypatch.delenv(response_contract.LOCAL_MODEL_ENV, raising=False)
    assert response_contract.local_model() == response_contract.LAYA_MODEL
    assert response_contract.LAYA_MODEL == "convaiinnovations/laya"


def test_local_model_default_reaches_the_default_engine_name(monkeypatch):
    loads = []

    class Agent:
        def predict(self, state, questions):
            return _answers(response_contract.LAYA_MODEL)

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: loads.append(name) or Agent()))
    monkeypatch.delenv(response_contract.LOCAL_MODEL_ENV, raising=False)
    result = response_contract.evaluate_contract("request", mode="local_only")
    assert loads == ["convaiinnovations/laya"]
    assert result["model"] == "convaiinnovations/laya"


@pytest.mark.parametrize("engine", [
    "laya",
    "kev",
    "kev-0.8b",
    "tev1",
    "Tev1-4B",
    "jeff-qwen3.5-0.8b",
    "jeff-gemma4-e2b",
    "laya-multilingual",
    "laya-typed-decisions",
    "an-engine-nobody-has-released-yet",
])
def test_known_and_unlisted_local_models_are_accepted_verbatim(monkeypatch, engine):
    """No allowlist: a new engine works by configuration alone."""
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, engine)
    assert response_contract.local_model() == engine


def test_local_model_changes_what_the_local_request_asks_for(monkeypatch):
    loads = []

    class Agent:
        def predict(self, state, questions):
            return _answers(response_contract.local_model())

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: loads.append(name) or Agent()))
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "tev1")
    result = response_contract.evaluate_contract("request", mode="local_only")
    assert loads == ["tev1"]
    assert result["model"] == "tev1"


def test_switching_local_model_loads_the_new_engine_rather_than_reusing_the_old(monkeypatch):
    loads = []

    class Agent:
        def predict(self, state, questions):
            return _answers(response_contract.local_model())

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: loads.append(name) or Agent()))
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "laya")
    response_contract.evaluate_contract("first", mode="local_only")
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "kev")
    response_contract.evaluate_contract("second", mode="local_only")
    assert loads == ["laya", "kev"]


def test_the_same_engine_is_loaded_once_and_reused(monkeypatch):
    loads = []

    class Agent:
        def predict(self, state, questions):
            return _answers()

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: loads.append(name) or Agent()))
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "kev")
    for _ in range(3):
        response_contract.evaluate_contract("request", mode="local_only")
    assert loads == ["kev"]


def test_local_model_reaches_the_state_the_local_engine_is_given(monkeypatch):
    seen = {}

    class Agent:
        def predict(self, state, questions):
            seen.update(state=state, questions=questions)
            return _answers()

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: Agent()))
    response_contract.evaluate_contract("Recommend one path", mode="local_only")
    assert seen["state"] == {"user_request": "Recommend one path"}
    assert set(seen["questions"]) == set(response_contract.QUESTIONS)


def test_local_model_is_used_in_the_fallback_mode_too(monkeypatch):
    loads = []

    class Agent:
        def predict(self, state, questions):
            return _answers(response_contract.local_model())

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: loads.append(name) or Agent()))
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "jeff-qwen3.5-0.8b")
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")):
        response_contract.evaluate_contract("request", mode="local_with_api_fallback")
    assert loads == ["jeff-qwen3.5-0.8b"]


def test_a_namespace_slash_in_local_model_is_accepted(monkeypatch):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "some-namespace/some-engine-1.0")
    assert response_contract.local_model() == "some-namespace/some-engine-1.0"


@pytest.mark.parametrize("value", ["", "   ", "\t", "\n  "])
def test_empty_or_whitespace_only_local_model_is_rejected(monkeypatch, value):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, value)
    with pytest.raises(RuntimeError, match=response_contract.LOCAL_MODEL_ENV):
        response_contract.local_model()


@pytest.mark.parametrize("value", [
    'engine"name',              # would break out of the JSON string
    "engine'name",
    "engine name",              # would corrupt a URL path segment
    "engine\\name",
    "engine\nname",             # control character
    "../engine",                # path traversal
    "engine?query=1",            # would corrupt a URL path segment
    "engine#fragment",
    "engine/../other",
    "//engine",
    "engine\tvalue",
    "http://engine.example",
])
def test_unsafe_local_model_is_rejected_rather_than_passed_through(monkeypatch, value):
    """Rejected on character shape, not on being an unlisted engine."""
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, value)
    with pytest.raises(RuntimeError, match=response_contract.LOCAL_MODEL_ENV):
        response_contract.local_model()


def test_null_byte_local_model_is_rejected_at_the_environment_boundary(monkeypatch):
    """A null byte cannot even be placed in an environment variable."""
    with pytest.raises(ValueError):
        monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "engine\x00name")


def test_rejected_local_model_fails_the_route_without_a_silent_default(monkeypatch):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "bad name")
    loads = []

    class Agent:
        def predict(self, state, questions):
            return _answers()

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: loads.append(name) or Agent()))
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")):
        with pytest.raises(RuntimeError):
            response_contract.evaluate_contract("private message", mode="local_only")
    assert loads == []


def test_local_model_surrounding_whitespace_is_trimmed(monkeypatch):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "  tev1  ")
    assert response_contract.local_model() == "tev1"


def test_local_model_is_never_a_fallback_order_or_a_mode_alias(monkeypatch):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "kev")
    # It changes which engine answers, never the provider order.
    assert resolve_mode("local_only").providers == ("laya",)
    assert resolve_mode("local_with_api_fallback").providers == ("laya", "jev")
    # And it is not selectable as a mode name.
    with pytest.raises(response_contract.ModeError):
        resolve_mode("kev")


def test_status_reports_the_configured_local_model(monkeypatch):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "kev")
    state = plugin._PluginState()
    assert state.local_model_name() == "kev"
    assert state.to_dict()["local_model"] == "kev"


def test_status_reports_an_invalid_local_model_without_failing(monkeypatch):
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "bad name")
    state = plugin._PluginState()
    assert state.local_model_name() == "invalid"
    assert "Local model: invalid" in plugin._handle_doga("status")


# ---------------------------------------------------------------------------
# The plugin surface: slash commands, help, startup configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias", sorted(response_contract.MODE_ALIASES))
def test_every_alias_is_selectable_through_the_slash_command(monkeypatch, alias):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    monkeypatch.setattr(plugin._state, "_mode_valid", True)
    message = plugin._handle_doga(f"mode {alias}")
    assert message is not None
    assert "mode:" in message
    assert plugin._state._mode in response_contract.MODES


def test_help_lists_all_four_canonical_modes_and_the_aliases():
    help_text = plugin._handle_doga("help")
    assert help_text is not None
    for mode in response_contract.MODES:
        assert mode in help_text
    for alias in response_contract.MODE_ALIASES:
        assert alias in help_text


def test_help_no_longer_advertises_the_old_names_as_the_mode_list():
    help_text = plugin._handle_doga("help")
    assert help_text is not None
    # The old names survive only as aliases, in their own line, not as the
    # advertised mode list.
    assert "mode jev_api|clef_api|laya_local|laya_with_jev_fallback" not in help_text


def test_mode_usage_error_names_the_canonical_modes():
    message = plugin._handle_doga("mode")
    assert message is not None
    assert "Usage" in message
    for mode in response_contract.MODES:
        assert mode in message


def test_mode_rejected_by_the_command_preserves_the_current_mode(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    assert "Usage" in plugin._handle_doga("mode definitely-not-a-mode")
    assert plugin._state.decision_mode == "api_only"
    assert plugin._state._mode_valid is True


def test_status_reports_the_canonical_mode_not_the_alias(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    plugin._handle_doga("mode laya_with_jev_fallback")
    status = plugin._handle_doga("status")
    assert "local_with_api_fallback" in status
    assert "laya_with_jev_fallback" not in status


@pytest.mark.parametrize("env_value,canonical", [
    ("api_with_local_fallback", "api_with_local_fallback"),
    ("api_only", "api_only"),
    ("local_only", "local_only"),
    ("local_with_api_fallback", "local_with_api_fallback"),
    ("jev_api", "api_only"),
    ("laya_local", "local_only"),
    ("laya_with_jev_fallback", "local_with_api_fallback"),
    ("clef_api", "api_only"),
    ("clef_with_local_fallback", "api_with_local_fallback"),
])
def test_startup_mode_accepts_canonical_names_and_aliases(monkeypatch, env_value, canonical):
    monkeypatch.setenv("DOGA_DECISION_MODE", env_value)
    monkeypatch.delenv("DOGA_DECISION_PROVIDER", raising=False)
    monkeypatch.delenv("DOGA_LAYA_JEV_FALLBACK", raising=False)
    assert plugin._PluginState().decision_mode == canonical


def test_default_configuration_is_unchanged(monkeypatch):
    """No setting at all is still Jev, API only, which is api_only."""
    monkeypatch.delenv("DOGA_DECISION_MODE", raising=False)
    monkeypatch.delenv("DOGA_DECISION_PROVIDER", raising=False)
    monkeypatch.delenv("DOGA_LAYA_JEV_FALLBACK", raising=False)
    state = plugin._PluginState()
    assert state.decision_mode == "api_only"
    assert state.decision_provider == "jev"
    assert state.jev_fallback is False


def test_legacy_provider_setting_alone_still_selects_that_provider(monkeypatch):
    monkeypatch.delenv("DOGA_DECISION_MODE", raising=False)
    monkeypatch.setenv("DOGA_DECISION_PROVIDER", "laya")
    monkeypatch.delenv("DOGA_LAYA_JEV_FALLBACK", raising=False)
    assert plugin._PluginState().decision_mode == "local_only"


def test_legacy_fallback_flag_alone_still_enables_the_local_fallback(monkeypatch):
    monkeypatch.delenv("DOGA_DECISION_MODE", raising=False)
    monkeypatch.setenv("DOGA_DECISION_PROVIDER", "laya")
    monkeypatch.setenv("DOGA_LAYA_JEV_FALLBACK", "1")
    state = plugin._PluginState()
    assert state.decision_mode == "local_with_api_fallback"
    assert state.jev_fallback is True


def test_invalid_startup_mode_fails_closed_rather_than_defaulting_to_the_api(monkeypatch):
    monkeypatch.setenv("DOGA_DECISION_MODE", "not-a-mode")
    state = plugin._PluginState()
    assert "invalid" in state.decision_mode
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")), \
         patch.object(response_contract, "_request_laya", side_effect=AssertionError("local called")):
        with pytest.raises(Exception):
            response_contract.evaluate_contract("private message", provider=state.decision_provider)


def test_the_plugin_hook_routes_by_the_canonical_mode(monkeypatch):
    """Both API-led modes lead with the same provider and must route apart."""
    for mode, hosted_calls, local_calls in (
        ("api_only", 1, 0),
        ("api_with_local_fallback", 1, 0),
    ):
        monkeypatch.setattr(plugin._state, "decision_provider", "jev")
        assert plugin._handle_doga(f"mode {mode}")
        monkeypatch.setattr(plugin._state, "enabled", True)
        monkeypatch.setattr(plugin._state, "jev_enabled", True)
        monkeypatch.setattr(plugin._state, "memory_enabled", False)
        with patch.object(response_contract, "_request_jev", return_value=_answers()) as hosted, \
             patch.object(response_contract, "_request_laya", return_value=_answers()) as local:
            plugin._on_pre_llm_call(user_message="Recommend one path")
        assert hosted.call_count == hosted_calls, mode
        assert local.call_count == local_calls, mode


def test_the_plugin_hook_reports_an_invalid_mode_without_sending_the_request(monkeypatch):
    monkeypatch.setenv("DOGA_DECISION_MODE", "not-a-mode")
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("hosted called")), \
         patch.object(response_contract, "_request_laya", side_effect=AssertionError("local called")):
        result = plugin._on_pre_llm_call(user_message="private request")
    assert "[DOGA response contract]" not in result["context"]
    assert result["context"]


def test_local_model_json_body_is_not_corrupted_by_an_accepted_name(monkeypatch):
    """An accepted engine name survives the JSON round trip unchanged."""
    monkeypatch.setenv(response_contract.LOCAL_MODEL_ENV, "some_namespace/engine-1.0")
    captured = {}

    class Agent:
        def predict(self, state, questions):
            captured["payload"] = json.dumps({"state": state, "questions": questions, "model": response_contract.local_model()})
            return _answers()

    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda name: Agent()))
    response_contract.evaluate_contract("request", mode="local_only")
    assert json.loads(captured["payload"])["model"] == "some_namespace/engine-1.0"