# Upstream sync

This repository is a fork of DOGA, written and maintained by @0z1-ghb.

- Upstream: https://github.com/0z1-ghb/doga-hermes
- Local remote name: `upstream`
- Origin: https://github.com/bojansandhaus/doga-jev-hermes

## Attribution

Attribution is not ours to change and is left intact:

- `LICENSE` carries `Copyright (c) 2026 0z1-ghb`.
- `README.md` credits @0z1-ghb as the DOGA author.
- `pyproject.toml` keeps `authors = [{ name = "0z1-ghb" }]`.

## What this fork changes

This fork adds work that upstream does not have: a Jev response-contract route
by default, Cloudflare Clef as a fourth route, four canonical routing modes, and
the packaging fix described below. The upstream project is DOGA itself, and this
fork builds on it rather than replacing it.

## Sync policy

1. Upstream is fetched, never pushed to. Do not force-push to `upstream`.
2. Before merging upstream work, rebase or merge `upstream/main` onto a branch
   and run the full test suite plus `ruff check --select F .`.
3. Keep the fork's own commits intact. Do not squash the fork's history into
   upstream commits to make the two look identical.
4. Watch for conflict in `doga/response_contract.py`, which this fork has
   modified in the same areas upstream touches.
5. Document any upstream change that is deliberately not taken, with the reason.

## Current position

At the time of writing:

```
$ git rev-list --left-right --count upstream/main...main
0	13
```

That is 0 commits behind and 13 ahead, so there is nothing to merge today.

## Packaging fix worth keeping

Upstream declared `build-backend = "setuptools.backends._legacy:_Backend"`,
which is private setuptools API. setuptools 84.0.0 removed it, so installing
this package failed with:

```
ModuleNotFoundError: No module named 'setuptools.backends'
```

This fork uses the public `setuptools.build_meta` backend. The regression test
in `tests/test_packaging.py` pins that, so the private backend cannot come back
silently through a future upstream merge.