"""Regressions for two ways the plugin failed hard on ordinary bad input.

Both ran on the ordinary path: the regex on every assistant response, the mode
resolution once at plugin start. Neither was reachable from a happy-path test,
which is why 340 passing tests carried both.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from doga import output_formatter, response_contract  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The world-model regex backtracked for 18 seconds on one malformed reply
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spaces", [400, 800, 1600, 3200])
def test_an_unclosed_world_model_tag_does_not_backtrack(spaces: int) -> None:
    """`<world_model>\\s*(.*?)\\s*</world_model>` on an unclosed tag — a
    truncated response, a model that opened the block and stopped — retried the
    greedy whitespace prefix from every offset. Reads linear, is not.

    Measured before: 400 sp = 39 ms, 800 = 310 ms, 1600 = 2.42 s,
    3200 = 18.7 s, eight for one doubling. The same lengths with letters cost
    0.01–0.08 ms. This ran on every assistant response, so one malformed reply
    was an 18-second hang in front of the user.
    """
    text = "<world_model>" + " " * spaces
    started = time.perf_counter()
    output_formatter._WORLD_MODEL_RE.search(text)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 50, f"{spaces} spaces took {elapsed_ms:.1f} ms"


def test_the_quadratic_shape_is_gone_not_merely_smaller() -> None:
    """A 16x-larger input used to cost 480x the time. It now costs within a
    small constant of the time the 1x input costs, which is what linear looks
    like. Pinning the ratio rather than only a ceiling is what catches a bound
    that was raised instead of removed."""
    def cost(spaces: int) -> float:
        started = time.perf_counter()
        output_formatter._WORLD_MODEL_RE.search("<world_model>" + " " * spaces)
        return (time.perf_counter() - started) * 1000

    small, large = cost(200), cost(3200)
    assert large < small * 50, f"16x input took {large / small:.0f}x the time"


def test_the_captured_text_is_unchanged() -> None:
    """The `\\s*` on both sides was redundant: the caller already strips. Dropping
    it must not change a single extracted block, including the interior
    whitespace that `\\s` would previously have eaten."""
    for text, expected in [
        ("<world_model>  hello world  </world_model>", "hello world"),
        ("<world_model>\n  multi\n line \n</world_model>", "multi\n line"),
        ("<world_model>tight</world_model>", "tight"),
    ]:
        match = output_formatter._WORLD_MODEL_RE.search(text)
        assert match is not None, text
        assert match.group(1).strip() == expected, text


def test_extraction_behaviour_is_preserved() -> None:
    """The blocks and the remaining text are what every caller sees."""
    assert output_formatter._extract_world_model("a <world_model>  x  </world_model> b") == (
        ["x"], "a  b",
    )
    assert output_formatter._extract_world_model(
        "a <world_model>  x  </world_model> b <world_model>y</world_model> c"
    ) == (["x", "y"], "a  b  c")
    assert output_formatter._extract_world_model("no blocks here") == ([], "no blocks here")


def test_an_unclosed_tag_still_falls_back_without_data_loss() -> None:
    """The fallback path exists so a truncated response keeps its content. The
    regex change must not make an unclosed tag start swallowing text."""
    blocks, remaining = output_formatter._extract_world_model(
        "<world_model>the user still sees this"
    )
    assert blocks == [], blocks
    assert "the user still sees this" in remaining


# ---------------------------------------------------------------------------
# 2. `auto` with an unusable DOGA_LOCAL_MODEL broke `import doga`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "bad name", "a" * 200, "../etc/passwd"])
def test_a_malformed_local_model_degrades_instead_of_breaking_import(
    monkeypatch, value: str
) -> None:
    """`local_model()` raises RuntimeError for an empty or ill-formed engine id.
    `_local_model_usable()` called it unguarded, and `_apply_mode` catches only
    `ModeError`, so `DOGA_DECISION_MODE=auto` with an unusable value made
    `import doga` raise at module scope and took every slash command with it.

    A bad local model name is a reason to route hosted. It is not a reason the
    plugin cannot load.
    """
    monkeypatch.setenv("DOGA_DECISION_MODE", "auto")
    monkeypatch.setenv("DOGA_LOCAL_MODEL", value)

    assert response_contract._local_model_usable() is False
    # `auto` must resolve rather than raise, and must resolve to the hosted side.
    mode = response_contract.resolve_mode("auto")
    assert mode.name == "api_only", mode


def test_the_degradation_is_logged_not_swallowed(monkeypatch, caplog) -> None:
    """The trade-off is that a misconfiguration is now quiet. It is logged so it
    is not invisible, and this pins that the log names the offending variable
    and where the route landed."""
    monkeypatch.setenv("DOGA_LOCAL_MODEL", "bad name")
    with caplog.at_level("WARNING"):
        assert response_contract._local_model_usable() is False
    assert "DOGA_LOCAL_MODEL" in caplog.text
    assert "api_only" in caplog.text


def test_a_usable_local_model_is_still_recognised(monkeypatch) -> None:
    """The guard must not become an anything-goes gate: a well-formed engine id
    that is actually importable still selects the local side. The import is
    absent here, so the honest expectation is False via the ImportError branch,
    which is the same answer the pre-fix code gave for a missing engine."""
    monkeypatch.delenv("DOGA_LOCAL_MODEL", raising=False)
    monkeypatch.setattr(response_contract, "local_model", lambda: "Laya-7B")
    try:
        import laya  # noqa: F401

        importable = True
    except ImportError:
        importable = False
    assert response_contract._local_model_usable() is importable


def test_an_explicit_mode_is_unaffected(monkeypatch) -> None:
    """Only `auto` consults the local slot. A malformed model name must not
    change what an explicit mode resolves to."""
    monkeypatch.setenv("DOGA_LOCAL_MODEL", "bad name")
    for name in ("api_only", "local_only", "jev_api", "clef_api"):
        assert response_contract.resolve_mode(name).name in {
            "api_only", "local_only",
        }, name
