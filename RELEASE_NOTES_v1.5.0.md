# doga-jev-hermes v1.5.0

Two ways the plugin failed hard on ordinary bad input. Both ran on the ordinary
path — the regex on every assistant response, the mode resolution once at plugin
start — and neither was reachable from a happy-path test, which is why 340
passing tests carried both.

| | v1.4.1 | v1.5.0 |
| --- | --- | --- |
| test suite | 340 passed | **355 passed** |

---

## The world-model regex took 18.7 seconds on one malformed reply

`output_formatter._WORLD_MODEL_RE` ran over every assistant response:

```python
_WORLD_MODEL_RE = re.compile(
    r"<world_model>\s*(.*?)\s*</world_model>",
    re.DOTALL | re.IGNORECASE,
)
```

On an **unclosed** tag — a truncated response, a model that opened the block and
stopped mid-thought — the engine retried the greedy whitespace prefix from every
offset. That reads like a linear scan and is not one. Measured on a whitespace
run after an unclosed tag:

| input | before | after |
| --- | --- | --- |
| 400 spaces | 38.8 ms | 0.008 ms |
| 800 spaces | 309.7 ms | 0.011 ms |
| 1,600 spaces | 2,420 ms | 0.018 ms |
| 3,200 spaces | 18,676 ms | 0.035 ms |
| 12,800 spaces | ~2 min (extrapolated) | 0.142 ms |

Eight milliseconds more for each doubling of the input. The same lengths with
letters instead of spaces cost 0.01–0.08 ms, which is what points at the prefix
rather than the capture as the cost.

**What changed.** Both `\s*` are gone. The captured text is unchanged because
line 52 already strips it, so the surrounding whitespace never reached a caller —
the fix is behaviour-preserving rather than merely faster, and
`test_the_captured_text_is_unchanged` asserts that on three shapes including the
interior newlines the old `\s` would have eaten.

`_FALLBACK_WM_RE` has the same leading `\s*` and was **not** changed: measured on
identical inputs it costs 0.01 ms, because its alternation ends in `$` and never
re-enters the greedy prefix. Changing a pattern that is already fast is risk
without benefit.

The performance test pins the *shape* and not only a ceiling — a 16× larger input
must not cost more than 50× the time — because that is what distinguishes a bound
that was tightened from one that was merely raised.

---

## `DOGA_DECISION_MODE=auto` with an unusable local model broke `import doga`

`resolve_mode("auto")` calls `_local_model_usable()`, which calls
`local_model()`. `local_model()` raises `RuntimeError` for an empty or
ill-formed `DOGA_LOCAL_MODEL`, and that call was unguarded:

```python
def _local_model_usable() -> bool:
    if not local_model().strip():      # <-- raises RuntimeError
        return False
```

`_apply_mode` catches only `ModeError`, so the `RuntimeError` propagated out of
module scope. Verified with the env set either way:

```
DOGA_DECISION_MODE=auto DOGA_LOCAL_MODEL=""        import doga -> RuntimeError
DOGA_DECISION_MODE=auto DOGA_LOCAL_MODEL="bad name" import doga -> RuntimeError
```

The plugin fails to load and **every slash command with it**. `auto` is the mode
a user picks precisely when they do not want to think about providers, which
makes it the worst mode to break on a configuration slip.

**What changed.** A malformed local model answers `False` from
`_local_model_usable()`, so `auto` resolves to the hosted route and the plugin
loads. The warning names the offending variable and where the route landed:

```
local decision route unavailable: DOGA_LOCAL_MODEL is set but empty; unset it to
use the default; DOGA_DECISION_MODE resolves to api_only
```

**The trade-off is deliberate and worth stating plainly.** A misconfiguration now
degrades instead of erroring, which means it is quiet. The alternative — the
previous behaviour — took the entire plugin down over one mistyped variable, so
the quiet failure is the better one, and it is logged rather than swallowed. A
well-formed, importable engine id still selects the local side, and an explicit
mode never consults the local slot at all; both are pinned.

---

## Verification

```
340 passed  ->  355 passed
ruff check --select F .   All checks passed
```

The 15 new tests are in `tests/test_hard_input_regressions.py`: the quadratic
shape and the captured-text equivalence, extraction behaviour on zero, one, and
two blocks, the unclosed-tag fallback keeping its content, the import surviving
four malformed model values, the warning naming the variable, and the two
guards against over-correction.
