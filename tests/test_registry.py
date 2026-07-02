"""Tests for the chain registry — ChainConfig, ChainRecord, and registry operations."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import registry as reg
from registry import (
    ChainConfig,
    ChainRecord,
    generate_chain_id,
    _read_registry,
    _write_registry,
)


def test_generate_chain_id_format():
    """Chain IDs should be timestamp-hash format."""
    cid = generate_chain_id()
    parts = cid.split("-")
    assert len(parts) == 2
    assert len(parts[0]) == 8  # MMDDHHmm
    assert len(parts[1]) == 4  # 4-char hex


def test_generate_chain_id_unique():
    """Two consecutive IDs should differ."""
    a = generate_chain_id()
    b = generate_chain_id()
    assert a != b


def test_chain_config_workspace():
    cfg = ChainConfig(chain_id="test123", session="s1", seed="topic")
    assert cfg.workspace == reg.WORKSPACE / "test123"
    assert cfg.ws_a == reg.WORKSPACE / "test123" / "a"
    assert cfg.ws_b == reg.WORKSPACE / "test123" / "b"
    assert cfg.log_file == reg.WORKSPACE / "test123" / "chain_log.md"


def test_chain_config_bulletin_path():
    """Every chain produces a bulletin path under its workspace.

    Every chain runs with the curator; there is no opt-out field.
    """
    cfg = ChainConfig(chain_id="t", session="s", seed="x")
    assert cfg.bulletin_path == cfg.workspace / "bulletin.md"


def test_chain_config_project_dir():
    cfg = ChainConfig(chain_id="t", session="s", seed="x", project="/tmp/proj")
    assert cfg.project_dir == Path("/tmp/proj")
    cfg_none = ChainConfig(chain_id="t", session="s", seed="x")
    assert cfg_none.project_dir is None


def test_chain_config_plan_path_project_mode():
    """Project mode: plan_path is per-chain to avoid clobbering other chains."""
    cfg = ChainConfig(chain_id="abc12", session="s", seed="x", project="/tmp/proj")
    assert cfg.plan_path == Path("/tmp/proj/plan-abc12.md")
    # Two chains on the same project get different plan paths.
    other = ChainConfig(chain_id="xyz99", session="s2", seed="x", project="/tmp/proj")
    assert other.plan_path != cfg.plan_path


def test_chain_config_plan_path_no_project():
    """Records without a project (legacy or in-progress launches) have no plan path.

    This is a defensive shape: the CLI now always sets project, but the
    dataclass still allows project=None so that older registry rows or unit
    tests that build a ChainConfig directly continue to work.
    """
    cfg = ChainConfig(chain_id="t", session="s", seed="x")
    assert cfg.plan_path is None


def test_chain_config_pane_targets():
    cfg = ChainConfig(chain_id="t", session="mysess", seed="x")
    assert cfg.pane_a == "mysess:0.0"
    assert cfg.pane_b == "mysess:0.1"


def test_chain_config_pane_dirs_no_project(tmp_path):
    """When a config has no project, pane_dirs falls back to ws_a/ws_b."""
    cfg = ChainConfig(chain_id="test", session="s", seed="x")
    # Manually create workspace dirs
    cfg.ws_a.mkdir(parents=True, exist_ok=True)
    cfg.ws_b.mkdir(parents=True, exist_ok=True)
    a, b = cfg.pane_dirs()
    assert a == cfg.ws_a
    assert b == cfg.ws_b


def test_chain_config_pane_dirs_project(tmp_path):
    """In project mode, pane_dirs creates chain-specific subdirs."""
    cfg = ChainConfig(chain_id="abc", session="s", seed="x", project=str(tmp_path))
    a, b = cfg.pane_dirs()
    assert a == tmp_path / ".dialectic-a-abc"
    assert b == tmp_path / ".dialectic-b-abc"
    assert a.is_dir()
    assert b.is_dir()


def test_chain_record_round_trip():
    cfg = ChainConfig(chain_id="r1", session="s", seed="hello", role_a="a.txt", role_b="b.txt")
    record = ChainRecord.from_config(cfg, pid=12345)
    d = record.to_dict()
    restored = ChainRecord.from_dict(d)
    assert restored.chain_id == "r1"
    assert restored.session == "s"
    assert restored.pid == 12345
    assert restored.status == "running"


def test_registry_read_write(tmp_path):
    """Registry round-trip: write then read."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        data = {"chains": {"x": {"chain_id": "x", "status": "running"}}}
        _write_registry(data)
        assert reg_file.exists()
        loaded = _read_registry()
        assert loaded["chains"]["x"]["chain_id"] == "x"


def test_registry_read_missing(tmp_path):
    """Reading a nonexistent registry returns empty."""
    with patch.object(reg, "REGISTRY_FILE", tmp_path / "nope.json"):
        data = _read_registry()
        assert data == {"chains": {}}


def test_register_and_list(tmp_path):
    """Register a chain, then list it."""
    reg_file = tmp_path / ".registry.json"
    lock_file = tmp_path / ".registry.lock"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="c1", session="s1", seed="test")
        record = reg.register_chain(cfg, os.getpid())
        assert record.chain_id == "c1"
        assert record.status == "running"

        chains = reg.list_chains()
        assert len(chains) == 1
        assert chains[0].chain_id == "c1"
        assert chains[0].status == "running"


def test_unregister_chain(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="c2", session="s2", seed="test")
        reg.register_chain(cfg, os.getpid())
        reg.unregister_chain("c2")

        record = reg.get_chain("c2")
        assert record.status == "stopped"


def test_update_chain(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="c3", session="s3", seed="test")
        reg.register_chain(cfg, os.getpid())
        reg.update_chain("c3", current_round=5, last_output_snippet="hello")

        record = reg.get_chain("c3")
        assert record.current_round == 5
        assert record.last_output_snippet == "hello"


def test_count_active_chains(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg1 = ChainConfig(chain_id="a1", session="sa", seed="t", project="/proj")
        cfg2 = ChainConfig(chain_id="a2", session="sb", seed="t", project="/proj")
        cfg3 = ChainConfig(chain_id="a3", session="sc", seed="t", project="/other")
        reg.register_chain(cfg1, os.getpid())
        reg.register_chain(cfg2, os.getpid())
        reg.register_chain(cfg3, os.getpid())

        assert reg.count_active_chains() == 3
        assert reg.count_active_chains(project="/proj") == 2
        assert reg.count_active_chains(project="/other") == 1


def test_cleanup_dead_chains(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="dead1", session="sd", seed="t")
        # Use a PID that doesn't exist
        reg.register_chain(cfg, 999999999)

        cleaned = reg.cleanup_dead_chains()
        assert cleaned == 1

        record = reg.get_chain("dead1")
        assert record.status == "dead"


def test_get_chain_not_found(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        assert reg.get_chain("nonexistent") is None


def test_read_plan_text_returns_contents(tmp_path):
    """read_plan_text reads the per-chain plan file from the project dir."""
    reg_file = tmp_path / ".registry.json"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "plan-planc.md").write_text("# the plan\n\nstep 1\n", encoding="utf-8")
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="planc", session="s", seed="x", project=str(project))
        reg.register_chain(cfg, os.getpid())
        assert reg.read_plan_text("planc") == "# the plan\n\nstep 1\n"


def test_read_plan_text_none_when_missing(tmp_path):
    """read_plan_text returns None when the plan file has not been written yet."""
    reg_file = tmp_path / ".registry.json"
    project = tmp_path / "proj"
    project.mkdir()
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="nofile", session="s", seed="x", project=str(project))
        reg.register_chain(cfg, os.getpid())
        assert reg.read_plan_text("nofile") is None


def test_read_plan_text_none_when_record_has_no_project(tmp_path):
    """A registry row without a project (legacy / direct construction)
    returns None -- there's no project root to read a plan file from.
    """
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        cfg = ChainConfig(chain_id="noproj", session="s", seed="x")
        reg.register_chain(cfg, os.getpid())
        assert reg.read_plan_text("noproj") is None


def test_read_plan_text_none_for_unknown_chain(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        assert reg.read_plan_text("never-registered") is None
