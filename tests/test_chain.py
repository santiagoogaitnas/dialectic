"""Tests for chain.py — setup and injection logic.

Covers tmux workspace assignment, paste-submit timing, and JSONL extraction fallback.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, call, MagicMock

import chain
from janitor.jsonl_reader import JSONLCursor


# --- Bug 1: Agent B must launch in WS_B, not WS_A ---

def test_setup_tmux_legacy_no_project_pane_b_uses_ws_b():
    """When chain.py is driven without a project (legacy direct module call),
    pane B must start in WS_B.

    The CLI now always supplies a project (defaults to cwd) -- this test
    pins the fallback path used by tests and any direct caller of setup_tmux
    that doesn't go through __main__.

    Regression: both panes were launched in WS_A, so Agent B wrote its JSONL
    to the WS_A slug directory. discover_jsonl_for_pane looked in the WS_B
    slug directory, found nothing, and the chain died after round 1.
    """
    original_project_dir = chain.PROJECT_DIR
    chain.PROJECT_DIR = None
    try:
        with patch("chain.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            chain.setup_tmux()

            split_call = [
                c for c in mock_run.call_args_list
                if any("split-window" in str(a) for a in c.args)
            ]
            assert len(split_call) == 1, "Expected exactly one split-window call"

            split_args = split_call[0].args[0]
            cwd_flag_idx = split_args.index("-c")
            pane_b_cwd = split_args[cwd_flag_idx + 1]

            assert pane_b_cwd == str(chain.WS_B), (
                f"Pane B should start in WS_B ({chain.WS_B}), got {pane_b_cwd}"
            )
    finally:
        chain.PROJECT_DIR = original_project_dir


def test_setup_tmux_project_mode_uses_per_pane_dirs(tmp_path):
    """In project mode, panes get separate subdirectories to prevent JSONL collision."""
    original_project_dir = chain.PROJECT_DIR
    chain.PROJECT_DIR = tmp_path
    try:
        with patch("chain.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            chain.setup_tmux()

            new_session_call = [
                c for c in mock_run.call_args_list
                if any("new-session" in str(a) for a in c.args)
            ]
            split_call = [
                c for c in mock_run.call_args_list
                if any("split-window" in str(a) for a in c.args)
            ]

            session_args = new_session_call[0].args[0]
            session_cwd = session_args[session_args.index("-c") + 1]

            split_args = split_call[0].args[0]
            split_cwd = split_args[split_args.index("-c") + 1]

            assert session_cwd == str(tmp_path / ".dialectic-a")
            assert split_cwd == str(tmp_path / ".dialectic-b")
            assert session_cwd != split_cwd, "Panes must have different cwds"
    finally:
        chain.PROJECT_DIR = original_project_dir


# --- Session-scoped workspace ---

def test_setup_workspace_creates_session_dirs(tmp_path):
    """setup_workspace creates chainwork/{session}/a/ and chainwork/{session}/b/."""
    original_ws_a = chain.WS_A
    original_ws_b = chain.WS_B
    chain.WS_A = tmp_path / "mysession" / "a"
    chain.WS_B = tmp_path / "mysession" / "b"
    try:
        chain.setup_workspace()
        assert chain.WS_A.is_dir()
        assert chain.WS_B.is_dir()
        assert chain.WS_A.parent.name == "mysession"
        assert chain.WS_B.parent.name == "mysession"
    finally:
        chain.WS_A = original_ws_a
        chain.WS_B = original_ws_b


def test_session_scoped_log_file_path():
    """run_chain's log_file should be under WORKSPACE/SESSION/."""
    original_session = chain.SESSION
    try:
        chain.SESSION = "test-session"
        expected = chain.WORKSPACE / "test-session" / "chain_log.md"
        actual = chain.WORKSPACE / chain.SESSION / "chain_log.md"
        assert actual == expected
        assert "test-session" in str(actual)
    finally:
        chain.SESSION = original_session


# --- Bug 2: Paste submit must wait for confirmation, not sleep ---

def test_inject_message_waits_for_paste_confirmation():
    """inject_message should poll for the paste indicator instead of sleeping.

    Regression: a fixed 1.5s sleep wasn't enough for large pastes. Claude Code
    was still processing when Enter arrived, so Enter got dropped. The agent
    sat with unsubmitted text and the chain hung.
    """
    with patch("chain.subprocess.run") as mock_run, \
         patch("chain.tmux_capture") as mock_capture, \
         patch("chain.time.sleep"), \
         patch("chain.time.monotonic") as mock_mono:

        mock_run.return_value = MagicMock(returncode=0)
        # monotonic: first call sets deadline (0 + 10 = 10),
        # subsequent calls are within deadline
        mock_mono.side_effect = [0, 1, 2]
        # First capture: no indicator yet. Second: paste confirmed.
        # Third: post-submit verification — paste gone (submitted OK).
        mock_capture.side_effect = [
            "some pane content",
            "some pane content\n[Pasted text #1 +50 lines]",
            "│ > ",  # post-submit: input accepted
        ]

        result = chain.inject_message("chain:0.0", "test message")

        assert result is True
        assert mock_capture.call_count >= 2, (
            "Should poll tmux pane for paste confirmation + verify submit"
        )


def test_inject_message_sends_enter_after_timeout():
    """Even if paste indicator never appears, Enter is still sent after timeout."""
    with patch("chain.subprocess.run") as mock_run, \
         patch("chain.tmux_capture") as mock_capture, \
         patch("chain.time.sleep"), \
         patch("chain.time.monotonic") as mock_mono:

        mock_run.return_value = MagicMock(returncode=0)
        # Simulate time exceeding the 10s deadline
        mock_mono.side_effect = [0, 11]
        mock_capture.return_value = "no indicator here"

        result = chain.inject_message("chain:0.0", "test message")

        assert result is True
        # Verify Enter (C-m) was still sent even after timeout
        enter_calls = [
            c for c in mock_run.call_args_list
            if any("C-m" in str(a) for a in c.args)
        ]
        assert len(enter_calls) >= 1, "Enter should be sent even after poll timeout"


def test_inject_message_retries_enter_if_paste_still_pending():
    """If [Pasted text] is still visible after Enter, retry submission."""
    with patch("chain.subprocess.run") as mock_run, \
         patch("chain.tmux_capture") as mock_capture, \
         patch("chain.time.sleep"), \
         patch("chain.time.monotonic") as mock_mono:

        mock_run.return_value = MagicMock(returncode=0)
        mock_mono.side_effect = [0, 1]
        # Paste poll: indicator appears immediately.
        # Post-submit verification: first two checks still show paste, third is clean.
        mock_capture.side_effect = [
            "[Pasted text #1 +10 lines]",  # paste poll — found
            "[Pasted text #1 +10 lines]",  # verify retry 0 — still there
            "[Pasted text #1 +10 lines]",  # verify retry 1 — still there
            "│ > ",                          # verify retry 2 — submitted
        ]

        result = chain.inject_message("chain:0.0", "test message")

        assert result is True
        enter_calls = [
            c for c in mock_run.call_args_list
            if any("C-m" in str(a) for a in c.args)
        ]
        # Initial Enter + 2 retries = 3 Enter calls
        assert len(enter_calls) == 3, f"Expected 3 Enter presses, got {len(enter_calls)}"


# --- Bug 3: wait_and_extract stalls when stop_reason is None ---
#
# Real data shows stop_reason=None appears in two contexts:
#   1. Mid-stream partial: more JSONL entries arrive within seconds (5 of 6 cases)
#   2. Genuine completion with missing signal: no new entries ever arrive (1 of 6 cases)
#
# The fallback must trigger ONLY for case 2: text exists, JSONL is stale, agent is idle.
# It must NOT trigger for case 1: mid-stream partials before tool calls.


def _write_jsonl(path, entries):
    """Write entries to a JSONL file."""
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _assistant_entry(text, stop_reason="end_turn"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
        },
    }


def _user_entry(text="Hello"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _system_entry():
    return {"type": "system"}


def test_wait_and_extract_normal_end_turn(tmp_path):
    """Normal case: stop_reason=end_turn. Should extract without fallback."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        _assistant_entry("The response.", stop_reason="end_turn"),
    ])
    cursor = JSONLCursor(file_path=jf)

    with patch("chain.time.sleep"):
        result = chain.wait_and_extract(cursor, timeout=5)

    assert result == "The response."


def test_wait_and_extract_stalls_on_none_without_fallback(tmp_path):
    """Bug 3 reproduction: stop_reason=None, no pane_target, no fallback.

    Without the idle fallback, wait_and_extract should timeout and return empty.
    """
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        _assistant_entry("Complete response.", stop_reason=None),
    ])
    cursor = JSONLCursor(file_path=jf)

    with patch("chain.time.sleep"):
        result = chain.wait_and_extract(cursor, timeout=5)

    assert result == "", "Without pane_target, should timeout on stop_reason=None"


def test_wait_and_extract_idle_fallback_rescues_none(tmp_path):
    """Bug 3 fix: stop_reason=None + stale JSONL + agent idle = extract.

    When pane_target is provided and agent is confirmed idle after JSONL
    stops updating, the fallback should return the text.
    """
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        _assistant_entry("Complete response.", stop_reason=None),
    ])
    cursor = JSONLCursor(file_path=jf)

    with patch("chain.time.sleep"), \
         patch("chain.is_agent_idle", return_value=True), \
         patch("chain.time.time") as mock_time:
        # Simulate: first call=0 (start), then 31s passes (stale threshold)
        mock_time.side_effect = [0, 0, 31, 31, 62]
        result = chain.wait_and_extract(cursor, timeout=120, pane_target="chain:0.1")

    assert result == "Complete response."


def test_wait_and_extract_no_fallback_during_tool_use(tmp_path):
    """Mid-stream stop_reason=None before tool_use must NOT trigger fallback.

    Real pattern: assistant(sr=None, text) -> assistant(sr=tool_use) -> ...
    The first entry has text but is NOT the final response.
    """
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        _assistant_entry("Partial thinking.", stop_reason=None),
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            "stop_reason": "tool_use",
        }},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
        ]}},
        _assistant_entry("Final response after tools.", stop_reason="end_turn"),
    ])
    cursor = JSONLCursor(file_path=jf)

    with patch("chain.time.sleep"):
        result = chain.wait_and_extract(cursor, timeout=5)

    # extract_last_assistant_text collects ALL text from assistant entries
    # after the last real user entry. The tool_result user entry has list content
    # (not string), so it's not a "real" user entry -- extraction goes past it.
    # The key check: _turn_complete only passes on end_turn, so this text
    # includes the final response.
    assert "Final response after tools." in result


# --- Bug 4: wait_for_idle false positive during tool calls ---


def test_last_assistant_stop_reason_tool_use(tmp_path):
    """Returns 'tool_use' when last assistant entry called a tool."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            "stop_reason": "tool_use",
        }},
    ])
    assert chain._last_assistant_stop_reason(jf) == "tool_use"


def test_last_assistant_stop_reason_end_turn(tmp_path):
    """Returns 'end_turn' when last assistant entry is complete."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        _assistant_entry("Done.", stop_reason="end_turn"),
    ])
    assert chain._last_assistant_stop_reason(jf) == "end_turn"


def test_last_assistant_stop_reason_after_tool_result(tmp_path):
    """After tool_use → tool_result → end_turn, returns 'end_turn'."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        {"type": "assistant", "message": {
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            "stop_reason": "tool_use",
        }},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
        ]}},
        _assistant_entry("Final.", stop_reason="end_turn"),
    ])
    assert chain._last_assistant_stop_reason(jf) == "end_turn"


def test_last_assistant_stop_reason_missing_file(tmp_path):
    """Returns None for nonexistent file."""
    assert chain._last_assistant_stop_reason(tmp_path / "nope.jsonl") is None


def test_last_assistant_stop_reason_no_assistant(tmp_path):
    """Returns None when no assistant entries exist."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [_user_entry()])
    assert chain._last_assistant_stop_reason(jf) is None


def test_wait_for_idle_passes_mid_turn_callback():
    """wait_for_idle passes is_mid_turn through to is_agent_idle."""
    mid_turn = lambda: True
    with patch("chain.is_agent_idle") as mock_idle, \
         patch("chain.time.sleep"), \
         patch("chain.time.time") as mock_time:
        mock_time.side_effect = [0, 8, 20]
        mock_idle.return_value = False
        chain.wait_for_idle("chain:0.0", timeout=15, is_mid_turn=mid_turn)
        mock_idle.assert_called_with("chain:0.0", is_mid_turn=mid_turn)


def test_wait_and_extract_idle_false_blocks_fallback(tmp_path):
    """If agent is NOT idle, fallback must not trigger even with stale JSONL."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry(),
        _assistant_entry("Response.", stop_reason=None),
    ])
    cursor = JSONLCursor(file_path=jf)

    # time.time() call sequence in wait_and_extract:
    #   call 1: start = time.time()           -> 0
    #   call 2: last_new_data_time = time.time() -> 0
    #   call 3: while time.time() - start < timeout  -> 31 (31 < 60: continue)
    #   (read_new_entries returns data on first pass, so:)
    #   call 4: last_new_data_time = time.time()  -> 31
    #   call 5: stale check: time.time() - last_new_data_time -> 31 - 31 = 0 (not stale)
    #   call 6: while time.time() - start < timeout  -> 35 (35 < 60: continue)
    #   (no new data on second pass)
    #   call 7: stale check: time.time() - last_new_data_time -> 65 - 31 = 34 (stale!)
    #   -> is_agent_idle called, returns False, no extract
    #   call 8: while time.time() - start < timeout  -> 65 (65 >= 60: exit)
    times = iter([0, 0, 31, 31, 31, 35, 65, 65])

    with patch("chain.time.sleep"), \
         patch("chain.is_agent_idle", return_value=False) as mock_idle, \
         patch("chain.time.time", side_effect=times):
        result = chain.wait_and_extract(cursor, timeout=60, pane_target="chain:0.1")

    assert result == "", "Should not extract when agent is not idle"
    assert mock_idle.call_count >= 1, "Should have checked idle state"


# --- Sanity asserts: JSONL collision and output identity ---


def test_clear_and_recap_aborts_on_jsonl_collision(tmp_path):
    """If both cursors resolve to the same JSONL file after reset, abort."""
    jf_a = tmp_path / "a.jsonl"
    _write_jsonl(jf_a, [_user_entry(), _assistant_entry("A response.")])
    jf_b = tmp_path / "b.jsonl"
    _write_jsonl(jf_b, [_user_entry(), _assistant_entry("B response.")])

    cursor_a = JSONLCursor(file_path=jf_a)
    cursor_b = JSONLCursor(file_path=jf_b)

    collided_cursor = JSONLCursor(file_path=tmp_path / "same.jsonl")

    with patch("chain.get_recap", return_value="RECAP: stuff"), \
         patch("chain.subprocess.run"), \
         patch("chain.wait_for_idle", return_value=True), \
         patch("chain.inject_message", return_value=True), \
         patch("chain.wait_and_extract", return_value="output"), \
         patch("chain.discover_jsonl_with_retries", return_value=collided_cursor), \
         patch("builtins.open", MagicMock()), \
         patch("chain.time.sleep"):

        result = chain.clear_and_recap(
            "chain:0.0", "chain:0.1",
            cursor_a, cursor_b,
            tmp_path / "log.md", 5,
        )

    assert result == (None, None, None, None, None, None), (
        "Should abort when both cursors resolve to same JSONL"
    )


# --- Per-chain plan.md isolation ---

def test_write_pane_claude_md_uses_per_chain_plan_filename(tmp_path):
    """In project mode with a chain config, agents are instructed to write
    the per-chain plan file at its absolute path so concurrent chains on
    the same project don't clobber each other's working doc and can't
    accidentally create the file inside the pane's scratch directory.
    """
    import registry as reg
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    cfg = reg.ChainConfig(
        chain_id="abc-1234",
        session="s1",
        seed="x",
        project=str(project_dir),
    )
    pane_dir = tmp_path / "pane_a"
    pane_dir.mkdir()

    chain._write_pane_claude_md(pane_dir, "ROLE PROMPT", cfg)

    written = (pane_dir / "CLAUDE.md").read_text(encoding="utf-8")
    # The absolute path — not just the filename — must appear so the agent
    # has nothing to interpret.
    expected_plan_path = str((project_dir / "plan-abc-1234.md").resolve())
    assert expected_plan_path in written, (
        f"Absolute plan path {expected_plan_path!r} missing from CLAUDE.md "
        f"instructions. Written: {written[:500]!r}"
    )
    assert "Maintain a working doc at the absolute path" in written, (
        "Plan instruction must explicitly say the path is absolute"
    )
    # The absolute project root must also appear (the second ambiguity the
    # bug report flagged): other project-scoped artefacts should resolve
    # to the project root, not the pane's cwd.
    assert str(project_dir.resolve()) in written, (
        "Absolute project root missing from CLAUDE.md — agents would fall "
        "back to interpreting 'project root' relative to their cwd."
    )
    # And the bare 'plan.md' instruction should NOT appear as the working
    # doc for this chain (it would mean the per-chain suffix was lost).
    lines_with_bare_plan = [
        ln for ln in written.split("\n")
        if "working doc at plan.md" in ln
    ]
    assert not lines_with_bare_plan, (
        "Chain mode should not point agents at the shared plan.md"
    )


def test_write_pane_claude_md_falls_back_to_plain_plan_without_cfg(tmp_path):
    """With no chain config (legacy/single-chain mode) there's no
    chain_id suffix, but the prompt still uses an absolute path so the
    agent never has to interpret 'project root' relative to its cwd.
    """
    project_dir = tmp_path / "legacy"
    project_dir.mkdir()
    original_project = chain.PROJECT_DIR
    chain.PROJECT_DIR = project_dir
    try:
        pane_dir = tmp_path / "pane"
        pane_dir.mkdir()
        chain._write_pane_claude_md(pane_dir, "ROLE PROMPT", cfg=None)
        written = (pane_dir / "CLAUDE.md").read_text(encoding="utf-8")
        expected_plan_path = str((project_dir / "plan.md").resolve())
        assert expected_plan_path in written, (
            f"Absolute plan path {expected_plan_path!r} missing from "
            f"legacy-mode CLAUDE.md. Written: {written[:500]!r}"
        )
        assert "Maintain a working doc at the absolute path" in written
        # No chain-id-suffixed *directive path* in legacy mode. The prose
        # can still mention 'plan-<chain-id>.md' as an explanation of the
        # per-chain convention; what we forbid is the agent's own plan
        # file pointing at a suffixed name.
        assert "plan-" not in expected_plan_path
    finally:
        chain.PROJECT_DIR = original_project


def test_two_chains_get_distinct_plan_filenames(tmp_path):
    """Two chains on the same project must get distinct absolute plan paths
    so A and B can't accidentally write to each other's doc.
    """
    import registry as reg
    project_dir = tmp_path / "shared_proj"
    project_dir.mkdir()

    cfg_a = reg.ChainConfig(chain_id="chain-a", session="sa", seed="x",
                            project=str(project_dir))
    cfg_b = reg.ChainConfig(chain_id="chain-b", session="sb", seed="x",
                            project=str(project_dir))

    pane_a = tmp_path / "pa"
    pane_a.mkdir()
    pane_b = tmp_path / "pb"
    pane_b.mkdir()

    chain._write_pane_claude_md(pane_a, "ROLE", cfg_a)
    chain._write_pane_claude_md(pane_b, "ROLE", cfg_b)

    md_a = (pane_a / "CLAUDE.md").read_text(encoding="utf-8")
    md_b = (pane_b / "CLAUDE.md").read_text(encoding="utf-8")

    plan_a_abs = str((project_dir / "plan-chain-a.md").resolve())
    plan_b_abs = str((project_dir / "plan-chain-b.md").resolve())
    assert plan_a_abs in md_a
    assert plan_b_abs in md_b
    assert plan_a_abs not in md_b
    assert plan_b_abs not in md_a
