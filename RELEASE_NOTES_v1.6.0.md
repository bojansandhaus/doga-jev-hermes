# doga-jev-hermes v1.6.0

Eight findings from a review of the fork, all on paths the happy-path tests do
not reach. Two of them cost money: a validation rejection that bought a second
billed call, and a `/doga provider` command that reported one vendor while
routing to another. Both are closed, and both are pinned by tests that fail on
the pre-fix behaviour rather than on the new code path.

| | v1.5.0 | v1.6.0 |
| --- | --- | --- |
| test suite | 355 passed | **387 passed** |

---

## A rejected answer bought a second billed call

`_request_jev` wrapped both routes in `except Exception`. OpenRouter answers 200
OK with unusable choices often enough — a choice outside its own criteria, a
probability that is a string — and `_validate_typed_answers` rejects those on
purpose. But the rejection is a `RuntimeError`, so it was caught by the same
handler that catches a connection timeout, appended to `errors`, and the call
fell through to `_request_typesafe` with the same user payload. Two vendors
billed for one request, and the second answer was no more trustworthy than the
first.

The failure modes now say different things, so the handler can tell them apart:

```python
class AnswerValidationError(RuntimeError):
    """A route answered 200 OK and its typed answers cannot be trusted.

    Distinct from a transport failure, because the two demand opposite
    responses. A transport failure is what the fallback exists for: the
    request did not reach a working model. A 200 with invalid choices means the
    request was already paid for and the answer is unusable, so the fallback
    would spend a second time to get a second answer of equally unknown
    provenance.
    """
```

`_request_jev` re-raises it on both branches and lets it escalate. A transport
failure still falls through, which is the documented behaviour and is pinned by
a test that asserts the fallback is still called for one. Everything that
previously caught `RuntimeError` still does: the new class is a subclass, so
the routes' own tests, `pytest.raises(RuntimeError)` calls, and the hook's
`except Exception` are unaffected.

## The TypeSafe error message carried 2,000 bytes of upstream response

`_request_typesafe` interpolated the response body into the error:

```python
detail = exc.read(2000).decode(errors="replace")
raise RuntimeError(f"TypeSafe API returned HTTP {exc.code}: {detail}") from exc
```

An upstream error page is exactly where a DSN, a signed URL, or an account
identifier appears, and that string reached any log that recorded the error.
OpenRouter and Cloudflare both log the status only, and the repo's own
documentation claims error-type-only logging. The body is no longer read, so
the claim is now true of all three routes. Two tests assert the secret is in
neither the raised message nor `caplog.text`, including through the combined
error `_request_jev` builds.

## `n_iterations` had a declared minimum and nothing enforced it

The schema declares `"minimum": 100, "maximum": 50000`. The model layer sends
neither, so `n=1` ran one sample and reported probability 1.0 for whatever it
picked, and `n=-5` produced `total_iterations: -5`. A value above the maximum is
still clamped — a bigger request is the same request done more expensively. A
value below the minimum is refused with the documented error shape, because it
cannot be clamped into meaning: one iteration is not a simulation, and zero or a
negative count divides by zero. Non-integers are refused for the same reason.

## The goal regex matched the taxonomy word anywhere in the response

```python
m = re.search(r"<world_model>.*?(Information|Understanding|Action)",
              cleaned_for_goal, re.DOTALL | re.IGNORECASE)
```

`re.DOTALL` lets `.*?` cross newlines, so the match ran from the first
`<world_model>` to the first taxonomy word anywhere in the rest of the text —
the final answer, an echoed question, or ordinary prose. Whatever word came
first was recorded as the goal in Mnemosyne and fed back into later turns.

Detection now reads the block the formatter already extracts, and only accepts a
goal statement: the word appears after the word *goal*, or is used as a label.
A bare word alone is not a goal, which is the behaviour the old pattern had and
the new one does not: a block that merely contains "Information" records
`unknown`. This is intentional and is pinned by a test. An unclosed tag is not a
block and records `unknown` too, because a truncated response is not a place to
read an intention out of.

## The settings singleton was written without a lock

Two writes were unguarded. `/_doga` mutated the global settings from whichever
thread served the slash command, and `_last_jev_status` was one process-wide
slot written from every turn. Two sessions running concurrently raced on both,
and a `/doga status` on a quiet session reported whatever a concurrent session
last wrote.

The mode state is now a locked read-modify-write, following the locked cache
already in `doga/simulation_engine.py`. The slash commands commit their
settings as one locked change through a single helper rather than field by
field. The status is per turn: a thread reports its own outcome, a thread with
no turn of its own reports the last attempt in the process, and a new turn
starts with no outcome so a failed turn's `error` cannot outlive it.

## Raising `max_recursion` did not clear the stop latch it had tripped

`_stop_sent` gates `simulate` and `reason_deeper`, and it was never reset by
`/_doga max_recursion`. Raising the limit from 1 to 5 mid-turn did nothing until
the next turn: the latch set by the old limit kept refusing the calls. The latch
and its count are cleared when the limit changes, so a raised limit takes effect
in the turn it was raised in.

`hard_break` is documented as advisory, because that is all it is. The wording
strengthens the instruction on the third ignored stop and nothing more — the
model still decides whether to call again. The backstop is Hermes' own
`max_iterations` on the tool-calling loop, and the docstring now says so
plainly instead of implying the flag forces anything.

## A pinned provider could survive into a mode that did not use it

`/doga provider clef` set a hosted pin, and a mode with no hosted side kept it
in reserve, so it came back silently on the next API-led selection — a route to
a second paid vendor that no current mode asked for. The pin is now dropped
when the mode resolves to no hosted provider, and every reply and `/doga status`
names the resolved provider, because `api_only` on its own does not say which
vendor it means.

The same path had the opposite bug, which the review's repro passed through:
`/doga provider clef` resolved the alias, then re-resolved the unqualified
canonical name against the *old* pin before storing the new one, so the command
reported `api_only` and routed to Jev. The pin is applied before the
re-resolution now, so the command does what it says.

## Two docs described settings that do not exist

`local_model()`'s docstring promised that the engine name is "interpolated
into" a URL path segment and embedded in a JSON body. The only consumer is
`laya.load(name)` in-process, so neither is true, and the README documented a
`base_url` setting and a "local server URL" that this plugin does not have —
`base_url` belongs to the Decis project cited alongside it. The README also
listed a URL separator among the rejected characters, which the pattern accepts
as a namespace separator. Both now describe the code: the name is one Python
argument, split on `/`, and nothing is sent anywhere.

---

## Kept working

- Every mode name, alias, and legacy setting selects the same routing decision
  as before. `/doga provider clef` now routes to Clef, which is what the
  command has always said it means.
- `tests/test_hard_input_regressions.py`, the v1.5.0 regressions, passes
  unchanged. The AST-whitelisted `eval()` in `doga/simulation_engine.py` was
  not touched.
- No live call was made to any provider. Every test uses a fake module object,
  a monkeypatched function, or a constructed error object.

## Verification

`python3 -m pytest -q` reports **387 passed**, up from 355. `ruff check
--select F .` reports all checks passed, which is the CI gate. Each test file
also passes in isolation.
