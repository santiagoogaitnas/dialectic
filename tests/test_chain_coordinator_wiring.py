"""Tests for the chain.py ↔ chain_coordinator wiring.

chain_coordinator.ChainCoordinatorContext is now used inside run_chain to
register the chain's presence on the project, run a background heartbeat,
and deregister on exit. chain.py also exposes a --focus CLI argument so
launchers can declare a focus area for multi-chain work segmentation.

These tests pin both concerns without booting a real tmux session:

- Static: --focus is parsed by argparse with the right default.
- Static: chain.py imports ChainCoordinatorContext at module level (so the
  import wiring can't silently disappear under a future refactor).
- Behavioural: run_chain instantiates ChainCoordinatorContext with
  (project_dir=cfg.project, chain_id=cfg.chain_id, focus=focus) and uses
  it as a context manager. Uses a sentinel to short-circuit run_chain
  before it touches tmux, so the test stays a unit test.
- Behavioural: when cfg has no project, ChainCoordinatorContext is still
  instantiated but with project_dir=None — the context's own `active`
  property handles the no-op case; run_chain doesn't branch.
- CLI: python3 chain.py "seed" --focus "backend" reaches the launch path
  with args.focus populated.
"""

from __future__ import annotations

import inspect
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import chain
import registry as reg
from registry import ChainConfig


REPO_DIR = Path(__file__).parent.parent.resolve()
CHAIN_PY = REPO_DIR / "chain.py"


# --- Static wiring checks -----------------------------------------------------


def test_chain_py_imports_chain_coordinator_context():
    """A future refactor that drops the import should fail loudly here.

    We check chain.py's module namespace rather than grepping source so a
    rename like `from chain_coordinator import ChainCoordinatorContext as X`
    would still satisfy the contract (the runtime symbol is what matters).
    """
    assert hasattr(chain, "ChainCoordinatorContext"), (
        "chain.py must import ChainCoordinatorContext from chain_coordinator"
    )
    from chain_coordinator import ChainCoordinatorContext as Real
    assert chain.ChainCoordinatorContext is Real


def test_run_chain_signature_accepts_focus_kwarg():
    """run_chain must accept focus=... so the CLI can forward --focus."""
    sig = inspect.signature(chain.run_chain)
    assert "focus" in sig.parameters, (
        "run_chain must accept `focus` so CLI --focus can propagate to "
        f"ChainCoordinatorContext. Got params: {list(sig.parameters)}"
    )
    assert sig.parameters["focus"].default == "", (
        "focus must default to empty string — a chain with no focus is the "
        "common case and shouldn't require the caller to pass anything."
    )


def test_chain_py_cli_accepts_focus_argument(tmp_path, capsys):
    """--focus prints in --help and is accepted as a long option.

    Uses argparse introspection rather than full CLI execution because
    __main__ would try to create a tmux session.
    """
    # Re-run the CLI with --help to confirm argparse knows about --focus.
    saved_argv = sys.argv
    sys.argv = ["chain.py", "--help"]
    try:
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(str(CHAIN_PY), run_name="__main__")
        assert excinfo.value.code == 0
    finally:
        sys.argv = saved_argv
    help_text = capsys.readouterr().out
    assert "--focus" in help_text, (
        f"--focus should appear in chain.py --help, got: {help_text[:500]}"
    )


# --- Behavioural wiring: ChainCoordinatorContext is invoked from run_chain ----


class _StopRunChain(Exception):
    """Sentinel raised to break out of run_chain after the coord wiring."""


@contextmanager
def _short_circuit_run_chain():
    """Patch enough of run_chain's dependencies to reach the `with` block,
    then raise _StopRunChain from the first do_round call so the context's
    __enter__/__exit__ both run exactly once.

    `discover_jsonl_with_retries` is intentionally NOT patched here — each
    test provides its own side_effect so cursor_a.file_path differs from
    cursor_b.file_path (else the JSONL-collision check returns before the
    with-block opens).
    """
    patches = [
        patch.object(chain, "setup_workspace", MagicMock()),
        patch.object(chain, "setup_tmux", MagicMock()),
        patch.object(chain, "_write_pane_claude_md", MagicMock()),
        patch.object(chain, "start_agent", MagicMock()),
        patch("time.sleep", MagicMock()),
        patch.object(chain, "wait_for_idle", MagicMock(return_value=True)),
        patch.object(chain, "inject_message", MagicMock(return_value=True)),
        patch.object(chain, "wait_and_extract",
                     MagicMock(return_value="dummy-output")),
        # Keep the relay loop alive long enough to reach do_round. The
        # dead-tmux exit path is the only non-KeyboardInterrupt exit, so
        # without this stub the loop breaks at round 0 and the test's
        # short-circuit exception never fires.
        patch.object(chain, "_session_alive", MagicMock(return_value=True)),
        patch.object(chain, "do_round", MagicMock(side_effect=_StopRunChain)),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            try:
                p.stop()
            except RuntimeError:
                pass


def _make_cfg(tmp_path, chain_id="test-chain", project=None):
    cfg = ChainConfig(
        chain_id=chain_id,
        session=f"sess-{chain_id}",
        seed="seed text",
        project=str(project) if project else None,
    )
    return cfg


def _distinct_jsonl(cfg):
    """discover_jsonl_with_retries must return different files for A and B,
    otherwise run_chain treats it as a collision and aborts before the loop.
    Uses cfg.pane_dirs() to compute plausible paths so the assertion the
    chain makes (cursor_a.file_path != cursor_b.file_path) holds.
    """
    pane_a_dir, pane_b_dir = cfg.pane_dirs()
    a = MagicMock(file_path=Path(pane_a_dir) / "a.jsonl")
    b = MagicMock(file_path=Path(pane_b_dir) / "b.jsonl")
    return [a, b]


def test_run_chain_wraps_main_loop_in_coordinator_context(tmp_path):
    """The `with ChainCoordinatorContext(...)` wrapping must execute with
    project/chain_id/focus from cfg + args."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cfg = _make_cfg(tmp_path, chain_id="wirecheck", project=project_dir)

    # Intercept ChainCoordinatorContext at the chain module level so we can
    # assert how run_chain instantiates and uses it.
    observed = {"init_kwargs": None, "entered": False, "exited": False}

    class FakeCtx:
        def __init__(self, **kwargs):
            observed["init_kwargs"] = kwargs

        def __enter__(self):
            observed["entered"] = True
            return self

        def __exit__(self, exc_type, exc, tb):
            observed["exited"] = True
            # Don't swallow _StopRunChain; let run_chain's outer try handle.
            return False

    with patch.object(chain, "ChainCoordinatorContext", FakeCtx), \
         patch.object(reg, "register_chain", MagicMock(return_value=None)), \
         patch.object(reg, "unregister_chain", MagicMock()), \
         patch.object(reg, "update_chain", MagicMock()), \
         _short_circuit_run_chain(), \
         patch.object(
             chain, "discover_jsonl_with_retries",
             MagicMock(side_effect=_distinct_jsonl(cfg)),
         ):
        # Patch open so the log write doesn't hit disk.
        with patch("builtins.open", MagicMock()):
            with pytest.raises(_StopRunChain):
                chain.run_chain(
                    seed="seed text",
                    cfg=cfg,
                    focus="backend api",
                )

    assert observed["init_kwargs"] is not None, (
        "ChainCoordinatorContext was never constructed — run_chain didn't "
        "reach the `with` block."
    )
    assert observed["init_kwargs"]["chain_id"] == "wirecheck"
    assert observed["init_kwargs"]["focus"] == "backend api"
    # project_dir should resolve to cfg.project (str form). The context's
    # own constructor coerces it to Path, but we assert what run_chain passed.
    passed_project = observed["init_kwargs"]["project_dir"]
    assert str(passed_project) == str(project_dir)
    assert observed["entered"] is True, (
        "context __enter__ must have run — the with block was entered."
    )
    assert observed["exited"] is True, (
        "context __exit__ must have run — even when the loop exits via "
        "_StopRunChain raised from do_round."
    )


def test_run_chain_with_no_project_still_wraps_loop_in_context(tmp_path):
    """cfg.project=None path must still open a context (it's inactive, but
    constructed). This keeps the code path uniform: we never skip the with
    block based on project presence — ChainCoordinatorContext handles the
    no-project case itself via its `active` property.
    """
    cfg = _make_cfg(tmp_path, chain_id="noproj", project=None)

    observed = {"init_kwargs": None, "entered": False}

    class FakeCtx:
        def __init__(self, **kwargs):
            observed["init_kwargs"] = kwargs

        def __enter__(self):
            observed["entered"] = True
            return self

        def __exit__(self, *a):
            return False

    with patch.object(chain, "ChainCoordinatorContext", FakeCtx), \
         patch.object(reg, "register_chain", MagicMock(return_value=None)), \
         patch.object(reg, "unregister_chain", MagicMock()), \
         patch.object(reg, "update_chain", MagicMock()), \
         _short_circuit_run_chain(), \
         patch.object(
             chain, "discover_jsonl_with_retries",
             MagicMock(side_effect=_distinct_jsonl(cfg)),
         ):
        with patch("builtins.open", MagicMock()):
            with pytest.raises(_StopRunChain):
                chain.run_chain(seed="seed", cfg=cfg, focus="")

    assert observed["init_kwargs"] is not None
    assert observed["init_kwargs"]["chain_id"] == "noproj"
    assert observed["init_kwargs"]["project_dir"] is None, (
        "With cfg.project=None, run_chain should pass project_dir=None so "
        "the context falls into its inactive branch."
    )
    assert observed["entered"] is True


# --- CLI-to-run_chain plumbing (source-inspection, not runpy) ------------------
#
# An earlier version of this file used runpy.run_path to execute chain.py's
# __main__ block with a patched chain.run_chain. That doesn't work — runpy
# creates a fresh module namespace, so the real run_chain runs, which in
# turn really opens tmux and launches Claude. Tests MUST NOT spawn real
# tmux sessions or agents. Instead, verify the CLI-side forwarding by
# reading chain.py's source: the __main__ block has one run_chain(...) call
# and it must pass focus=args.focus.


def test_cli_main_calls_run_chain_with_focus_from_args():
    """chain.py's __main__ run_chain(...) call must include focus=args.focus.

    Reads chain.py and matches the run_chain call. A rename like
    `args.focus_area` would make the regression visible here, as would a
    drop of focus= entirely.
    """
    source = CHAIN_PY.read_text(encoding="utf-8")
    # The call spans lines in chain.py. Flatten whitespace to match across
    # any reformat.
    flat = " ".join(source.split())
    assert "run_chain(" in flat, "chain.py should call run_chain() in __main__"
    # Everything between the final `run_chain(` and its matching `)` is our
    # target. We only care about the LAST one — that's the __main__ call.
    last_call_start = flat.rfind("run_chain(")
    after = flat[last_call_start:]
    # Find balanced close paren.
    depth = 0
    end = None
    for idx, ch in enumerate(after):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = idx
                break
    assert end is not None, f"Unbalanced run_chain(...) near: {after[:200]!r}"
    call_text = after[: end + 1]
    assert "focus=args.focus" in call_text, (
        f"__main__'s run_chain call must forward focus=args.focus. "
        f"Got: {call_text!r}"
    )


def test_cli_focus_default_is_empty_string_in_argparse():
    """The --focus argparse default must be '' so chains without an explicit
    focus call into ChainCoordinatorContext with focus='' (the underlying
    API's annotated `str` type), not None.
    """
    source = CHAIN_PY.read_text(encoding="utf-8")
    # Collapse whitespace for a robust match across line wraps.
    flat = " ".join(source.split())
    # Require the add_argument literal with default=""
    assert 'add_argument("--focus"' in flat, (
        "chain.py must declare --focus in its argparse setup"
    )
    # Find the add_argument block for --focus.
    start = flat.find('add_argument("--focus"')
    chunk = flat[start:start + 400]
    assert 'default=""' in chunk, (
        f"--focus must default to the empty string. argparse block: {chunk!r}"
    )
