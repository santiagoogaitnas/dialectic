"""Tests for chain.py's CLI --max-chains guard.

The guard lives inside chain.py's __main__ block (between cmd_stop dispatch
and ChainConfig creation). If the number of running chains for the target
project is already at or above --max-chains, it logs an error and exits 1.

No function wraps this logic, so the tests exercise it via runpy.run_path
with sys.argv set. The registry is redirected to a tmp_path, and any tmux
subprocess calls are intercepted before they can boot real panes.
"""

import logging
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import registry as reg
from registry import ChainConfig


REPO_DIR = Path(__file__).parent.parent.resolve()
CHAIN_PY = REPO_DIR / "chain.py"


@pytest.fixture
def isolated_registry(tmp_path):
    """Redirect registry.REGISTRY_FILE / WORKSPACE to tmp_path for one test."""
    with patch.object(reg, "REGISTRY_FILE", tmp_path / ".registry.json"), \
         patch.object(reg, "WORKSPACE", tmp_path):
        yield tmp_path


def _run_chain_main(argv):
    """Invoke chain.py's __main__ block with a controlled sys.argv.

    Returns the SystemExit code if one was raised, or 0 if the block ran to
    completion. Non-SystemExit exceptions propagate so tests can assert them.
    """
    saved_argv = sys.argv
    sys.argv = list(argv)
    try:
        runpy.run_path(str(CHAIN_PY), run_name="__main__")
        return 0
    except SystemExit as e:
        return e.code if e.code is not None else 0
    finally:
        sys.argv = saved_argv


def _preload_running_chains(n, project=None):
    """Register n chains tied to this test process's PID (so they look 'running').

    When `project` is None, the preload mirrors chain.py's own invariant:
    every chain runs inside a project, and the default when ``--project``
    is omitted is ``Path.cwd()``. Preloading with project=None would
    leave the guard blind to the resolved cwd the test's chain.py call
    filters against, so the guard would never fire. Callers that want
    to model chains on a different project pass it explicitly (see
    test_max_chains_guard_counts_only_same_project).
    """
    if project is None:
        project = str(Path.cwd().resolve())
    for i in range(n):
        cfg = ChainConfig(
            chain_id=f"preload-{i}",
            session=f"preload-sess-{i}",
            seed="x",
            project=project,
        )
        reg.register_chain(cfg, os.getpid())


# --- Above-limit: guard exits 1 -----------------------------------------------


def test_max_chains_guard_fires_when_at_limit(isolated_registry, tmp_path, caplog):
    """count == limit should block: the guard uses >=, not >."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _preload_running_chains(3, project=str(proj.resolve()))
    with caplog.at_level(logging.ERROR, logger="chain"):
        code = _run_chain_main([
            "chain.py", "seed", "--project", str(proj), "--max-chains", "3",
        ])
    assert code == 1
    messages = [r.message for r in caplog.records if r.name == "chain"]
    assert any("Already 3" in m and "max: 3" in m for m in messages), (
        f"Expected guard error mentioning '3 / 3', got: {messages!r}"
    )


def test_max_chains_guard_fires_when_over_limit(isolated_registry, tmp_path, caplog):
    """If somehow count > limit (e.g. limit lowered), the guard still blocks."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _preload_running_chains(5, project=str(proj.resolve()))
    with caplog.at_level(logging.ERROR, logger="chain"):
        code = _run_chain_main([
            "chain.py", "seed", "--project", str(proj), "--max-chains", "2",
        ])
    assert code == 1
    messages = [r.message for r in caplog.records if r.name == "chain"]
    assert any("Already 5" in m for m in messages), (
        "Guard message should report the actual count (5), not just the limit."
    )


def test_max_chains_guard_message_is_actionable(isolated_registry, tmp_path, caplog):
    """Error should point the user at --list / --stop / --max-chains to recover."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _preload_running_chains(1, project=str(proj.resolve()))
    with caplog.at_level(logging.ERROR, logger="chain"):
        _run_chain_main([
            "chain.py", "seed", "--project", str(proj), "--max-chains", "1",
        ])
    combined = "\n".join(r.message for r in caplog.records if r.name == "chain")
    assert "--list" in combined
    assert "--stop" in combined
    assert "--max-chains" in combined


# --- Per-project filtering ----------------------------------------------------


def test_max_chains_guard_counts_only_same_project(isolated_registry, tmp_path):
    """Chains in a different project must not count against this project's limit.

    Pre-populate the registry with chains tied to /unrelated/project, then
    launch against a different project with --max-chains 1. The guard should
    pass because zero chains are active in the new project. We intercept the
    next step (tmux boot via subprocess.run) with a sentinel exception so the
    test does not spawn a real session.
    """
    _preload_running_chains(3, project="/unrelated/project")
    my_project = tmp_path / "mine"
    my_project.mkdir()

    sentinel = RuntimeError("boot-path-reached")
    with patch("subprocess.run", side_effect=sentinel):
        with pytest.raises(RuntimeError, match="boot-path-reached"):
            _run_chain_main([
                "chain.py", "seed",
                "--project", str(my_project),
                "--max-chains", "1",
            ])


def test_max_chains_guard_fires_on_same_project(isolated_registry, tmp_path, caplog):
    """Two chains on /proj plus --max-chains 2 on the same /proj must exit 1."""
    proj = tmp_path / "shared"
    proj.mkdir()
    _preload_running_chains(2, project=str(proj.resolve()))

    with caplog.at_level(logging.ERROR, logger="chain"):
        code = _run_chain_main([
            "chain.py", "seed",
            "--project", str(proj),
            "--max-chains", "2",
        ])
    assert code == 1


# --- Guard is bypassed by --list and --stop -----------------------------------


def test_list_bypasses_guard_even_when_over_limit(isolated_registry, capsys):
    """--list must work even if the registry is full; sys.exit(0) fires first."""
    _preload_running_chains(10)
    # --max-chains omitted on purpose: the guard would fire with the default 5
    # if --list did not short-circuit first.
    code = _run_chain_main(["chain.py", "--list"])
    assert code == 0
    out = capsys.readouterr().out
    # 10 rows plus header; just confirm we saw some of them.
    assert "preload-0" in out
    assert "preload-9" in out


def test_stop_bypasses_guard_even_when_over_limit(isolated_registry):
    """--stop short-circuits before the guard; cmd_stop handles its own exits."""
    _preload_running_chains(10)
    # Stopping a nonexistent ID → cmd_stop prints 'not found' and exits 1,
    # but critically NOT because of the --max-chains guard. If the guard had
    # fired first, the exit would still be 1 but the caplog trail would show
    # the guard message. We check the log source path instead.
    with patch("subprocess.run"), patch("registry.os.kill"):
        code = _run_chain_main(["chain.py", "--stop", "does-not-exist"])
    assert code == 1


# --- Below-limit passes the guard --------------------------------------------


def test_below_limit_passes_guard(isolated_registry, tmp_path):
    """With count < limit, control flows past the guard into boot setup.

    We prove the guard did not fire by observing that the code reached
    subprocess.run (first call is tmux kill-session in setup_tmux). The
    mock raises a sentinel so execution stops there.
    """
    # Zero preloaded chains, limit 5: guard must pass.
    proj = tmp_path / "proj"
    proj.mkdir()
    sentinel = RuntimeError("past-guard")
    with patch("subprocess.run", side_effect=sentinel):
        with pytest.raises(RuntimeError, match="past-guard"):
            _run_chain_main([
                "chain.py", "seed", "--project", str(proj), "--max-chains", "5",
            ])


def test_default_max_chains_is_five(isolated_registry, tmp_path, caplog):
    """When --max-chains is omitted, the default is 5: four preloads pass, five blocks."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _preload_running_chains(5, project=str(proj.resolve()))
    with caplog.at_level(logging.ERROR, logger="chain"):
        code = _run_chain_main(["chain.py", "seed", "--project", str(proj)])
    assert code == 1
    messages = [r.message for r in caplog.records if r.name == "chain"]
    assert any("max: 5" in m for m in messages), (
        f"Default limit should be 5; got: {messages!r}"
    )
