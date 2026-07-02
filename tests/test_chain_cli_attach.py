"""Tests for chain.py's CLI --attach command (cmd_attach).

cmd_attach is a CLI-only helper that replaces the current process with
`tmux attach -t <session>` for a registered chain, so a user can jump
into the right tmux session without copying a session name by hand.

We never let os.execvp actually fire in tests — it would replace the
pytest process. Instead we patch it and assert the argv it was called
with.
"""

import os
from unittest.mock import patch

import pytest

import chain
import registry as reg
from registry import ChainConfig


def _isolate_registry(tmp_path):
    return (
        patch.object(reg, "REGISTRY_FILE", tmp_path / ".registry.json"),
        patch.object(reg, "WORKSPACE", tmp_path),
    )


def test_cmd_attach_execs_tmux_with_session(tmp_path):
    """A registered chain → os.execvp invoked with its session name."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-attach", session="sess-attach", seed="t")
        reg.register_chain(cfg, os.getpid())
        with patch("chain.os.execvp") as mock_exec:
            chain.cmd_attach("c-attach")
    mock_exec.assert_called_once_with(
        "tmux", ["tmux", "attach", "-t", "sess-attach"]
    )


def test_cmd_attach_not_found_exits_1(tmp_path, capsys):
    """Unknown chain id → 'not found' message + exit 1 (no execvp call)."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        with patch("chain.os.execvp") as mock_exec:
            with pytest.raises(SystemExit) as exc_info:
                chain.cmd_attach("no-such-chain")
        mock_exec.assert_not_called()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "no-such-chain" in out
    assert "not found" in out


def test_cmd_attach_dead_chain_warns_but_still_execs(tmp_path, capsys):
    """If the chain is 'dead', we warn on stderr but still attempt attach.

    Rationale: the registry marks a chain dead when its parent Python
    process is gone, but the tmux session can outlive that process. A
    user wanting to inspect the orphan session should still be able to.
    """
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-dead", session="sess-dead", seed="t")
        # impossible PID → list_chains/get_chain will flip status to 'dead'
        reg.register_chain(cfg, 999_999_999)
        with patch("chain.os.execvp") as mock_exec:
            chain.cmd_attach("c-dead")

    captured = capsys.readouterr()
    assert "dead" in captured.err.lower()
    mock_exec.assert_called_once_with(
        "tmux", ["tmux", "attach", "-t", "sess-dead"]
    )


def test_cmd_attach_no_tmux_binary_exits_1(tmp_path, capsys):
    """If tmux is not installed, execvp raises FileNotFoundError → exit 1."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-notmux", session="sess-x", seed="t")
        reg.register_chain(cfg, os.getpid())
        with patch("chain.os.execvp", side_effect=FileNotFoundError()):
            with pytest.raises(SystemExit) as exc_info:
                chain.cmd_attach("c-notmux")

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "tmux not found" in err.lower()


def test_cmd_attach_argparse_wires_to_cmd(tmp_path):
    """--attach CHAIN_ID routes through chain.py's __main__ to execvp.

    Running chain.py via runpy loads a fresh module (so patching `chain.xxx`
    doesn't affect it). Instead we register a chain in the isolated registry,
    patch os.execvp at the stdlib level, invoke chain.py --attach, and assert
    that execvp was called with the chain's session argv. Proves the argparse
    flag reaches cmd_attach *and* short-circuits before max-chains boots a
    tmux session.
    """
    import runpy
    import sys
    from pathlib import Path

    REPO_DIR = Path(__file__).parent.parent.resolve()
    CHAIN_PY = REPO_DIR / "chain.py"

    p1, p2 = _isolate_registry(tmp_path)
    saved_argv = sys.argv
    sys.argv = ["chain.py", "--attach", "argp-c"]
    try:
        with p1, p2:
            cfg = ChainConfig(chain_id="argp-c", session="argp-sess", seed="t")
            reg.register_chain(cfg, os.getpid())
            with patch("os.execvp") as mock_exec:
                with pytest.raises(SystemExit) as exc_info:
                    runpy.run_path(str(CHAIN_PY), run_name="__main__")
        mock_exec.assert_called_once_with(
            "tmux", ["tmux", "attach", "-t", "argp-sess"]
        )
        assert exc_info.value.code == 0
    finally:
        sys.argv = saved_argv
