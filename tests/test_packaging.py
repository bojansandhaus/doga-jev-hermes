"""Regression test for the build backend this fork already repaired.

Upstream declared ``build-backend = "setuptools.backends._legacy:_Backend"``,
a private module. setuptools 84.0.0 removed it, so installing this package
failed with::

    ModuleNotFoundError: No module named 'setuptools.backends'

This fork moved to the public ``setuptools.build_meta`` backend. The test reads
the declared backend out of pyproject.toml rather than importing it, so it
asserts on what a consumer of the repository would resolve, and it fails on the
exact string that regressed rather than on any incidental detail.

pyproject.toml is read with narrow regular expressions rather than tomllib,
because tomllib is Python 3.11 or newer and this project's test matrix starts
at 3.10. Every value asserted here has its own assertion, so a narrow read
cannot hide a regression.
"""
import pathlib
import re

import pytest

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_text() -> str:
    return PYPROJECT.read_text()


def _quoted(key: str) -> str:
    """The last double quoted value for a key, which is the one in effect."""
    matches = re.findall(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', _pyproject_text(), re.MULTILINE)
    assert matches, f"{key} is not declared in pyproject.toml"
    return matches[-1]


def test_build_backend_is_the_public_setuptools_entry_point():
    """The backend must be importable from every supported setuptools.

    ``setuptools.backends._legacy`` is private API and was removed in setuptools
    84.0.0, which broke installation outright.
    """
    backend = _quoted("build-backend")
    assert backend == "setuptools.build_meta", (
        "build-backend must be the public setuptools.build_meta; "
        f"found {backend!r}"
    )


def test_build_backend_does_not_reference_a_private_setuptools_module():
    """No private setuptools submodule may come back through the back door."""
    backend = _quoted("build-backend")
    assert "setuptools.backends" not in backend
    assert "_legacy" not in backend


def test_build_backend_module_is_importable_in_this_interpreter():
    """Import the declared backend rather than trusting the string.

    The backend may be spelled ``module`` or ``module:object``. A plain module
    name has no object part, so only the object is checked when one is given.
    Skipped when setuptools is absent, because a build resolves its build
    requirements in an isolated environment and a bare test venv legitimately
    has no setuptools. The string assertions above are what actually pin the
    regression; this only confirms the entry point exists where it is present.
    """
    import importlib

    module_name, separator, attribute = _quoted("build-backend").partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        pytest.skip(f"{module_name} is not installed in this interpreter")
    if separator and attribute:
        assert hasattr(module, attribute), f"{module_name} has no {attribute}"
    else:
        # A bare module name must still be a PEP 517 backend, meaning it exposes
        # the mandatory build hooks.
        for hook in ("build_wheel", "build_sdist", "get_requires_for_build_wheel"):
            assert hasattr(module, hook), f"{module_name} is not a build backend: no {hook}"


def test_build_requires_a_setuptools_floor_that_ships_the_backend():
    """The declared floor must be one that provides setuptools.build_meta."""
    requires = re.findall(r'"([^"]+)"', _pyproject_text())
    setuptools_requirements = [
        item for item in requires
        if re.match(r"^setuptools\b", item)
    ]
    assert setuptools_requirements, f"no setuptools floor declared in {requires}"


def test_project_metadata_keeps_upstream_attribution():
    """The fork changed the URLs, never the author or the licence."""
    text = _pyproject_text()
    assert 'name = "0z1-ghb"' in text, "upstream author attribution was removed"
    assert re.search(r'^\s*license\s*=\s*\{\s*text\s*=\s*"MIT"\s*\}', text, re.MULTILINE), (
        "the MIT licence declaration was removed"
    )


def test_project_urls_point_at_this_fork():
    homepage = _quoted("Homepage")
    assert homepage == "https://github.com/bojansandhaus/doga-jev-hermes"