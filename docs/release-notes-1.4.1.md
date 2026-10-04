# DOGA Hermes v1.4.1 release notes

Release date: 2026-10-04. Previous release: v1.4.0.

This release changes the response-contract mode vocabulary and turns the local
classifier into a configurable slot. It is the next minor version, not a release
candidate: no release candidate was cut for this change.

## What changed

### The category is named in the documentation

The documentation now names the model category instead of describing it by its
best-known member. The category term is **System One decision model**, also
written *typed decision model*
([reference](https://systemonemodels.org/guides/what-is-a-system-one-model/)).
TypeSafe coined it on 15 September 2026 alongside Jev; it is also written
System 1. A member returns typed values (Choice, Score, Noul) with a probability
for each rather than prose. Jev is one vendor's member of the category, not the
category, so the docs never write "Jev-like model" as the category name.

Members named in the README: **Jev** (TypeSafe or OpenRouter, closed weights),
**Clef** and **Clef Flash** (Cloudflare Workers AI), **Laya** (local, open
weights, the default), **Kev** (open weights, 0.8B to 27B on Qwen3.5 and Qwen3.8
bases, serving the same `/v1/systemone` request shape as TypeSafe's API), and
named in the ecosystem index at [systemonemodels.org](https://systemonemodels.org/).

Other members catalogued in the same index: **CLM** and **GLiNER2.5-Decide** (open weights), plus hosted **d1** (Liquid AI), **Mercury Decide** (Inception, free on OpenRouter), **Solar Decide** (Upstage), **pplx-decider** (Perplexity), **Span-01** (Respan), **Decider 1** (meraGPT), and the **OpenAI Decisions API**.

This release also records the repository's GitHub topic tags, so the tags and the
documentation agree: `cloudflare`, `clef`, `laya`, `kev`, `tev1`, `system-one`,
`decision-model`.

This is a documentation change only. No mode, setting, provider route, help
string, or resolution rule changed with it.

### Four canonical modes

The plugin now exposes exactly four modes, named for which side answers and
whether the other side sits behind it as a fallback:

| Mode | Leads | Fallback | Provider order |
| --- | --- | --- | --- |
| `api_with_local_fallback` | Hosted API | Local model | hosted, then local |
| `api_only` | Hosted API | none | hosted |
| `local_only` | Local model | none | local |
| `local_with_api_fallback` | Local model | Hosted API | local, then hosted |

`api_only` and `local_only` are single-provider routes. A failure is reported and
never rerouted. `api_with_local_fallback` and `local_with_api_fallback` are
two-provider chains and use the existing cooldown, trigger, and breaker
machinery unchanged, including the suppression of remote fallback after three
consecutive local failures.

One deliberate limit is retained: a hosted failure is still never rerouted to a
*different* hosted provider. `api_with_local_fallback` tries the hosted route
and then the local slot; it never calls Jev on top of a Clef failure.

### An interchangeable local decision model

`DOGA_LOCAL_MODEL` selects which local System One decision model answers. It
defaults to `convaiinnovations/laya`, so the default path is unchanged, and its
value is the checkpoint or engine name sent to the local server. Switching
engines is a configuration change, with no code change and no new provider name;
the configuration name stays `laya`.

The value is not checked against an allowlist. Only a value that could not be
used safely is rejected: an empty or whitespace-only value, a value containing
whitespace, quotes, a backslash, a control character, or a URL separator, and a
`.` or `..` path segment. An unset variable takes the default; a variable that is
set but empty is an error rather than a silent default.

System One decision models documented as speaking the same `/v1/systemone`
contract and therefore fitting the slot: `laya` (also `laya-multilingual` and
`laya-typed-decisions`), `kev` (also `kev-0.8b`, 0.8B to 27B on Qwen3.5 and
Qwen3.8 bases), `tev1` (`Tev1-4B` and `Tev1-0.8B`), and `jeff-qwen3.5-0.8b` and
`jeff-gemma4-e2b`. Those membership and wire-contract claims come from each
project's own documentation; nothing in this list was confirmed by calling it
here. See [chaitin/Decis](https://github.com/chaitin/Decis) for the
interchangeable-engine reference, one Docker image per engine with `base_url` as
the whole migration, and [togethercomputer/tev1](https://github.com/togethercomputer/tev1)
for Tev1.

## What stayed compatible

Every mode name the plugin accepted before still selects the same routing
decision:

| Existing name | Resolves to |
| --- | --- |
| `auto` | `api_with_local_fallback` when a usable local model is configured, otherwise `api_only` |
| `jev`, `jev_api`, `typesafe`, `openrouter` | `api_only` on Jev |
| `clef`, `clef_api` | `api_only` on Clef |
| `clef_with_local_fallback` | `api_with_local_fallback` on Clef (newly added) |
| `laya`, `laya_local` | `local_only` |
| `laya_then_hosted`, `laya_with_jev_fallback` | `local_with_api_fallback` |

Also unchanged:

- `DOGA_DECISION_MODE` still takes precedence over `DOGA_DECISION_PROVIDER` and
  `DOGA_LAYA_JEV_FALLBACK`.
- The default configuration is still Jev with no fallback, now named
  `api_only`. No default was flipped.
- The legacy `/doga provider jev|clef|laya`, `/doga fallback on|off`, and
  `/doga jev on|off` commands still work, and selecting a legacy provider still
  resets the fallback.
- The per-model Clef setting `DOGA_CLEF_MODEL`, its two allowed values, the
  Cloudflare endpoint, and both Clef credential checks are untouched.
- No existing setting was removed and no setting had its meaning changed.

One observable difference: an alias is now reported as the canonical mode it
denotes. `/doga status` and a command reply say `local_only`, not
`laya_local`. The alias is still accepted and still routes identically; it just
no longer appears in output, which is what keeps the four canonical names the
single source of truth.

## Verification, and what was not verified

`python -m pytest tests -q` reports **307 passed**, up from 172 on v1.4.0. The
suite was additionally run with `DOGA_DECISION_MODE` set to
`local_with_api_fallback`, `clef_api`, and `api_only`, and with
`DOGA_LOCAL_MODEL` set to `kev` and `tev1`; it reported 307 passed in every one
of those runs. Each test file also passes in isolation. `git diff --check` is
clean.

**No live call was made to any provider for this release.** No local model other
than the default has been called live, ever. Every test uses a fake module
object, a monkeypatched function, or a socket fixture that fails the test if a
route attempts a real connection.

No credential available on the release machine is authorized for Cloudflare
Workers AI; every candidate returned HTTP 401 from `api.cloudflare.com`. That
limitation is unchanged from v1.4.0: all Clef behaviour is mocked, the wire
format follows Cloudflare's published model documentation, and the first real
call should be treated as unverified until one succeeds.

The only live behaviour recorded anywhere in this repository's history remains
what the earlier changelog entries state: local Laya inference with a cached
checkpoint under `HF_HUB_OFFLINE=1`, and a Jev request over HTTPS with a key
that has since been removed. `docs/benchmarks/` records the 100-question
evaluation of Jev and local Laya against authored labels, which is what motivates
keeping `api_only` as the recommended default.

## Upgrade notes

No action is required. An existing `DOGA_DECISION_MODE=jev_api`,
DOGA_DECISION_MODE=clef_api`, `DOGA_DECISION_MODE=laya_local`, or
`DOGA_DECISION_MODE=laya_with_jev_fallback` keeps working unchanged. To start
using a different local engine, set `DOGA_LOCAL_MODEL` and select `local_only`
or `local_with_api_fallback`.
