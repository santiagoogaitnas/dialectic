"""Tests for claude_config — seeding Claude Code's first-run acceptance keys.

The module pre-answers the one-time dialogs (folder trust, bypass-mode
acceptance, onboarding) that would otherwise stall a chain's panes on a
machine that has never clicked through them, and prunes the per-chain
scratch-dir entries claude leaves behind when a chain ends.

Contract under test:
- merge-only writes: nothing already in the file is removed or replaced
- trust is seeded for the resolved project root, not per-chain dirs
- onboarding keys appear only when the config file didn't exist at all
- unparseable config is never overwritten
- files are written atomically with 0600 permissions
- launch path (chain.py __main__) seeds before any tmux boot
- registry.unregister_chain removes exactly the finished chain's entries
"""

import json
import logging
import os
import runpy
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_config
import registry as reg
from registry import ChainConfig


REPO_DIR = Path(__file__).parent.parent.resolve()
CHAIN_PY = REPO_DIR / "chain.py"


def _read(config_file: Path) -> dict:
    return json.loads(config_file.read_text(encoding="utf-8"))


# --- seed_first_run_keys: fresh machine (no config file) ----------------------


def test_seed_creates_file_with_all_gates(tmp_path):
    config = tmp_path / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()

    assert claude_config.seed_first_run_keys(project, config_file=config)

    data = _read(config)
    entry = data["projects"][str(project.resolve())]
    assert entry[claude_config.TRUST_KEY] is True
    assert data[claude_config.BYPASS_KEY] is True
    for key, value in claude_config.FRESH_INSTALL_KEYS.items():
        assert data[key] == value


def test_seed_sets_0600_permissions(tmp_path):
    config = tmp_path / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()

    claude_config.seed_first_run_keys(project, config_file=config)

    mode = stat.S_IMODE(config.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_seed_creates_missing_config_parent_dir(tmp_path):
    config = tmp_path / "not-yet" / "nested" / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()

    assert claude_config.seed_first_run_keys(project, config_file=config)
    assert config.exists()


# --- seed_first_run_keys: existing config (machine has run claude) ------------


def test_seed_merges_without_touching_existing_keys(tmp_path):
    config = tmp_path / ".claude.json"
    existing = {
        "oauthAccount": {"emailAddress": "someone@example.com"},
        "numStartups": 500,
        "theme": "light",
        "hasCompletedOnboarding": True,
        "projects": {
            "/some/other/project": {"allowedTools": ["Bash"], "history": [1, 2]},
        },
    }
    config.write_text(json.dumps(existing), encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()

    assert claude_config.seed_first_run_keys(project, config_file=config)

    data = _read(config)
    # Everything that was there survives, byte-for-byte in value terms.
    assert data["oauthAccount"] == {"emailAddress": "someone@example.com"}
    assert data["projects"]["/some/other/project"] == {
        "allowedTools": ["Bash"], "history": [1, 2],
    }
    # Existing-machine values must NOT be reset by the fresh-install seed.
    assert data["numStartups"] == 500
    assert data["theme"] == "light"
    # And the new keys landed.
    assert data["projects"][str(project.resolve())][claude_config.TRUST_KEY] is True
    assert data[claude_config.BYPASS_KEY] is True


def test_seed_preserves_other_keys_in_existing_project_entry(tmp_path):
    config = tmp_path / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()
    root = str(project.resolve())
    config.write_text(
        json.dumps({"projects": {root: {"allowedTools": ["Edit"]}}}),
        encoding="utf-8",
    )

    claude_config.seed_first_run_keys(project, config_file=config)

    entry = _read(config)["projects"][root]
    assert entry["allowedTools"] == ["Edit"]
    assert entry[claude_config.TRUST_KEY] is True


def test_seed_skips_onboarding_keys_when_file_exists(tmp_path):
    """A machine that has run claude gets trust+bypass only — onboarding
    state belongs to the user and must not be invented for them."""
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()

    claude_config.seed_first_run_keys(project, config_file=config)

    data = _read(config)
    for key in claude_config.FRESH_INSTALL_KEYS:
        assert key not in data, f"{key} must not be written into an existing config"


def test_seed_is_idempotent(tmp_path):
    config = tmp_path / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()

    claude_config.seed_first_run_keys(project, config_file=config)
    first = config.read_bytes()
    first_mtime_ns = config.stat().st_mtime_ns

    assert claude_config.seed_first_run_keys(project, config_file=config)
    assert config.read_bytes() == first
    # Second call found nothing to change, so it must not rewrite the file.
    assert config.stat().st_mtime_ns == first_mtime_ns


def test_seed_repairs_non_dict_project_entry(tmp_path):
    """A null/garbage entry for the project can't hold the trust flag —
    it gets replaced with a dict rather than crashing the launch."""
    config = tmp_path / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()
    root = str(project.resolve())
    config.write_text(json.dumps({"projects": {root: None}}), encoding="utf-8")

    assert claude_config.seed_first_run_keys(project, config_file=config)
    assert _read(config)["projects"][root][claude_config.TRUST_KEY] is True


def test_seed_resolves_symlinked_project_root(tmp_path):
    """Trust is keyed by the path claude computes for the pane cwd, which
    has symlinks folded — the seeded key must match that form."""
    real = tmp_path / "real-proj"
    real.mkdir()
    link = tmp_path / "link-proj"
    link.symlink_to(real)
    config = tmp_path / ".claude.json"

    claude_config.seed_first_run_keys(link, config_file=config)

    projects = _read(config)["projects"]
    assert str(real.resolve()) in projects
    assert str(link) not in projects


# --- seed_first_run_keys: failure safety ---------------------------------------


def test_seed_never_overwrites_unparseable_config(tmp_path, caplog):
    config = tmp_path / ".claude.json"
    config.write_text("{definitely not json", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()

    with caplog.at_level(logging.WARNING, logger="claude_config"):
        result = claude_config.seed_first_run_keys(project, config_file=config)

    assert result is False
    assert config.read_text(encoding="utf-8") == "{definitely not json"
    assert any("parse" in r.message.lower() for r in caplog.records)


def test_seed_returns_false_when_config_is_not_an_object(tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()

    assert claude_config.seed_first_run_keys(project, config_file=config) is False
    assert _read(config) == ["a", "list"]


# --- seed_first_run_keys: launch-time sweep of stale scratch entries -------------


def test_seed_sweeps_stale_scratch_entries(tmp_path):
    """Entries for chains that are no longer live go; entries for chains
    still running stay; everything that isn't a scratch entry stays."""
    project = tmp_path / "proj"
    project.mkdir()
    root = project.resolve()
    other_project = "/somewhere/else"
    projects = {
        str(root): {claude_config.TRUST_KEY: True},
        str(root / ".dialectic-a-c-dead"): {claude_config.TRUST_KEY: True},
        str(root / ".dialectic-b-c-dead"): {claude_config.TRUST_KEY: True},
        str(root / ".dialectic-a-c-live"): {claude_config.TRUST_KEY: True},
        str(root / ".dialectic-b-c-live"): {claude_config.TRUST_KEY: True},
        # Same shape but under a different project: out of scope.
        other_project + "/.dialectic-a-c-dead": {claude_config.TRUST_KEY: True},
        # A user dir that merely resembles the prefix: not exactly
        # <root>/.dialectic-<side>-<id>, must survive.
        str(root / ".dialectic-a-c-dead" / "nested"): {claude_config.TRUST_KEY: True},
    }
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": projects}), encoding="utf-8")

    assert claude_config.seed_first_run_keys(
        project, config_file=config, active_chain_ids={"c-live"},
    )

    remaining = _read(config)["projects"]
    assert str(root / ".dialectic-a-c-dead") not in remaining
    assert str(root / ".dialectic-b-c-dead") not in remaining
    assert str(root / ".dialectic-a-c-live") in remaining
    assert str(root / ".dialectic-b-c-live") in remaining
    assert str(root) in remaining
    assert other_project + "/.dialectic-a-c-dead" in remaining
    assert str(root / ".dialectic-a-c-dead" / "nested") in remaining


def test_seed_without_active_ids_sweeps_nothing(tmp_path):
    """active_chain_ids=None means 'not launching, don't sweep' — stale
    entries stay untouched."""
    config, project = _config_with_chain_entries(tmp_path, ["c-old"])

    claude_config.seed_first_run_keys(project, config_file=config)

    remaining = _read(config)["projects"]
    root = project.resolve()
    assert str(root / ".dialectic-a-c-old") in remaining
    assert str(root / ".dialectic-b-c-old") in remaining


def test_main_launch_sweeps_dead_chain_entries(isolated_registry, tmp_path,
                                               monkeypatch):
    """End of the real-life loop: a previous chain's prune lost the race
    against its dying panes, the next launch on the project sweeps the
    leftovers."""
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    config, project = _config_with_chain_entries(tmp_path, ["c-leftover"])
    (config_dir / ".claude.json").write_text(
        config.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # c-leftover is registered but stopped — exactly what a raced prune
    # leaves behind.
    cfg = ChainConfig(chain_id="c-leftover", session="s-x", seed="x",
                      project=str(project.resolve()))
    reg.register_chain(cfg, os.getpid())
    reg.unregister_chain("c-leftover")
    (config_dir / ".claude.json").write_text(
        config.read_text(encoding="utf-8"), encoding="utf-8"
    )  # simulate the panes' claude re-adding the entries after the prune

    sentinel = RuntimeError("tmux-boot-reached")
    with patch("shutil.which", return_value="/usr/bin/stub"), \
         patch("subprocess.run", side_effect=sentinel):
        with pytest.raises(RuntimeError, match="tmux-boot-reached"):
            _run_chain_main(["chain.py", "seed", "--project", str(project)])

    remaining = _read(config_dir / ".claude.json")["projects"]
    root = project.resolve()
    assert str(root / ".dialectic-a-c-leftover") not in remaining
    assert str(root / ".dialectic-b-c-leftover") not in remaining
    assert str(root) in remaining


# --- config_path resolution -----------------------------------------------------


def test_config_path_uses_claude_config_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert claude_config.config_path() == tmp_path / "cfg" / ".claude.json"


def test_config_path_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert claude_config.config_path() == Path.home() / ".claude.json"


# --- remove_chain_scratch_entries ------------------------------------------------


def _config_with_chain_entries(tmp_path, chain_ids):
    """A config holding the project-root trust entry plus per-chain
    scratch entries for every chain id given — the state claude leaves
    behind after those chains have run."""
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    root = project.resolve()
    projects = {str(root): {claude_config.TRUST_KEY: True}}
    for cid in chain_ids:
        for side in ("a", "b"):
            projects[str(root / f".dialectic-{side}-{cid}")] = {
                claude_config.TRUST_KEY: True, "history": ["x"],
            }
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return config, project


def test_cleanup_removes_only_this_chains_entries(tmp_path):
    config, project = _config_with_chain_entries(tmp_path, ["c-one", "c-two"])

    assert claude_config.remove_chain_scratch_entries(
        project, "c-one", config_file=config
    )

    projects = _read(config)["projects"]
    root = str(project.resolve())
    assert root in projects, "project-root trust entry must survive cleanup"
    assert str(Path(root) / ".dialectic-a-c-one") not in projects
    assert str(Path(root) / ".dialectic-b-c-one") not in projects
    assert str(Path(root) / ".dialectic-a-c-two") in projects
    assert str(Path(root) / ".dialectic-b-c-two") in projects


def test_cleanup_noop_when_config_missing(tmp_path):
    config = tmp_path / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()

    assert claude_config.remove_chain_scratch_entries(
        project, "c-x", config_file=config
    )
    assert not config.exists(), "cleanup must never create the config file"


def test_cleanup_noop_when_entries_absent(tmp_path):
    config, project = _config_with_chain_entries(tmp_path, ["c-other"])
    before = config.read_bytes()
    before_mtime_ns = config.stat().st_mtime_ns

    assert claude_config.remove_chain_scratch_entries(
        project, "c-never-ran", config_file=config
    )
    assert config.read_bytes() == before
    assert config.stat().st_mtime_ns == before_mtime_ns


def test_cleanup_never_overwrites_unparseable_config(tmp_path):
    config = tmp_path / ".claude.json"
    config.write_text("not json at all", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()

    assert claude_config.remove_chain_scratch_entries(
        project, "c-x", config_file=config
    ) is False
    assert config.read_text(encoding="utf-8") == "not json at all"


# --- wiring: chain.py __main__ seeds before any pane boots -----------------------


@pytest.fixture
def isolated_registry(tmp_path):
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


def test_main_seeds_config_before_tmux_boot(isolated_registry, tmp_path, monkeypatch):
    """By the time the first subprocess call fires (tmux kill-session in
    setup_tmux), the trust and bypass keys must already be on disk —
    that ordering is the whole point of the fix."""
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    project = tmp_path / "proj"
    project.mkdir()

    sentinel = RuntimeError("tmux-boot-reached")
    with patch("shutil.which", return_value="/usr/bin/stub"), \
         patch("subprocess.run", side_effect=sentinel):
        with pytest.raises(RuntimeError, match="tmux-boot-reached"):
            _run_chain_main(["chain.py", "seed", "--project", str(project)])

    data = _read(config_dir / ".claude.json")
    assert data["projects"][str(project.resolve())][claude_config.TRUST_KEY] is True
    assert data[claude_config.BYPASS_KEY] is True


def test_main_launch_survives_unseedable_config(isolated_registry, tmp_path,
                                                monkeypatch, caplog):
    """A config that can't be updated must not abort the launch — worst
    case is the old behavior (dialog stalls the pane), and the warning
    tells the user what to do about it."""
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    project = tmp_path / "proj"
    project.mkdir()

    sentinel = RuntimeError("tmux-boot-reached")
    with caplog.at_level(logging.WARNING, logger="chain"):
        with patch("shutil.which", return_value="/usr/bin/stub"), \
             patch("subprocess.run", side_effect=sentinel):
            with pytest.raises(RuntimeError, match="tmux-boot-reached"):
                _run_chain_main(["chain.py", "seed", "--project", str(project)])

    assert (config_dir / ".claude.json").read_text(encoding="utf-8") == "{broken"
    warnings = [r.message for r in caplog.records if r.name == "chain"]
    assert any("first-run prompts" in m for m in warnings)


# --- wiring: unregister_chain prunes the finished chain's entries ----------------


def test_unregister_chain_prunes_scratch_entries(isolated_registry, tmp_path,
                                                 monkeypatch):
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    config, project = _config_with_chain_entries(tmp_path, ["c-live", "c-stay"])
    # Point the env-resolved config at the file we just built.
    (config_dir / ".claude.json").write_text(
        config.read_text(encoding="utf-8"), encoding="utf-8"
    )

    cfg = ChainConfig(
        chain_id="c-live", session="s-live", seed="x",
        project=str(project.resolve()),
    )
    reg.register_chain(cfg, os.getpid())
    reg.unregister_chain("c-live")

    projects = _read(config_dir / ".claude.json")["projects"]
    root = str(project.resolve())
    assert root in projects
    assert str(Path(root) / ".dialectic-a-c-live") not in projects
    assert str(Path(root) / ".dialectic-b-c-live") not in projects
    assert str(Path(root) / ".dialectic-a-c-stay") in projects


def test_ctrl_c_during_boot_still_unregisters_and_prunes(isolated_registry,
                                                         tmp_path, monkeypatch):
    """Found live: SIGINT while the agents were still booting used to skip
    run_chain's finally entirely — the registry kept a stale 'running'
    record and the scratch entries survived. The boot phase now sits
    inside the same try/finally as the relay loop."""
    import chain

    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    config, project = _config_with_chain_entries(tmp_path, ["c-boot"])
    (config_dir / ".claude.json").write_text(
        config.read_text(encoding="utf-8"), encoding="utf-8"
    )

    cfg = ChainConfig(
        chain_id="c-boot", session="s-boot", seed="x",
        project=str(project.resolve()),
    )
    # First time.sleep in the boot path raises, as a real Ctrl+C would.
    with patch.object(chain, "setup_workspace"), \
         patch.object(chain, "setup_tmux"), \
         patch.object(chain, "_write_pane_claude_md"), \
         patch.object(chain, "start_agent"), \
         patch.object(chain.time, "sleep", side_effect=KeyboardInterrupt):
        chain.run_chain("seed", cfg=cfg)  # must swallow the interrupt, not raise

    assert reg.get_chain("c-boot").status == "stopped", (
        "boot-phase Ctrl+C must not leave a stale 'running' record"
    )
    projects = _read(config_dir / ".claude.json")["projects"]
    root = str(project.resolve())
    assert str(Path(root) / ".dialectic-a-c-boot") not in projects
    assert str(Path(root) / ".dialectic-b-c-boot") not in projects
    assert root in projects


def test_unregister_chain_without_project_skips_cleanup(isolated_registry,
                                                        tmp_path, monkeypatch):
    """Workspace-mode chains have no project; unregistering one must not
    touch the claude config at all."""
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    cfg = ChainConfig(chain_id="c-noproj", session="s-noproj", seed="x")
    reg.register_chain(cfg, os.getpid())
    reg.unregister_chain("c-noproj")

    assert not (config_dir / ".claude.json").exists()
