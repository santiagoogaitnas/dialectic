"""Tests for chain._write_pane_claude_md ↔ coordination_prompt wiring.

coordination_prompt.claude_md_section(...) builds the "Multi-chain
coordination" section. chain.py's _write_pane_claude_md is the caller
that appends it to each pane's CLAUDE.md. These tests verify the join:

- The section is included when project_dir + cfg.chain_id are both set.
- The section is skipped in the no-project fallback (cfg=None, no
  PROJECT_DIR env) because there's no coordination file to reference.
- The focus string passed through run_chain lands in the rendered
  section (so agents see 'Your focus area is: ...').
- Missing/unreadable coordination.json doesn't break the writer — the
  degraded-state placeholder coordination_prompt emits still reaches
  the file.
- The prior sections (role prompt, plan filename, relay contract) still
  render alongside the new section.

No real chains are booted and no tmux commands are shelled out — we call
_write_pane_claude_md directly with a fabricated ChainConfig and read the
resulting CLAUDE.md from disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chain  # noqa: E402
import coordination_prompt  # noqa: E402
import project_coordinator as pc  # noqa: E402
import registry as reg  # noqa: E402


def _make_cfg(tmp_path: Path, chain_id: str = "99999999-beef") -> reg.ChainConfig:
    """Build a ChainConfig whose project_dir points at a real tmp_path."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return reg.ChainConfig(
        chain_id=chain_id,
        session=f"chain-{chain_id}",
        seed="test-seed",
        project=str(project_dir),
        role_a="builder.txt",
        role_b="thinker.txt",
    )


def _read_claude_md(pane_dir: Path) -> str:
    return (pane_dir / "CLAUDE.md").read_text(encoding="utf-8")


def test_coordination_section_included_when_project_and_chain_id(tmp_path):
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE PROMPT", cfg, focus="backend api")

    text = _read_claude_md(pane_dir)
    assert "## Multi-chain coordination" in text
    assert f"chain `{cfg.chain_id}`" in text
    # Header references the concrete project dir.
    assert str(cfg.project_dir) in text


def test_focus_string_flows_into_rendered_section(tmp_path):
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="frontend polish")

    text = _read_claude_md(pane_dir)
    assert "Your focus area is: 'frontend polish'." in text


def test_empty_focus_renders_unset_guidance(tmp_path):
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="")

    text = _read_claude_md(pane_dir)
    assert "Your focus area is not set yet." in text


def test_section_degrades_gracefully_when_coord_file_missing(tmp_path):
    """The coordination.json doesn't exist yet (first chain on project).
    coordination_prompt.claude_md_section catches that and emits a
    placeholder, so the CLAUDE.md still writes without raising.
    """
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()
    coord_dir = cfg.project_dir / ".dialectic"
    assert not coord_dir.exists(), "test precondition: no coord file yet"

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="x")

    text = _read_claude_md(pane_dir)
    assert "## Multi-chain coordination" in text
    # Protocol body (the instructions) must reach the agent even when the
    # state snapshot is empty — the point of the whole section.
    assert "--summary" in text
    assert "--claims" in text


def test_section_embeds_live_summary_when_coord_file_exists(tmp_path):
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()
    # Seed the project with a second chain already registered so the summary
    # isn't empty.
    pc.register_chain(cfg.project_dir, "other-chain-abc", focus="tests")

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="backend")

    text = _read_claude_md(pane_dir)
    assert "### Current coordination state" in text
    assert "other-chain-abc" in text
    assert "tests" in text


def test_no_coordination_section_without_project(tmp_path, monkeypatch):
    """cfg=None and no PROJECT_DIR env → no project → no section."""
    monkeypatch.setattr(chain, "PROJECT_DIR", None, raising=False)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg=None, focus="")

    text = _read_claude_md(pane_dir)
    assert "## Multi-chain coordination" not in text
    assert "ROLE" in text


def test_no_coordination_section_when_chain_id_missing(tmp_path):
    """A ChainConfig without a chain_id (shouldn't happen in practice, but
    defend against it) skips the section — coordination_prompt requires
    chain_id to build a sensible header.
    """
    # Build a cfg with project_dir but blank chain_id.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cfg = reg.ChainConfig(
        chain_id="",
        session="chain-blank",
        seed="test-seed",
        project=str(project_dir),
        role_a="builder.txt",
        role_b="thinker.txt",
    )
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="x")

    text = _read_claude_md(pane_dir)
    assert "## Multi-chain coordination" not in text


def test_existing_sections_still_render_alongside_coordination(tmp_path):
    """Regression: the plan-filename sentence and relay-contract paragraph
    (added by earlier coordination work) must not disappear when we append the
    coordination section.
    """
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE PROMPT X", cfg, focus="")

    text = _read_claude_md(pane_dir)
    # Role prompt is still first.
    assert text.startswith("ROLE PROMPT X")
    # Plan-filename sentence still present (absolute path, not a bare
    # filename — see the plan-path segment). cfg.plan_path.resolve() is
    # the exact string the rendered instruction uses.
    assert str(cfg.plan_path.resolve()) in text
    assert "Maintain a working doc at the absolute path" in text
    # Relay contract still present.
    assert "You are in a relay" in text
    # And the new section sits alongside them.
    assert "## Multi-chain coordination" in text


def test_project_claude_md_contents_copied_before_coordination(tmp_path):
    """Regression: a project-local CLAUDE.md is still spliced in ahead of
    the plan/relay/coordination sections.
    """
    cfg = _make_cfg(tmp_path)
    (cfg.project_dir / "CLAUDE.md").write_text(
        "PROJECT-LEVEL CLAUDE MD CONTENTS\n",
        encoding="utf-8",
    )
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="")

    text = _read_claude_md(pane_dir)
    assert "PROJECT-LEVEL CLAUDE MD CONTENTS" in text
    # Coordination section still present too.
    assert "## Multi-chain coordination" in text


def test_two_panes_get_matching_coordination_header(tmp_path):
    """A+B panes on the same chain should both reference the same chain_id
    and project in their coordination headers (they're one pair).
    """
    cfg = _make_cfg(tmp_path)
    pane_a = tmp_path / "pane_a"
    pane_b = tmp_path / "pane_b"
    pane_a.mkdir()
    pane_b.mkdir()

    chain._write_pane_claude_md(pane_a, "A-ROLE", cfg, focus="frontend")
    chain._write_pane_claude_md(pane_b, "B-ROLE", cfg, focus="frontend")

    a_text = _read_claude_md(pane_a)
    b_text = _read_claude_md(pane_b)
    for text in (a_text, b_text):
        assert f"chain `{cfg.chain_id}`" in text
        assert "Your focus area is: 'frontend'." in text
    # But role prompts differ — just a sanity check that we didn't swap files.
    assert a_text.startswith("A-ROLE")
    assert b_text.startswith("B-ROLE")


def test_coordination_prompt_render_failure_does_not_break_writer(
    tmp_path, monkeypatch,
):
    """If render_summary raises (e.g. disk error), coordination_prompt
    swallows the exception and emits a placeholder — CLAUDE.md still writes.
    """
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    def _boom(project_dir):
        raise OSError("simulated disk error")

    monkeypatch.setattr(pc, "render_summary", _boom)

    # Must not raise.
    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="x")

    text = _read_claude_md(pane_dir)
    assert "## Multi-chain coordination" in text
    assert "(coordination state not available)" in text


def test_claude_md_uses_absolute_coordinator_script(tmp_path):
    """End-to-end wiring: the CLAUDE.md on disk must tell the agent to
    invoke ``project_coordinator`` via its absolute path, not ``-m``.

    The pane cwd is ``<project>/.dialectic-a-<chain_id>/`` — nowhere near
    the dialectic repo — so ``python3 -m project_coordinator`` fails with
    ``No module named project_coordinator``. A rendered CLAUDE.md that
    still carries the ``-m`` form as an *instruction* is the bug this test
    catches. (A contrastive mention of `-m` inside prose warning the
    agent *not* to use it is fine — we check the executable command lines
    specifically.)
    """
    cfg = _make_cfg(tmp_path)
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE", cfg, focus="backend")

    text = _read_claude_md(pane_dir)
    # No command line (leading whitespace only) invokes via `-m`.
    bad_command_lines = [
        line for line in text.splitlines()
        if line.lstrip().startswith("python3 -m project_coordinator")
    ]
    assert not bad_command_lines, (
        f"CLAUDE.md still tells the agent to use `-m project_coordinator`: "
        f"{bad_command_lines!r}"
    )
    # The absolute script path is what the fix looks like.
    pc_script = Path(pc.__file__).resolve()
    assert str(pc_script) in text
    # And the script it points at exists, so an agent can actually run it.
    assert pc_script.is_file()
    # At least one command-line actually runs the script.
    good_command_lines = [
        line for line in text.splitlines()
        if line.lstrip().startswith(f"python3 {pc_script}")
    ]
    assert good_command_lines, (
        "No `python3 <abs-path> ...` command line found in CLAUDE.md."
    )
