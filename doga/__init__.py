"""DOGA Plugin — probabilistic, goal-aware reasoning for Hermes Agent.

Injects a thinking guidance prompt before each LLM call, registers a
``simulate`` tool for Monte Carlo analysis, and formats the output to
show a simulation summary alongside the final answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, Optional

from . import de_bono_hats
from . import depth_selector
from . import simulation_engine
from . import thinking_prompt
from . import output_formatter
from . import response_contract

logger = logging.getLogger(__name__)

# Marks a turn whose status has not been recorded yet, so "no outcome yet" is
# never confused with the string an outcome would be.
_UNSET = object()

# Optional Mnemosyne memory backend
try:
    from mnemosyne import remember, recall
    MNEMOSYNE_AVAILABLE = True
except ImportError:
    MNEMOSYNE_AVAILABLE = False

# ---------------------------------------------------------------------------
# PHASE 3 — Recursive Reasoning via Hermes tool-calling loop
#
# Hermes' existing tool-calling loop (tool → execute → feed back → repeat)
# enables recursive reasoning without core changes. reason_deeper tool
# triggers the loop; handler tracks depth via _reasoning_stack.
# max_iterations in Hermes prevents infinite loops.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Plugin state
# ---------------------------------------------------------------------------

class _PluginState:
    """Mutable plugin settings, adjustable via /doga slash command.

    Global settings (enabled, depth, etc.) are shared across sessions, so every
    write to them is taken under ``_write_lock``: a slash command can land on
    another session's thread while that thread is mid-turn, and an unsynchronised
    read-modify-write of the mode state would interleave into a half-applied
    mode. ``_last_jev_status`` is per-turn and keyed by the turn that produced
    it, so one session's status cannot report another session's outcome.
    """

    def __init__(self):
        self._write_lock = threading.Lock()
        # Per-turn response-contract status. The turn is the key, so the outcome
        # a slash command reports is the one this thread produced, rather than
        # whichever turn happened to write last.
        self._status_lock = threading.Lock()
        self._status_local = threading.local()
        self._status_latest: str = "enabled"
        self.enabled: bool = True
        self.auto_depth: bool = True
        self.depth: int = 3
        self.show_simulation: bool = True
        self.max_scenarios: int = 5
        self.memory_enabled: bool = True
        self.jev_enabled: bool = True
        self._last_jev_status: str = "enabled"
        self._mode: str = "api_only"
        self._mode_valid: bool = True
        self._fallback: bool = False
        # Which hosted provider the API side resolves to. Kept beside the mode
        # rather than inside it, because the legacy DOGA_DECISION_PROVIDER value
        # names a provider and must keep working on its own.
        self._hosted: str = "jev"
        self._resolve_startup_mode()
        self.de_bono_enabled: bool = True
        self.max_recursion: int = 3
        self._local = threading.local()

    @property
    def _last_jev_status(self) -> str:
        """The outcome of the most recent response-contract attempt for this turn.

        Held per turn and written under a lock. A slash command on one session's
        thread reports that session's outcome; without this, ``/doga status``
        on a quiet session reported whatever a concurrent session last wrote,
        which is the race this closes. A thread with no turn of its own — a
        status request that carries no turn — reports the last attempt in the
        process, which is the closest honest answer available to it.
        """
        own = getattr(self._status_local, "value", _UNSET)
        if own is not _UNSET:
            return own
        with self._status_lock:
            return self._status_latest

    @_last_jev_status.setter
    def _last_jev_status(self, value: str) -> None:
        with self._status_lock:
            self._status_local.value = value
            self._status_latest = value

    def _reset_turn_status(self) -> None:
        """Start this thread's turn with no outcome recorded.

        Called from ``pre_llm_call``, so the status a turn reports is the one it
        produced. Clearing to the empty marker rather than to a previous turn's
        value matters: without it a failed turn's ``error`` outlived the turn
        that failed and was reported as this one's outcome.
        """
        with self._status_lock:
            self._status_local.value = "enabled"

    def reset_recursion_stop(self) -> None:
        """Clear this turn's recursion stop latch and its ignored-stop count.

        Called when the limit changes. Both are set against the limit that was
        in force, so leaving them in place would make a raised limit take
        effect only from the next turn.
        """
        with self._write_lock:
            self._stop_sent = False
            self._stop_count = 0

    def set(self, **settings: Any) -> None:
        """Write global settings together, under the plugin lock.

        A slash command can land on another session's thread while that thread
        is mid-turn, so the fields are committed as one change rather than one
        at a time, and a concurrent reader cannot observe half a new setting.
        """
        with self._write_lock:
            for name, value in settings.items():
                setattr(self, name, value)

    def _resolve_startup_mode(self) -> None:
        """Pick the startup mode from the environment, newest setting first.

        ``DOGA_DECISION_MODE`` wins over the legacy ``DOGA_DECISION_PROVIDER``
        and ``DOGA_LAYA_JEV_FALLBACK`` pair, exactly as it did before this
        change. An unrecognised name is kept verbatim and reported as invalid
        rather than being coerced into a mode that sends the request somewhere
        nobody asked for, so a typo fails closed on the first request instead.
        """
        startup_mode = os.environ.get("DOGA_DECISION_MODE", "").strip()
        if startup_mode:
            self._apply_mode(startup_mode)
            return
        provider = os.environ.get("DOGA_DECISION_PROVIDER", "jev").strip().lower() or "jev"
        fallback = os.environ.get("DOGA_LAYA_JEV_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
        if provider in response_contract.PROVIDERS and provider != "laya":
            self._hosted = provider
        if provider != "laya":
            fallback = False
        self._apply_mode(provider, fallback=fallback)

    def _apply_mode(self, name: str, *, fallback: bool | None = None, strict: bool = True) -> bool:
        """Set provider and fallback from any accepted mode spelling.

        Returns False and changes nothing when the name is not accepted, so a
        rejected selection cannot leave a half-applied mode behind. An alias is
        rewritten to its canonical name here, so no alias string reaches a log
        line, a status line, or a provider call.

        ``strict`` marks a name that came from configuration as invalid rather
        than ignoring it. That distinction matters at startup, where a typo in
        ``DOGA_DECISION_MODE`` has to surface as an invalid mode, while a
        rejected ``/doga mode`` from a user must simply leave the current mode
        untouched and be reported as a usage error.

        The whole read-modify-write is taken under ``_write_lock``. The mode,
        the hosted pin and the fallback flag are read to compute the next value
        and written together, so a slash command landing on another session's
        thread mid-update cannot observe or commit a half-applied mode.
        """
        try:
            with self._write_lock:
                resolved = response_contract.resolve_mode(name, hosted=self._hosted)
                # A spelling that names a hosted provider pins it *before* the
                # fallback re-resolution below, because that re-resolution
                # resolves an unqualified canonical name against ``_hosted``.
                # Applying the pin afterwards left ``/doga provider clef``
                # reporting ``api_only`` while routing to the default vendor.
                pinned = next(
                    (p for p in resolved.providers if p in response_contract.HOSTED_PROVIDERS),
                    None,
                )
                if pinned is not None:
                    self._hosted = pinned
                if fallback is not None:
                    # An explicit fallback flag overrides what the spelling implied, so
                    # the legacy provider plus fallback pair lands on the mode that
                    # actually has that chain. Without this, ``provider=laya`` with the
                    # fallback flag set would report local_only while routing as a chain.
                    side_leads_local = resolved.providers[0] == response_contract.LOCAL_PROVIDER
                    if side_leads_local:
                        resolved = response_contract.resolve_mode(
                            response_contract.LOCAL_WITH_API_FALLBACK if fallback
                            else response_contract.LOCAL_ONLY, hosted=self._hosted)
                    else:
                        resolved = response_contract.resolve_mode(
                            response_contract.API_WITH_LOCAL_FALLBACK if fallback
                            else response_contract.API_ONLY, hosted=self._hosted)
                # A pin that the new mode cannot use is cleared here rather than
                # silently carried: leaving ``clef`` pinned on ``local_only``
                # would leave exactly one session away from routing every
                # request to a vendor the current mode says is not in play.
                resolved_providers = resolved.providers
                hosted_in_chain = next(
                    (p for p in resolved_providers if p in response_contract.HOSTED_PROVIDERS),
                    None,
                )
                if hosted_in_chain is not None:
                    self._hosted = hosted_in_chain
                else:
                    # No hosted provider is in play, so a pin from an earlier
                    # API-led selection is dropped rather than kept in reserve:
                    # a silent pin that comes back on the next API-led selection
                    # is a vendor nobody asked for on this one.
                    self._hosted = response_contract.DEFAULT_HOSTED
                self._mode = resolved.name
                self._mode_valid = True
                # Remember which hosted provider an API-led mode resolved to, so the
                # next mode selection that does not pin one keeps that provider.
                if fallback is None:
                    fallback = (
                        len(resolved_providers) > 1
                        and resolved.fallback in response_contract.HOSTED_PROVIDERS
                    )
                self._fallback = fallback
        except response_contract.ModeError:
            if not strict:
                return False
            self._mode = str(name)
            self._mode_valid = False
            return False
        return True

    @property
    def decision_provider(self) -> str:
        """The provider that leads, kept for backwards compatibility.

        Derived from the resolved mode rather than stored separately, so the two
        can never disagree. Assigning to it selects a mode, because the legacy
        ``DOGA_DECISION_PROVIDER`` and ``/doga provider`` settings mean exactly
        that.
        """
        if not self._mode_valid:
            return "unknown"
        return self._hosted if self._mode.startswith("api") else response_contract.LOCAL_PROVIDER

    @decision_provider.setter
    def decision_provider(self, value: str) -> None:
        # A direct assignment is a legacy path, including from tests and older
        # callers, so it selects a mode but never marks the state invalid.
        self._apply_mode(str(value).strip().lower() or response_contract.DEFAULT_HOSTED,
                         fallback=False, strict=False)

    @property
    def jev_fallback(self) -> bool:
        """Whether the local side has the hosted API behind it."""
        return self._fallback

    @jev_fallback.setter
    def jev_fallback(self, value: bool) -> None:
        # Re-resolves the mode rather than only storing the flag, so assigning
        # this alone cannot leave a mode that disagrees with the flag. Which
        # side leads comes from the provider, which is preserved here: that is
        # exactly what the legacy provider plus fallback pair means.
        if not self._mode_valid:
            self._fallback = bool(value)
            return
        current = self._mode
        if self._apply_mode(current, fallback=bool(value), strict=False):
            return
        # An unusable current mode: keep the flag without touching it.
        self._fallback = bool(value)

    @property
    def _current_user_message(self) -> str:
        return getattr(self._local, 'current_user_message', "")

    @_current_user_message.setter
    def _current_user_message(self, value: str):
        self._local.current_user_message = value

    @property
    def _last_complexity(self) -> str:
        return getattr(self._local, 'last_complexity', "medium")

    @_last_complexity.setter
    def _last_complexity(self, value: str):
        self._local.last_complexity = value

    @property
    def _active_hats(self) -> list[str]:
        if not hasattr(self._local, 'active_hats'):
            self._local.active_hats = []
        return self._local.active_hats

    @_active_hats.setter
    def _active_hats(self, value: list[str]):
        self._local.active_hats = value

    @property
    def _recursion_depth(self) -> int:
        return getattr(self._local, 'recursion_depth', 0)

    @_recursion_depth.setter
    def _recursion_depth(self, value: int):
        self._local.recursion_depth = value

    @property
    def _reasoning_stack(self) -> list[dict]:
        if not hasattr(self._local, 'reasoning_stack'):
            self._local.reasoning_stack = []
        return self._local.reasoning_stack

    @_reasoning_stack.setter
    def _reasoning_stack(self, value: list[dict]):
        self._local.reasoning_stack = value

    @property
    def _stop_sent(self) -> bool:
        return getattr(self._local, 'stop_sent', False)

    @_stop_sent.setter
    def _stop_sent(self, value: bool):
        self._local.stop_sent = value

    @property
    def _stop_count(self) -> int:
        return getattr(self._local, 'stop_count', 0)

    @_stop_count.setter
    def _stop_count(self, value: int):
        self._local.stop_count = value

    @property
    def decision_mode(self) -> str:
        """The canonical mode name, or an explicit invalid marker.

        Never an alias, because this string reaches ``/doga status``, the help
        text, and the failure log line.
        """
        if not self._mode_valid:
            return f"invalid ({self._mode})"
        return self._mode

    def local_model_name(self) -> str:
        """The engine name the local slot would use, or why it cannot."""
        try:
            return response_contract.local_model()
        except RuntimeError:
            return "invalid"

    def to_dict(self) -> dict:
        mode = f"auto (complexity: {self._last_complexity})" if self.auto_depth else f"manual (depth: {self.depth})"
        return {
            "enabled": self.enabled,
            "mode": mode,
            "depth": self.depth,
            "show_simulation": self.show_simulation,
            "max_scenarios": self.max_scenarios,
            "de_bono_hats": "enabled" if self.de_bono_enabled else "disabled",
            "max_recursion": self.max_recursion,
            "memory": "enabled" if self.memory_enabled else "disabled",
            "jev": "enabled" if self.jev_enabled else "disabled",
            "decision_mode": self.decision_mode,
            "decision_provider": self.decision_provider,
            "local_model": self.local_model_name(),
            "jev_fallback": self.jev_fallback,
            "mnemosyne": "available" if MNEMOSYNE_AVAILABLE else "not installed",
        }


_state = _PluginState()

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_pre_llm_call(
    user_message: str = "",
    is_first_turn: bool = False,
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Inject thinking guidance before the LLM call.

    Returns a dict with a ``context`` key that gets appended to the
    current turn's user message.
    """
    if not _state.enabled:
        return None

    # Reset recursion state for this turn
    _state._recursion_depth = 0
    _state._reasoning_stack = []
    _state._stop_sent = False
    _state._stop_count = 0
    _state._reset_turn_status()

    _state._current_user_message = user_message

    if _state.auto_depth and user_message:
        _state._last_complexity = depth_selector.assess_complexity(user_message)
        _state.depth = depth_selector.complexity_to_depth(_state._last_complexity)

    if _state.de_bono_enabled:
        _state._active_hats = de_bono_hats.hats_for_depth(_state.depth)
    else:
        _state._active_hats = []

    past_patterns = None
    if MNEMOSYNE_AVAILABLE and _state.memory_enabled and user_message:
        try:
            results = recall(user_message, top_k=3)
            if results:
                seen = {}
                for r in results:
                    meta = getattr(r, "metadata", {}) or {}
                    gt = meta.get("goal_type", "unknown")
                    seen[gt] = seen.get(gt, 0) + 1
                past_patterns = [
                    {"goal_type": k, "count": v}
                    for k, v in sorted(seen.items(), key=lambda x: -x[1])
                ]
        except Exception:
            logger.debug("doga memory recall failed", exc_info=True)

    guide = thinking_prompt.build_goal_prompt(
        user_message,
        depth=_state.depth,
        past_patterns=past_patterns,
        hats_enabled=_state.de_bono_enabled,
    )
    if _state.jev_enabled and user_message:
        try:
            # The canonical mode is passed rather than inferred from the legacy
            # provider plus fallback pair, because that pair cannot express
            # api_with_local_fallback: both API-led modes lead with the same
            # provider and differ only in what sits behind it. An invalid mode
            # passes None, which falls back to the legacy pair and then fails
            # closed on the unknown provider.
            judged = response_contract.evaluate_contract(
                user_message,
                provider=_state.decision_provider,
                fallback_to_jev=_state.jev_fallback,
                mode=_state._mode if _state._mode_valid else None,
                hosted=_state._hosted if _state._mode_valid else None,
            )
            contract = response_contract.build_contract(judged)
            guide += "\n\n" + response_contract.render_contract(contract)
            _state._last_jev_status = "ok (fallback)" if judged.get("_doga_provider", "").endswith("_fallback") else "ok"
        except Exception as exc:
            _state._last_jev_status = "error"
            logger.warning("DOGA %s contract failed (%s); using standard guidance", _state.decision_mode, type(exc).__name__)
    return {"context": guide}


def _detect_goal_type(text: str) -> str:
    """Read the taxonomy word the model picked, from the block it picked it in.

    Anchored to an extracted ``<world_model>`` block rather than searched for
    anywhere in the cleaned text. The taxonomy words — Information,
    Understanding, Action — also appear in the injected guidance, in an echoed
    question, and in ordinary prose, so an unanchored search attributed the
    goal of whatever sentence happened to come first to every later turn in
    Mnemosyne. With no block, or no goal statement inside one, the answer is
    ``unknown``: the goal was not stated, which is what memory should record.

    Extraction is the same one the formatter uses, so both read one block the
    same way, and a goal can never be read out of text the formatter strips.
    """
    blocks, _ = output_formatter._extract_world_model(text)
    if not blocks:
        return "unknown"
    hit = re.search(
        r"goal[^.\n]{0,80}?\b(Information|Understanding|Action)\b|"
        r"\b(Information|Understanding|Action)\b\s*[—:-]",
        blocks[0],
        re.IGNORECASE,
    )
    if not hit:
        return "unknown"
    return (hit.group(1) or hit.group(2)).lower()


def _on_transform_llm_output(
    response_text: str = "",
    **_: Any,
) -> Optional[str]:
    """Format the LLM output: simulation summary + final answer."""
    if not _state.enabled or not response_text:
        return None

    # Strip guide blocks first so goal type detection is accurate
    cleaned_for_goal = re.sub(
        r"\[world_model_guide\].*?\[/world_model_guide\]",
        "",
        response_text,
        flags=re.DOTALL,
    ).strip()

    # Save detected goal pattern to Mnemosyne if available
    if MNEMOSYNE_AVAILABLE and _state.memory_enabled and _state._current_user_message:
        try:
            goal_type = _detect_goal_type(cleaned_for_goal)
            remember(
                content=_state._current_user_message,
                importance=0.7,
                source="doga_goal",
                metadata={"goal_type": goal_type, "depth": _state.depth},
            )
        except Exception:
            logger.debug("doga memory save failed", exc_info=True)

    formatted = output_formatter.format_response(
        response_text,
        show_simulation=_state.show_simulation,
        active_hats=_state._active_hats,
    )
    return formatted if formatted != response_text else None


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    **_: Any,
) -> None:
    """Track tool usage for logging and recursion state."""
    if tool_name == "simulate" and isinstance(result, str):
        logger.debug(
            "doga simulate tool called with args=%s, result_len=%d",
            args,
            len(result),
        )
    elif tool_name == "reason_deeper" and isinstance(args, dict):
        _state._recursion_depth += 1
        _state._reasoning_stack.append({
            "level": _state._recursion_depth,
            "focus": args.get("focus", ""),
        })
        logger.debug(
            "doga reason_deeper called (level %d/%d, focus=%s)",
            _state._recursion_depth,
            _state.max_recursion,
            args.get("focus", ""),
        )

# ---------------------------------------------------------------------------
# Tool: simulate
# ---------------------------------------------------------------------------

_SIMULATE_SCHEMA = {
    "name": "simulate",
    "description": (
        "Run a Monte Carlo simulation over probabilistic scenarios. "
        "Provide scenarios with variable probabilities and optional logical conditions. "
        "Returns probability distribution and uncertainty metrics. "
        "Use this when you need to quantitatively weigh multiple uncertain factors."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scenarios": {
                "type": "array",
                "description": "List of scenarios to simulate.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short label for this scenario (e.g. 'contract_valid').",
                        },
                        "variables": {
                            "type": "object",
                            "description": (
                                "Variable name → probability (0.0–1.0). "
                                "Each variable represents an independent binary factor. "
                                "Example: {\"signature_ok\": 0.8, \"duress\": 0.1}"
                            ),
                            "additionalProperties": {"type": "number"},
                        },
                        "conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional Python boolean expressions that must be True "
                                "for this scenario to match. Variables use Python identifiers. "
                                'Example: ["signature_ok and not duress"]'
                            ),
                        },
                    },
                    "required": ["name", "variables"],
                },
            },
            "n_iterations": {
                "type": "integer",
                "description": "Number of Monte Carlo iterations (default 10000, max 50000).",
                "default": 10000,
                "minimum": 100,
                "maximum": 50000,
            },
        },
        "required": ["scenarios"],
    },
}


def _bounded_iterations(value: Any) -> Optional[int]:
    """Clamp a requested ``n_iterations`` to the schema's declared bounds.

    The schema declares ``minimum: 100`` and ``maximum: 50000`` and the model
    layer does not enforce either, so the handler enforces both. A value above
    the maximum is clamped, because a larger request is the same request done
    more expensively. A value below the minimum is rejected, because it cannot
    be clamped into meaning: one iteration yields probability 1.0 for whatever
    that iteration sampled, and zero or a negative count divides by zero or
    yields a negative ``total_iterations``. A non-integer is rejected for the
    same reason: a float silently floors, and a string does not.

    Returns ``None`` when the value must be refused rather than clamped.
    """
    minimum, maximum = 100, 50000
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum:
        return None
    return min(value, maximum)


def _simulate_tool_handler(args: Any, **kwargs: Any) -> str:
    """Handle the simulate tool call."""
    if _state._stop_sent:
        return json.dumps({"stop": True, "message": "Recursion limit reached. Produce your final answer."})
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if isinstance(args, str) else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": "Invalid tool arguments: expected JSON object."})
    scenarios = args.get("scenarios", [])
    n_iterations = _bounded_iterations(args.get("n_iterations", 10000))
    if n_iterations is None:
        # The documented shape is an error, because the schema declares the
        # bounds. Silently clamping would run a one-sample "simulation" that
        # reports probability 1.0 for whatever the single sample picked, or a
        # negative iteration count that the engine happily divides by.
        return json.dumps({
            "error": "n_iterations must be an integer between 100 and 50000.",
        })

    # Cap scenarios
    if len(scenarios) > _state.max_scenarios:
        scenarios = scenarios[:_state.max_scenarios]

    # Cap scenarios
    if len(scenarios) > _state.max_scenarios:
        scenarios = scenarios[:_state.max_scenarios]

    if not scenarios:
        return json.dumps({"error": "No scenarios provided."})

    try:
        result = simulation_engine.run_scenarios(
            scenarios,
            n_iterations=n_iterations,
        )
        return json.dumps(result)
    except Exception as exc:
        logger.warning("simulate tool failed: %s", exc)
        return json.dumps({
            "error": f"Simulation failed: {exc}",
            "scenarios": [],
            "summary": {"total_iterations": 0},
        })


def _check_simulate_requirements() -> bool:
    """simulate tool has no special requirements — always available."""
    return True

# ---------------------------------------------------------------------------
# Tool: reason_deeper (Phase 3 — recursive reasoning)
# ---------------------------------------------------------------------------

_REASON_DEEPER_SCHEMA = {
    "name": "reason_deeper",
    "description": (
        "Recursively deepen your own reasoning. Call this after your initial "
        "<world_model> analysis to critique and refine your thinking. "
        "Each call asks you to examine what you missed, consider edge cases, "
        "and produce a deeper analysis. Stop calling when analysis is thorough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": (
                    "What specific aspect to dig deeper into. "
                    "e.g., 'black hat risk cascade', 'yellow hat assumption', "
                    "'green hat alternative we overlooked'"
                ),
            },
        },
        "required": ["focus"],
    },
}


def _reason_deeper_handler(args: Any, **kwargs: Any) -> str:
    """Handle reason_deeper tool call — triggers next recursion level.

    A stop is enforced twice and neither half is a hard guarantee. ``_stop_sent``
    gates ``simulate`` and ``reason_deeper`` from the first ignored stop, which
    is what actually ends a turn. ``hard_break`` on the third ignored stop is
    advisory only — a stronger wording of the same instruction, not a different
    mechanism, and the model decides whether to keep calling. Nothing here can
    force the loop closed; the real backstop is Hermes' own ``max_iterations``
    on the tool-calling loop.
    """
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if isinstance(args, str) else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": "Invalid tool arguments."})

    focus = args.get("focus", "general")
    current_level = _state._recursion_depth

    if current_level >= _state.max_recursion or _state._stop_sent:
        _state._stop_sent = True
        _state._stop_count += 1
        _state._reasoning_stack.clear()
        if _state._stop_count >= 3:
            return json.dumps({
                "stop": True,
                "hard_break": True,
                "message": (
                    f"Maximum recursion depth ({_state.max_recursion}) reached. "
                    "Produce your final synthesized answer now."
                ),
            })
        return json.dumps({
            "stop": True,
            "message": (
                f"Maximum recursion depth ({_state.max_recursion}) reached. "
                "Produce your final synthesized answer now."
            ),
        })

    next_level = current_level + 1
    recursion_hats = de_bono_hats.hats_for_recursion_level(next_level)

    hat_lines = "\n".join(f"  [{h.upper()}] {de_bono_hats._HAT_DEFS[h]}"
                         for h in recursion_hats)

    instruction = (
        f"--- RECURSION LEVEL {next_level} ---\n"
        f"Focus: {focus}\n\n"
        "Critique your previous analysis. What did you miss?\n"
        "Consider edge cases, hidden assumptions, and alternative interpretations.\n"
        f"Use these parallel thinking lenses:\n"
        f"{hat_lines}\n\n"
        f"Produce a new <world_model> section for Level {next_level}.\n"
        "When done, call `reason_deeper` again to go deeper, "
        "or produce your final answer if the analysis is thorough."
    )

    return json.dumps({"continue": True, "instruction": instruction})


def _check_reason_deeper_requirements() -> bool:
    """reason_deeper has no special requirements."""
    return True

# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

_DOGA_HELP = """\
/doga — probabilistic DOGA thinking controller

Subcommands:
  on                   Enable DOGA thinking (default)
  off                  Disable DOGA thinking
  status               Show current settings
  auto                 Automatic depth (default — decides low/medium/high per query)
  manual low|medium|high  Force a specific thinking level
  depth <1-5>          Set depth manually (switches to manual mode)
  hats on              Enable De Bono Six Thinking Hats (default)
  hats off             Disable De Bono parallel thinking lenses
  max_recursion <1-5>  Max recursion depth for reason_deeper tool (default: 3)
  show                 Show simulation panel in responses
  hide                 Hide simulation panel (only final answer)
  memory on           Enable Mnemosyne goal memory (requires pip install mnemosyne-memory)
  memory off          Disable Mnemosyne goal memory
  jev on|off          Legacy alias: enable or disable response contracts
  mode <mode>          Select one of the four response-contract modes below
  provider jev|clef|laya   Legacy alias: select the hosted API provider or the local slot
  fallback on|off     Legacy alias: change the local error fallback

Response contract modes:
  api_with_local_fallback   Hosted API first, local model on its failure
  api_only                  Hosted API only, failure reported
  local_only                Local model only, failure reported
  local_with_api_fallback   Local model first, hosted API on its failure
  Mode aliases: auto, jev_api, typesafe, openrouter, clef, clef_api,
  clef_with_local_fallback, laya, laya_local, laya_then_hosted,
  laya_with_jev_fallback

Both sides are System One decision models (also called typed decision models):
Jev, Clef and Clef Flash, Laya, Kev, and Tev1. Jev is one vendor's member of
that category, not the category. See
https://systemonemodels.org/guides/what-is-a-system-one-model/

Current state: {state}
"""

_MODE_USAGE = "Usage: /doga mode " + response_contract.accepted_mode_names()


def _handle_doga(raw_args: str) -> Optional[str]:
    """Handle /doga slash command."""
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _DOGA_HELP.format(state=_state.to_dict())

    sub = argv[0].lower()

    if sub == "on":
        _state.set(enabled=True)
        return "DOGA thinking enabled."

    if sub == "off":
        _state.set(enabled=False)
        return "DOGA thinking disabled."

    if sub == "status":
        mem_status = "available" if MNEMOSYNE_AVAILABLE else "not installed"
        if _state.auto_depth:
            mode = f"auto (complexity: {_state._last_complexity})"
        else:
            mode = f"manual (depth: {_state.depth}/5)"
        hat_status = "enabled" if _state.de_bono_enabled else "disabled"
        # The resolved provider is named, not only the mode. ``api_only`` is
        # silent about which vendor it means, and a mode that silently stays on
        # a pinned vendor is one way every request goes to a second paid route.
        return (
            "DOGA status:\n"
            f"  Enabled: {_state.enabled}\n"
            f"  Mode: {mode}\n"
            f"  Response contracts: {'enabled' if _state.jev_enabled else 'disabled'} (mode: {_state.decision_mode}, provider: {_state.decision_provider}, last: {_state._last_jev_status})\n"
            f"  Local model: {_state.local_model_name()}\n"
            f"  Show simulation: {_state.show_simulation}\n"
            f"  Max scenarios: {_state.max_scenarios}\n"
            f"  De Bono hats: {hat_status}\n"
            f"  Max recursion: {_state.max_recursion}\n"
            f"  Memory: {_state.memory_enabled} ({mem_status})"
        )

    if sub == "auto":
        _state.set(auto_depth=True)
        return f"DOGA set to auto mode (complexity: {_state._last_complexity})."

    if sub == "manual":
        if len(argv) < 2:
            return "Usage: /doga manual low|medium|high"
        level = argv[1].lower()
        mapping = {"low": 1, "medium": 3, "high": 5}
        if level not in mapping:
            return "Level must be low, medium, or high."
        _state.set(auto_depth=False, depth=mapping[level])
        return f"DOGA set to manual {level} (depth: {_state.depth}/5)."

    if sub == "depth":
        if len(argv) < 2:
            return f"Current depth: {_state.depth}/5\nUsage: /doga depth <1-5>"
        try:
            d = int(argv[1])
            if d < 1 or d > 5:
                return "Depth must be between 1 and 5."
            _state.set(auto_depth=False, depth=d)
            return f"DOGA depth set to {d}/5 (manual mode)."
        except ValueError:
            return "Invalid number. Use /doga depth <1-5>."

    if sub == "show":
        _state.set(show_simulation=True)
        return "DOGA simulation panel will be shown in responses."

    if sub == "hide":
        _state.set(show_simulation=False)
        return "DOGA simulation panel hidden. Only final answer will be shown."

    if sub == "hats":
        if len(argv) < 2:
            return f"De Bono hats: {'enabled' if _state.de_bono_enabled else 'disabled'}\nUsage: /doga hats on|off"
        h = argv[1].lower()
        if h == "on":
            _state.set(de_bono_enabled=True)
            return "De Bono parallel thinking hats enabled."
        elif h == "off":
            _state.set(de_bono_enabled=False)
            _state._active_hats = []
            return "De Bono parallel thinking hats disabled."
        return "Usage: /doga hats on|off"

    if sub == "max_recursion":
        if len(argv) < 2:
            return f"Current max recursion: {_state.max_recursion}\nUsage: /doga max_recursion <1-5>"
        try:
            r = int(argv[1])
            if r < 1 or r > 5:
                return "Max recursion must be between 1 and 5."
            # The stop latch is keyed to the limit that set it, so raising the
            # limit has to clear it: a latch left over from the old, lower limit
            # kept gating ``simulate`` and ``reason_deeper`` for the rest of the
            # turn, which made the new limit take effect only on the next turn.
            _state.reset_recursion_stop()
            _state.set(max_recursion=r)
            return f"DOGA max recursion set to {r}."
        except ValueError:
            return "Invalid number. Use /doga max_recursion <1-5>."

    if sub == "mode":
        if len(argv) != 2:
            return _MODE_USAGE
        # Non-strict: an unusable name here leaves the current mode alone.
        if not _state._apply_mode(argv[1], strict=False):
            return _MODE_USAGE
        _state._last_jev_status = "enabled"
        return (
            f"DOGA response contract mode: {_state.decision_mode} "
            f"(provider: {_state.decision_provider})."
        )

    if sub == "provider":
        if len(argv) != 2 or argv[1].lower() not in response_contract.PROVIDERS:
            return "Usage: /doga provider " + "|".join(response_contract.PROVIDERS)
        # Selecting a legacy provider resets the fallback, as it always did.
        if not _state._apply_mode(argv[1].lower(), fallback=False, strict=False):
            return "Usage: /doga provider " + "|".join(response_contract.PROVIDERS)
        _state._last_jev_status = "enabled"
        return (
            f"DOGA response contract mode: {_state.decision_mode} "
            f"(provider: {_state.decision_provider})."
        )

    if sub == "fallback":
        if len(argv) != 2 or argv[1].lower() not in {"on", "off"}:
            return "Usage: /doga fallback on|off"
        if _state.decision_provider != response_contract.LOCAL_PROVIDER:
            return ("API fallback is only available with the local model. "
                    "Select /doga mode local_with_api_fallback.")
        if not _state._apply_mode(response_contract.LOCAL_PROVIDER,
                                 fallback=argv[1].lower() == "on", strict=False):
            return "Usage: /doga fallback on|off"
        return (
            f"DOGA response contract mode: {_state.decision_mode} "
            f"(provider: {_state.decision_provider})."
        )

    if sub == "jev":
        if len(argv) < 2:
            return f"Jev: {'enabled' if _state.jev_enabled else 'disabled'}\nUsage: /doga jev on|off"
        setting = argv[1].lower()
        if setting == "on":
            _state.set(jev_enabled=True)
            _state._last_jev_status = "enabled"
            return "DOGA response contracts enabled."
        if setting == "off":
            _state.set(jev_enabled=False)
            _state._last_jev_status = "disabled"
            return "DOGA response contracts disabled."
        return "Usage: /doga jev on|off"

    if sub == "memory":
        if len(argv) < 2:
            return f"Memory: {'enabled' if _state.memory_enabled else 'disabled'} ({'available' if MNEMOSYNE_AVAILABLE else 'not installed'})\nUsage: /doga memory on|off"
        m = argv[1].lower()
        if m == "on":
            if not MNEMOSYNE_AVAILABLE:
                return "Mnemosyne is not installed. Run: pip install mnemosyne-memory"
            _state.set(memory_enabled=True)
            return "DOGA goal memory enabled."
        elif m == "off":
            _state.set(memory_enabled=False)
            return "DOGA goal memory disabled."
        return "Usage: /doga memory on|off"

    return f"Unknown subcommand: {sub}\n\n{_DOGA_HELP.format(state=_state.to_dict())}"

# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

    ctx.register_tool(
        name="simulate",
        toolset="doga",
        schema=_SIMULATE_SCHEMA,
        handler=_simulate_tool_handler,
        check_fn=_check_simulate_requirements,
        description="Run Monte Carlo probability simulations over scenarios.",
        emoji="🎲",
    )

    ctx.register_tool(
        name="reason_deeper",
        toolset="doga",
        schema=_REASON_DEEPER_SCHEMA,
        handler=_reason_deeper_handler,
        check_fn=_check_reason_deeper_requirements,
        description="Recursively deepen reasoning via self-critique.",
        emoji="🔄",
    )

    ctx.register_command(
        "doga",
        handler=_handle_doga,
        description="Control DOGA probabilistic thinking.",
        args_hint="on|off|status|mode " + "|".join(response_contract.MODES) + "|auto|manual low|medium|high|depth <1-5>|hats on|off|max_recursion <1-5>|show|hide|memory on|off|jev on|off",
    )
