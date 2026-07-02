"""Tests for chain.py's chain-leak guard.

The guard — ``chain._reject_project_inside_repo`` — refuses any ``--project``
that resolves inside ``REPO_DIR``. It runs from ``__main__`` immediately
after ``--project`` has been resolved to an absolute path and validated as a
directory, but before any tmux session, registry write, or coordination
state is touched.

Motivation: chains accidentally launched with ``--project`` pointing at
this repo itself leave ``[Chain: ...]`` commits and ``.dialectic-*``
scratch dirs in the tool's own tree; the guard rejects the launch before
side effects land.

Coverage here:

- Direct call on the helper (``_reject_project_inside_repo``): exits with
  code 2 for REPO_DIR itself and for a subdirectory under REPO_DIR; returns
  without side effects for a path outside REPO_DIR.
- Full ``__main__`` exercise via ``runpy``: passing ``--project`` at
  REPO_DIR exits 2, logs an actionable error, and never touches tmux /
  registry.
- Sanity: a ``--project`` pointing at ``tmp_path`` (i.e., outside the repo)
  proceeds past the guard (reaches the tmux boot sentinel).
"""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import chain
import registry as reg


REPO_DIR = Path(chain.__file__).parent.resolve()
CHAIN_PY = REPO_DIR / "chain.py"


# --- Direct helper: _reject_project_inside_repo -----------------------------


def test_reject_project_inside_repo_refuses_repo_dir(caplog):
    """Passing REPO_DIR itself must exit 2."""
    with caplog.at_level(logging.ERROR, logger="chain"):
        with pytest.raises(SystemExit) as excinfo:
            chain._reject_project_inside_repo(REPO_DIR)
    assert excinfo.value.code == 2
    assert any(
        "dialectic repo" in r.message for r in caplog.records if r.name == "chain"
    ), "Error message should name the dialectic repo so the operator can course-correct."


def test_reject_project_inside_repo_refuses_subdir():
    """A subdirectory under REPO_DIR (even a nested one) must also exit 2.

    The chain-leak pattern has surfaced with chains targeting things like
    ``REPO_DIR / "chainwork"`` or ``REPO_DIR / "tests"``. Any path below
    REPO_DIR is out-of-bounds, not just REPO_DIR itself.
    """
    sub = REPO_DIR / "tests"
    assert sub.is_dir(), "Fixture assumption: tests/ lives inside REPO_DIR."
    with pytest.raises(SystemExit) as excinfo:
        chain._reject_project_inside_repo(sub)
    assert excinfo.value.code == 2


def test_reject_project_inside_repo_allows_outside_path(tmp_path):
    """A path fully outside REPO_DIR must return silently.

    ``tmp_path`` is always outside REPO_DIR (pytest puts it under the OS
    temp dir). The helper must not raise, not log, and not sys.exit.
    """
    # Resolve to match how __main__ calls the helper.
    outside = tmp_path.resolve()
    assert not outside.is_relative_to(REPO_DIR), (
        "Fixture assumption: pytest's tmp_path must be outside REPO_DIR."
    )
    # No raise: helper returns None for OK paths.
    assert chain._reject_project_inside_repo(outside) is None


def test_reject_project_inside_repo_refuses_repo_parent_chain_leak_scratch(tmp_path):
    """A ``.dialectic-*`` scratch dir under REPO_DIR still counts as inside.

    This is the exact surface the chain-leak exposes: if a chain is started
    with ``--project`` somehow resolving to one of the scratch dirs this
    repo leaves behind, the guard must still catch it.
    """
    # Construct a plausible scratch-dir path; we don't need it to exist
    # on disk — is_relative_to works on path strings.
    scratch = REPO_DIR / ".dialectic-a-deadbeef"
    with pytest.raises(SystemExit) as excinfo:
        chain._reject_project_inside_repo(scratch)
    assert excinfo.value.code == 2


# --- Full __main__ exercise -------------------------------------------------


@pytest.fixture
def isolated_registry(tmp_path):
    """Redirect registry.REGISTRY_FILE / WORKSPACE to tmp_path for one test.

    Mirrors the pattern in ``test_cli_max_chains.py`` so a guard failure
    here cannot accidentally pollute the real registry file.
    """
    with patch.object(reg, "REGISTRY_FILE", tmp_path / ".registry.json"), \
         patch.object(reg, "WORKSPACE", tmp_path):
        yield tmp_path


def _run_chain_main(argv):
    saved_argv = sys.argv
    sys.argv = list(argv)
    try:
        runpy.run_path(str(CHAIN_PY), run_name="__main__")
        return 0
    except SystemExit as e:
        return e.code if e.code is not None else 0
    finally:
        sys.argv = saved_argv


def test_main_refuses_project_equal_to_repo(isolated_registry, caplog):
    """``--project <REPO_DIR>`` exits 2 before any subprocess / registry write."""
    with caplog.at_level(logging.ERROR, logger="chain"):
        # subprocess.run is patched so that if the guard failed, the test
        # would reach tmux boot and we'd see the sentinel fire. The fact
        # that the guard exits 2 first is what we're asserting.
        with patch("subprocess.run", side_effect=RuntimeError("tmux-reached")):
            code = _run_chain_main([
                "chain.py", "seed", "--project", str(REPO_DIR),
            ])
    assert code == 2, (
        "--project pointed inside the repo should exit 2, not reach the "
        "tmux / registry boot path."
    )
    messages = [r.message for r in caplog.records if r.name == "chain"]
    joined = "\n".join(messages)
    assert "dialectic repo" in joined, (
        f"Expected a 'dialectic repo' message in the error, got: {messages!r}"
    )


def test_main_refuses_project_subdir_of_repo(isolated_registry, caplog):
    """``--project <REPO_DIR>/tests`` also exits 2."""
    sub = REPO_DIR / "tests"
    with caplog.at_level(logging.ERROR, logger="chain"):
        with patch("subprocess.run", side_effect=RuntimeError("tmux-reached")):
            code = _run_chain_main([
                "chain.py", "seed", "--project", str(sub),
            ])
    assert code == 2


def test_main_allows_project_outside_repo(isolated_registry, tmp_path):
    """A clean ``--project <tmp_path>`` must pass the guard.

    We prove the guard did not fire by observing that execution reaches
    ``subprocess.run`` (first call is tmux kill-session in ``setup_tmux``),
    at which point our sentinel raises. If the guard had fired, the exit
    would have been 2 instead of the sentinel.
    """
    proj = tmp_path / "outside-proj"
    proj.mkdir()
    sentinel = RuntimeError("past-leak-guard")
    with patch("subprocess.run", side_effect=sentinel):
        with pytest.raises(RuntimeError, match="past-leak-guard"):
            _run_chain_main([
                "chain.py", "seed", "--project", str(proj),
            ])


def test_main_default_cwd_still_guarded_when_cwd_is_repo(isolated_registry, caplog):
    """Without ``--project``, cwd is the default. If cwd resolves to REPO_DIR
    (the common misconfiguration), the guard must still fire.

    The test runs its subprocess with cwd=REPO_DIR implicitly — pytest is
    launched from the repo root — so ``Path.cwd()`` resolves to REPO_DIR
    and the guard should catch the missing-``--project`` case the same way
    it catches an explicit mistake.
    """
    import os
    saved_cwd = os.getcwd()
    os.chdir(str(REPO_DIR))
    try:
        with caplog.at_level(logging.ERROR, logger="chain"):
            with patch("subprocess.run", side_effect=RuntimeError("tmux-reached")):
                code = _run_chain_main(["chain.py", "seed"])
    finally:
        os.chdir(saved_cwd)
    assert code == 2, (
        "Guard must fire when cwd defaults to REPO_DIR, not just on "
        "an explicit --project."
    )


# --- Regression fence: guard runs before registry mutation -----------------


def test_main_leaks_no_registry_entry_on_rejection(isolated_registry, tmp_path):
    """A rejected launch must not write anything to the registry.

    The guard runs before ``count_active_chains`` / ``register_chain`` /
    any other registry write. After a rejection, the registry JSON should
    either be missing or contain no chains.
    """
    with patch("subprocess.run", side_effect=RuntimeError("tmux-reached")):
        code = _run_chain_main([
            "chain.py", "seed", "--project", str(REPO_DIR),
        ])
    assert code == 2

    # The isolated registry file lives under tmp_path. If the guard held
    # the line, no write should have landed.
    chains = reg.list_chains()
    assert chains == [], (
        f"Rejected launch must not write to the registry; got: {chains!r}"
    )
