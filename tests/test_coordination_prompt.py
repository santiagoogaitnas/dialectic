"""Tests for coordination_prompt — the CLAUDE.md addendum builder.

The module's job is pure text assembly on top of project_coordinator. These
tests pin the shape of that text: the chain id appears where it should, the
focus line reflects the input, the protocol body is present, the summary
block renders when a state file exists and is omitted cleanly when it does
not, and a corrupt state file doesn't kill the call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make repo-root imports resolve whether pytest is launched from repo root or
# from inside tests/.
_REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import coordination_prompt as cp  # noqa: E402
import project_coordinator as pc  # noqa: E402


CHAIN_ID = "a-04170211-687b"
PC_SCRIPT = Path(pc.__file__).resolve()


# --- claude_md_section_from_state: pure text shape ---

def test_section_includes_header(tmp_path):
    """The header names the chain and the project it belongs to."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    assert text.startswith("## Multi-chain coordination")
    assert f"`{CHAIN_ID}`" in text
    assert str(tmp_path) in text


def test_section_includes_protocol_body(tmp_path):
    """The protocol body's two headings are present and imperative."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    # Claim heading must be imperative ("You MUST") — this is the whole point
    # of the Wave-3 strengthening pass.
    assert "### You MUST claim files before editing them" in text
    assert "### Focus areas" in text


def test_section_embeds_cli_commands_bound_to_project_dir(tmp_path):
    """Read + mutate CLI commands both carry the actual project path."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    # Read-side (pre-edit inspection). Commands are rendered with the
    # absolute coordinator script path so they resolve from the pane cwd
    # (`<project>/.dialectic-a-<id>/`) without any PYTHONPATH setup.
    assert (
        f"python3 {PC_SCRIPT} --project {tmp_path} --summary"
        in text
    )
    assert (
        f"python3 {PC_SCRIPT} --project {tmp_path} --claims"
        in text
    )
    # Mutate-side (actual claim + release). The command is split across lines
    # via a `\` continuation, so match a substring that survives the wrap.
    assert f"--project {tmp_path}" in text
    assert f"--claim " in text
    assert f"--release " in text
    assert f"--chain {CHAIN_ID}" in text


def test_section_uses_absolute_script_not_module_flag(tmp_path):
    """The rendered protocol must NOT instruct agents to run `python3 -m project_coordinator`.

    The in-chain agent's cwd is `<project>/.dialectic-a-<chain_id>/`, which
    isn't on PYTHONPATH. `-m project_coordinator` fails there with
    `No module named project_coordinator`, silently breaking the claim
    protocol. The absolute script path is the only invocation that reaches
    the agent's environment without extra setup — this test pins that.

    A *contrastive* mention of `-m project_coordinator` in prose (warning
    the agent why not to use it) is fine; we scan executable command lines
    specifically.
    """
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    bad_command_lines = [
        line for line in text.splitlines()
        if line.lstrip().startswith("python3 -m project_coordinator")
    ]
    assert not bad_command_lines, (
        "Rendered section still invokes `python3 -m project_coordinator` "
        f"as a command: {bad_command_lines!r}"
    )
    # And the absolute script path is present.
    assert str(PC_SCRIPT) in text
    assert PC_SCRIPT.is_absolute()


def test_section_pc_script_override_is_interpolated(tmp_path):
    """Passing an explicit pc_script overrides the default and appears in commands."""
    custom = Path("/opt/dialectic/tools/project_coordinator.py")
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
        pc_script=custom,
    )
    assert f"python3 {custom} --project {tmp_path} --summary" in text
    # Default path must not leak in when an override is provided.
    assert str(PC_SCRIPT) not in text


def test_section_default_pc_script_is_real_file(tmp_path):
    """The default coordinator-script path points at a file that actually exists.

    If this path ever goes wrong (refactor, rename, package move) the
    in-chain agent would be told to run a script that isn't there, which
    is silently as bad as `-m project_coordinator`. Pin the invariant.
    """
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    # Find the first real command line (leading whitespace, then "python3 ").
    # Prose mentions starting mid-line shouldn't match.
    suffix = f" --project {tmp_path} --summary"
    command_line = next(
        (
            line for line in text.splitlines()
            if line.lstrip().startswith("python3 ") and line.endswith(suffix)
        ),
        None,
    )
    assert command_line is not None, (
        "expected a `python3 <script> --project <dir> --summary` command line"
    )
    script = command_line.lstrip()[len("python3 "): -len(suffix)]
    assert Path(script).is_file(), f"rendered script path doesn't exist: {script}"
    assert Path(script).name == "project_coordinator.py"


def test_section_uses_imperative_voice_on_protocol(tmp_path):
    """Protocol must read as a mandate, not a suggestion."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    # At least one "You MUST" in the section (header or body).
    assert "You MUST" in text


def test_state_snapshot_renders_before_protocol_body(tmp_path):
    """The current state comes FIRST so agents see what's happening now
    before reading how to participate."""
    summary_text = "Active chains on /proj:\n  - chain-other: backend api"
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary=summary_text,
    )
    state_idx = text.find("### Current coordination state")
    claim_idx = text.find("### You MUST claim files before editing them")
    assert state_idx != -1, "State snapshot heading missing"
    assert claim_idx != -1, "Claim-protocol heading missing"
    assert state_idx < claim_idx, (
        "State snapshot must render before the protocol body — agents should "
        "see what's happening now before reading how to join."
    )


def test_focus_sentence_reflects_provided_focus(tmp_path):
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
        focus="backend api",
    )
    assert "'backend api'" in text
    # The "not set" fallback must not leak in when focus is supplied.
    assert "Your focus area is not set" not in text


def test_focus_sentence_falls_back_when_empty(tmp_path):
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    assert "Your focus area is not set" in text


def test_summary_block_omitted_when_empty(tmp_path):
    """No summary → no "Current coordination state" heading."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    assert "### Current coordination state" not in text


def test_summary_block_present_when_nonempty(tmp_path):
    summary_text = "Active chains on /proj:\n  - a: backend (last seen 5s ago)"
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary=summary_text,
    )
    assert "### Current coordination state" in text
    assert summary_text in text


def test_summary_block_is_a_code_fence(tmp_path):
    """The summary must land inside a ``` fence so markdown doesn't mangle it."""
    summary_text = "Active chains on /proj:\n  - a: stuff"
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary=summary_text,
    )
    # Opening and closing fences surround the summary.
    after_heading = text.split("### Current coordination state", 1)[1]
    opening = after_heading.find("```")
    closing = after_heading.rfind("```")
    assert opening != -1 and closing != -1 and closing > opening
    assert summary_text in after_heading[opening:closing]


def test_summary_trailing_newline_stripped(tmp_path):
    """A summary with a trailing newline shouldn't produce a blank line inside the fence."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="line1\nline2\n",
    )
    assert "line1\nline2\n```" in text
    assert "line1\nline2\n\n```" not in text


def test_accepts_path_like_project_dir(tmp_path):
    """Passing a str should be equivalent to passing a Path."""
    text_str = cp.claude_md_section_from_state(
        project_dir=str(tmp_path),
        chain_id=CHAIN_ID,
        summary="",
    )
    text_path = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    assert text_str == text_path


# --- claude_md_section: integration with project_coordinator ---

def test_section_embeds_real_summary_from_live_state(tmp_path):
    """With a registered chain the summary block contains that chain's focus."""
    pc.register_chain(tmp_path, "chain-other", focus="backend api")
    text = cp.claude_md_section(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        focus="frontend polish",
    )
    assert "### Current coordination state" in text
    assert "chain-other" in text
    assert "backend api" in text
    assert "frontend polish" in text  # our own focus in the header sentence


def test_empty_project_yields_no_chains_notice(tmp_path):
    """An empty project still gets a readable state block, not a crash."""
    # No registered chains.
    text = cp.claude_md_section(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
    )
    assert "### Current coordination state" in text
    assert "No active chains" in text


def test_include_summary_false_suppresses_state_block(tmp_path):
    """Explicit opt-out drops the state block entirely."""
    pc.register_chain(tmp_path, "chain-other", focus="backend api")
    text = cp.claude_md_section(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        include_summary=False,
    )
    assert "### Current coordination state" not in text
    # Protocol body still present.
    assert "### You MUST claim files before editing them" in text


def test_corrupt_coordination_file_does_not_raise(tmp_path):
    """A broken coordination.json shouldn't kill CLAUDE.md generation."""
    coord_dir = pc.coordination_dir(tmp_path)
    coord_dir.mkdir(parents=True, exist_ok=True)
    pc.coordination_path(tmp_path).write_text("{not json", encoding="utf-8")
    # Either the module's own try/except catches it, or project_coordinator's
    # recovery path runs — either way, no exception should reach the caller.
    text = cp.claude_md_section(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
    )
    assert "## Multi-chain coordination" in text
    assert "### You MUST claim files before editing them" in text


def test_render_summary_exception_is_swallowed(tmp_path, monkeypatch):
    """If render_summary itself raises, the protocol still reaches the agent."""
    def _boom(*a, **kw):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(pc, "render_summary", _boom)
    text = cp.claude_md_section(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
    )
    assert "### You MUST claim files before editing them" in text
    assert "(coordination state not available)" in text


# --- shape invariants that protect against accidental edits ---

def test_section_is_not_empty(tmp_path):
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    assert len(text) > 200  # a realistic protocol body is at least this long


def test_section_is_single_string_joinable(tmp_path):
    """The caller (chain.py) joins sections with \\n\\n — must not contain tabs / \\r."""
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
    )
    assert "\t" not in text
    assert "\r" not in text


@pytest.mark.parametrize(
    "focus", ["", "backend", "tests/auth only", "weird 'quoted' focus"]
)
def test_any_focus_renders_cleanly(tmp_path, focus):
    text = cp.claude_md_section_from_state(
        project_dir=tmp_path,
        chain_id=CHAIN_ID,
        summary="",
        focus=focus,
    )
    assert "## Multi-chain coordination" in text
    if focus:
        assert repr(focus) in text
