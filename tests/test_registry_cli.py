"""Tests for registry.py's prune_inactive(), remove_chain(), and _cli_main().

These three were added in commit 434e14d (af16, committed by f44f as a
stale-lock takeover) and shipped without test coverage. tests/test_registry.py
covers register/unregister/update/list/cleanup_dead_chains/get/read_plan_text
but stops short of the af16 additions; this file fills that gap.

All tests isolate the registry to tmp_path via patch.object on REGISTRY_FILE
and WORKSPACE, mirroring the convention in tests/test_registry.py.
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import registry as reg
from registry import ChainConfig, _cli_main, prune_inactive, remove_chain


# --- prune_inactive --------------------------------------------------------


def test_prune_inactive_removes_stopped_and_dead(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        for cid, status in [("alive", "running"), ("done", "stopped"), ("gone", "dead")]:
            cfg = ChainConfig(chain_id=cid, session=cid, seed="t")
            reg.register_chain(cfg, os.getpid())
            if status != "running":
                reg.update_chain(cid, status=status, stopped_at=time.time() - 60)

        removed = prune_inactive()
        assert sorted(removed) == ["done", "gone"]

        remaining = [c.chain_id for c in reg.list_chains()]
        assert remaining == ["alive"]


def test_prune_inactive_returns_empty_when_nothing_to_prune(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="only-running", session="s", seed="t")
        reg.register_chain(cfg, os.getpid())

        assert prune_inactive() == []
        # The running chain stayed put.
        assert [c.chain_id for c in reg.list_chains()] == ["only-running"]


def test_prune_inactive_custom_statuses(tmp_path):
    """Caller can target a single status, e.g. only 'stopped'."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        for cid, status in [("s1", "stopped"), ("d1", "dead")]:
            cfg = ChainConfig(chain_id=cid, session=cid, seed="t")
            reg.register_chain(cfg, os.getpid())
            reg.update_chain(cid, status=status, stopped_at=time.time() - 60)

        removed = prune_inactive(statuses=("stopped",))
        assert removed == ["s1"]
        # 'd1' (dead) was NOT in the eligible set, so it survives.
        assert [c.chain_id for c in reg.list_chains()] == ["d1"]


def test_prune_inactive_older_than_keeps_recent(tmp_path):
    """older_than_seconds protects against pruning a chain that just stopped."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        now = time.time()
        for cid, stopped_at in [("recent", now - 10), ("old", now - 3600)]:
            cfg = ChainConfig(chain_id=cid, session=cid, seed="t")
            reg.register_chain(cfg, os.getpid())
            reg.update_chain(cid, status="stopped", stopped_at=stopped_at)

        # 60s older_than: 'recent' (10s old) survives, 'old' (1h old) goes.
        removed = prune_inactive(older_than_seconds=60)
        assert removed == ["old"]
        assert [c.chain_id for c in reg.list_chains()] == ["recent"]


def test_prune_inactive_older_than_skips_records_without_stopped_at(tmp_path):
    """A stopped record with stopped_at=0 (defaulted) shouldn't slip through
    older_than just because (now - 0) is huge — the guard requires a real
    stopped_at."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="hazy", session="hazy", seed="t")
        reg.register_chain(cfg, os.getpid())
        reg.update_chain("hazy", status="stopped", stopped_at=0)

        removed = prune_inactive(older_than_seconds=10)
        assert removed == []  # zero stopped_at → not eligible
        assert [c.chain_id for c in reg.list_chains()] == ["hazy"]


def test_prune_inactive_reclassifies_dead_pid_then_prunes(tmp_path):
    """A 'running' record with a dead PID gets re-classified to 'dead' inside
    prune_inactive (same liveness check as cleanup_dead_chains), so a single
    --prune call cleans up orphans without a separate sweep first."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="orphan", session="o", seed="t")
        # PID 1 is init/launchd — alive on every Unix host. We patch os.kill
        # to simulate a dead PID instead of fabricating one (PIDs above
        # /proc/sys/kernel/pid_max would also work but are platform-specific).
        reg.register_chain(cfg, 1)

        with patch("registry.os.kill", side_effect=ProcessLookupError):
            removed = prune_inactive()

        assert removed == ["orphan"]
        assert reg.list_chains() == []


def test_prune_inactive_reclassifies_dead_then_persists_even_when_no_removal(tmp_path):
    """The persistence branch hits when older_than blocks the prune but the
    re-classification still mutated the record. The on-disk row should reflect
    status='dead' even though nothing was removed."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="ghost", session="g", seed="t")
        reg.register_chain(cfg, 1)

        with patch("registry.os.kill", side_effect=ProcessLookupError):
            removed = prune_inactive(older_than_seconds=3600)

        assert removed == []
        record = reg.get_chain("ghost")
        assert record.status == "dead"
        assert record.stopped_at > 0


# --- remove_chain ----------------------------------------------------------


def test_remove_chain_success(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="rm-me", session="r", seed="t")
        reg.register_chain(cfg, os.getpid())

        assert remove_chain("rm-me") is True
        assert reg.get_chain("rm-me") is None
        assert reg.list_chains() == []


def test_remove_chain_missing_returns_false(tmp_path):
    """Removing a non-existent id is a soft no-op."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        assert remove_chain("never-was") is False


def test_remove_chain_does_not_affect_siblings(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        for cid in ["keep-1", "drop", "keep-2"]:
            cfg = ChainConfig(chain_id=cid, session=cid, seed="t")
            reg.register_chain(cfg, os.getpid())

        assert remove_chain("drop") is True
        ids = sorted(c.chain_id for c in reg.list_chains())
        assert ids == ["keep-1", "keep-2"]


# --- _cli_main -------------------------------------------------------------


def test_cli_main_list_empty_registry(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        rc = _cli_main(["--list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Registry is empty" in out


def test_cli_main_list_populated_renders_header_and_rows(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg_with_proj = ChainConfig(
            chain_id="abc12345", session="sess-abc",
            seed="topic", project="/tmp/myproj",
        )
        cfg_no_proj = ChainConfig(chain_id="def98765", session="sess-def", seed="t")
        reg.register_chain(cfg_with_proj, os.getpid())
        reg.register_chain(cfg_no_proj, os.getpid())

        rc = _cli_main(["--list"])

    assert rc == 0
    out = capsys.readouterr().out
    # Header columns
    for col in ("ID", "Status", "PID", "Session", "Project"):
        assert col in out
    # Both chain ids
    assert "abc12345" in out and "def98765" in out
    # Project value vs the dash for the no-project row
    assert "/tmp/myproj" in out
    # Row separator
    assert "-" * 90 in out


def test_cli_main_list_truncates_long_project_path(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    long_path = "/this/is/a/very/long/project/path/that/exceeds/the/forty/char/limit/leaf"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="long1", session="sl", seed="t", project=long_path)
        reg.register_chain(cfg, os.getpid())

        rc = _cli_main(["--list"])

    assert rc == 0
    out = capsys.readouterr().out
    # Left-truncated with '...' so the meaningful tail (basename "leaf") survives.
    assert "..." in out
    assert "leaf" in out


def test_cli_main_prune_empty_reports_nothing(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        rc = _cli_main(["--prune"])

    assert rc == 0
    assert "Nothing to prune" in capsys.readouterr().out


def test_cli_main_prune_removes_and_reports(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="bye", session="b", seed="t")
        reg.register_chain(cfg, os.getpid())
        reg.update_chain("bye", status="stopped", stopped_at=time.time() - 100)

        rc = _cli_main(["--prune"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Removed 1 record" in out
    assert "bye" in out


def test_cli_main_prune_with_older_than_filter(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        now = time.time()
        for cid, stopped_at in [("fresh", now - 5), ("aged", now - 600)]:
            cfg = ChainConfig(chain_id=cid, session=cid, seed="t")
            reg.register_chain(cfg, os.getpid())
            reg.update_chain(cid, status="stopped", stopped_at=stopped_at)

        rc = _cli_main(["--prune", "--older-than", "60"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "aged" in out
        assert "fresh" not in out
        # 'fresh' survives the prune and 'aged' is gone. Read while still
        # inside the patch — once the with-block exits, reg.list_chains()
        # falls back to the real chainwork/.registry.json.
        surviving = [c.chain_id for c in reg.list_chains()]
        assert surviving == ["fresh"]


def test_cli_main_rm_success(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="rmcli", session="r", seed="t")
        reg.register_chain(cfg, os.getpid())

        rc = _cli_main(["--rm", "rmcli"])

        assert rc == 0
        assert "Removed chain rmcli" in capsys.readouterr().out
        assert reg.get_chain("rmcli") is None


def test_cli_main_rm_missing_exits_nonzero(tmp_path, capsys):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        rc = _cli_main(["--rm", "ghost"])

        # Exit code 1 lets shell pipelines treat "no such chain" as failure.
        assert rc == 1
        assert "not found" in capsys.readouterr().out


def test_cli_main_requires_a_subcommand(tmp_path):
    """Mutually-exclusive group with required=True → argparse exits non-zero
    when no subcommand is given. SystemExit is the expected escape hatch."""
    with patch.object(reg, "REGISTRY_FILE", tmp_path / ".registry.json"), \
         patch.object(reg, "WORKSPACE", tmp_path):
        with pytest.raises(SystemExit):
            _cli_main([])


def test_cli_main_subcommands_are_mutually_exclusive(tmp_path):
    """--list and --prune in the same invocation must be rejected."""
    with patch.object(reg, "REGISTRY_FILE", tmp_path / ".registry.json"), \
         patch.object(reg, "WORKSPACE", tmp_path):
        with pytest.raises(SystemExit):
            _cli_main(["--list", "--prune"])
