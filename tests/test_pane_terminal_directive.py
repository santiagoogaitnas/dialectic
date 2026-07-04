"""Tests for the pane terminal directive in CLAUDE.md.

The relay drives each pane by reading its screen (janitor/injection.py),
so an in-pane agent that opens an interactive question prompt or a
team/multi-terminal display takeover stalls the chain: the prompt waits
for a human who isn't there, and a display reformat removes the patterns
idle detection scrapes for (reproduced on Claude Code 2.1.201, where the
agent-team status panel narrows the transcript column until no ready
pattern survives).

This is deliberately an INSTRUCTION, not a code-level tool block: which
UI modes disrupt the scrape varies across Claude Code versions, so the
constraint lives in the pane CLAUDE.md where it can steer behavior
without permanently stripping capabilities. The directive is also kept
terse on purpose -- it names the two forbidden surfaces without
describing the relay's mechanics to the agent. These tests pin two
things: the directive is always written, and the launch command stays
free of tool restrictions.
"""

from unittest.mock import patch

import chain
from registry import ChainConfig


def _sent_command(mock_run):
    """The command string start_agent typed into the pane."""
    (args, _), = [c for c in mock_run.call_args_list]
    # args[0] == ["tmux", "send-keys", "-t", pane, cmd, "C-m"]
    return args[0][4]


def test_start_agent_command_has_no_tool_restrictions(monkeypatch):
    """The launch line is the plain claude invocation — the terminal
    directive is instruction-only, nothing is stripped in code."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cfg = ChainConfig(chain_id="c-cmd", session="s-cmd", seed="x")
    with patch.object(chain.subprocess, "run") as mock_run:
        chain.start_agent(0, "builder", cfg)
    cmd = _sent_command(mock_run)
    assert cmd == 'claude --dangerously-skip-permissions --name "builder"'


def test_start_agent_keeps_config_dir_prefix(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/some-config")
    cfg = ChainConfig(chain_id="c-env", session="s-env", seed="x")
    with patch.object(chain.subprocess, "run") as mock_run:
        chain.start_agent(1, "thinker", cfg)
    cmd = _sent_command(mock_run)
    assert cmd == ('CLAUDE_CONFIG_DIR="/tmp/some-config" '
                   'claude --dangerously-skip-permissions --name "thinker"')


def test_pane_claude_md_contains_terminal_directive(tmp_path):
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    cfg = ChainConfig(chain_id="c-md", session="s-md", seed="x",
                      project=str(project))
    chain._write_pane_claude_md(pane_dir, "ROLE PROMPT", cfg)
    content = (pane_dir / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Never open an interactive question prompt" in content
    assert "Never use team-create or multi-terminal display modes" in content
    # The directive must not explain the relay's mechanics to the agent.
    assert "no human" not in content.lower()
    assert "unattended" not in content.lower()


def test_directive_present_without_project_too(tmp_path):
    """Workspace-mode panes are scraped the same way; the directive is
    unconditional."""
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()
    with patch.object(chain, "PROJECT_DIR", None):
        chain._write_pane_claude_md(pane_dir, "ROLE PROMPT", None)
    content = (pane_dir / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Never open an interactive question prompt" in content
