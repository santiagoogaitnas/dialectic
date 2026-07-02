"""Integration tests for chain.wait_and_extract and its relay-gate helpers.

The existing coverage is split across tests/test_extraction.py (primitives:
extract_last_assistant_text + _turn_complete) and tests/test_chain.py (a
handful of file-backed happy/stall paths). This file fills the remaining
gaps by mocking chain.read_new_entries so we can drive the polling loop
deterministically across multiple iterations without writing to disk, and
by exercising STALE_THRESHOLD boundary conditions, multi-step tool chains,
and helper edge cases.

The functions under test are the relay gate: they decide when an agent's
turn has finished and the loop can hand off. The loop is sacred -- these
tests are the safety net that proves the gate doesn't prematurely close
or silently hang. None of the tests touch tmux, subprocess, or the
network.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import chain
from janitor.jsonl_reader import JSONLCursor


# --- Helpers ---


def _user(text="hi"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text, stop_reason="end_turn"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
        },
    }


def _assistant_tool_use(tool_id="t1", tool_name="Bash"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}}],
            "stop_reason": "tool_use",
        },
    }


def _tool_result(tool_id="t1", content="output"):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content}
            ],
        },
    }


def _system():
    return {"type": "system", "message": {}}


def _write_lines(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _make_cursor(tmp_path):
    """A cursor with a real (but empty) file so read_new_entries can be mocked
    without the cursor's file_path.exists() short-circuiting the call.
    """
    jf = tmp_path / "session.jsonl"
    jf.write_text("")
    return JSONLCursor(file_path=jf)


# --- wait_and_extract: polling dynamics via mocked read_new_entries ---


def test_wait_and_extract_gathers_entries_across_multiple_polls(tmp_path):
    """Entries arriving across three successive polls should all feed into
    the accumulator before end_turn triggers extraction.

    Simulates the live case: user message on poll 1, partial assistant text
    on poll 2, end_turn assistant on poll 3.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [_user()],                                     # poll 1
        [_assistant_text("partial", stop_reason=None)],  # poll 2 (incomplete)
        [_assistant_text("final answer", stop_reason="end_turn")],  # poll 3
    ])

    def fake_read(c):
        try:
            return next(polls)
        except StopIteration:
            return []

    with patch("chain.read_new_entries", side_effect=fake_read), \
         patch("chain.time.sleep"):
        result = chain.wait_and_extract(cursor, timeout=30)

    # extract_last_assistant_text collects every assistant text after the
    # last real user entry -- both the partial and the final message make it
    # into the returned string.
    assert "partial" in result
    assert "final answer" in result


def test_wait_and_extract_terminates_on_system_entry_without_end_turn(tmp_path):
    """A system entry after an assistant turn signals the turn completed,
    even when stop_reason is None. _turn_complete honors this; the gate
    must not hang waiting for an end_turn that will never arrive.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [
            _user(),
            _assistant_text("answer text", stop_reason=None),
            _system(),
        ],
    ])

    with patch("chain.read_new_entries", side_effect=lambda c: next(polls, [])), \
         patch("chain.time.sleep"):
        result = chain.wait_and_extract(cursor, timeout=30)

    assert result == "answer text"


def test_wait_and_extract_multi_step_tool_chain_completes_on_final_end_turn(tmp_path):
    """A realistic multi-step tool chain -- text, tool_use, tool_result,
    text, tool_use, tool_result, final text with end_turn -- should
    accumulate and only return once end_turn lands.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [_user(), _assistant_text("Reading files...", stop_reason=None)],
        [_assistant_tool_use(tool_id="a"), _tool_result(tool_id="a", content="file ok")],
        [_assistant_text("Partial findings.", stop_reason=None)],
        [_assistant_tool_use(tool_id="b"), _tool_result(tool_id="b", content="done")],
        [_assistant_text("Full analysis complete.", stop_reason="end_turn")],
    ])

    with patch("chain.read_new_entries", side_effect=lambda c: next(polls, [])), \
         patch("chain.time.sleep"):
        result = chain.wait_and_extract(cursor, timeout=60)

    # All three assistant-text blocks reach the extractor (tool_result is
    # not a "real" user entry for the extractor's reverse scan).
    assert "Reading files" in result
    assert "Partial findings" in result
    assert "Full analysis complete" in result


def test_wait_and_extract_pure_tool_use_turn_does_not_complete(tmp_path):
    """An assistant turn that is ONLY tool_use -- no text block, no
    end_turn -- must not cause a premature return. The gate should keep
    polling.
    """
    cursor = _make_cursor(tmp_path)
    call_count = {"n": 0}

    def fake_read(c):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [_user(), _assistant_tool_use()]
        return []

    # Force a hard cutoff once we've verified the loop kept polling past
    # the tool_use turn. We use a time stub that returns 0 for the first
    # few calls and then jumps past the timeout.
    times = iter([0, 0] + [0] * 20 + [1000] * 20)
    with patch("chain.read_new_entries", side_effect=fake_read), \
         patch("chain.time.sleep"), \
         patch("chain.time.time", side_effect=lambda: next(times, 1000)):
        result = chain.wait_and_extract(cursor, timeout=30)

    # No text block in the turn -> extract_last_assistant_text returns "",
    # and the loop times out rather than returning a fabricated response.
    assert result == ""
    assert call_count["n"] >= 2, "Loop should poll more than once when turn is tool_use-only"


# --- wait_and_extract: STALE_THRESHOLD fallback edges ---


def test_wait_and_extract_no_fallback_without_pane_target(tmp_path):
    """Fallback is gated on pane_target being truthy. Without it, even text
    + stale JSONL + long wait must NOT trigger extraction -- the caller
    must opt into fallback explicitly.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [_user(), _assistant_text("text body", stop_reason=None)],
    ])
    # time.time(): start=0, last_new=0, loop=0, post-read last_new=0,
    #   stale=0, sleep; loop=100, stale=100, sleep; loop=100 (timeout hit).
    times = iter([0, 0, 0, 0, 0, 100, 100, 100])

    with patch("chain.read_new_entries", side_effect=lambda c: next(polls, [])), \
         patch("chain.time.sleep"), \
         patch("chain.time.time", side_effect=lambda: next(times, 1000)), \
         patch("chain.is_agent_idle") as mock_idle:
        result = chain.wait_and_extract(cursor, timeout=60, pane_target=None)

    assert result == ""
    mock_idle.assert_not_called()


def test_wait_and_extract_fallback_requires_text(tmp_path):
    """The fallback branch requires that extract_last_assistant_text
    already found text. If no text has accumulated (e.g., assistant has
    only produced tool_use turns), stale + idle must NOT extract.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [_user(), _assistant_tool_use()],  # no text
    ])
    times = iter([0, 0, 0, 0, 0, 100, 100, 100])

    with patch("chain.read_new_entries", side_effect=lambda c: next(polls, [])), \
         patch("chain.time.sleep"), \
         patch("chain.time.time", side_effect=lambda: next(times, 1000)), \
         patch("chain.is_agent_idle", return_value=True) as mock_idle:
        result = chain.wait_and_extract(
            cursor, timeout=60, pane_target="chain:0.0",
        )

    assert result == ""
    # With no text, the fallback branch short-circuits before idle is even
    # consulted -- but is_agent_idle has a lazy-eval guard so it may not
    # be called at all. Either way, no extraction happened.


def test_wait_and_extract_fallback_triggers_when_all_conditions_met(tmp_path):
    """text + stale JSONL + is_agent_idle True + pane_target set ->
    fallback extracts. Mirrors the live idle-rescue case but via mocks.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [_user(), _assistant_text("rescued response", stop_reason=None)],
    ])
    # Sequence: start=0, last_new=0, while-check=0, post-read last_new=0,
    #   stale=0 (not yet stale), sleep. Next iter: while-check=45 (still
    #   under 60s timeout), stale=45 (> 30 threshold -> fallback path).
    times = iter([0, 0, 0, 0, 0, 45, 45])

    with patch("chain.read_new_entries", side_effect=lambda c: next(polls, [])), \
         patch("chain.time.sleep"), \
         patch("chain.time.time", side_effect=lambda: next(times, 1000)), \
         patch("chain.is_agent_idle", return_value=True):
        result = chain.wait_and_extract(
            cursor, timeout=60, pane_target="chain:0.0",
        )

    assert result == "rescued response"


def test_wait_and_extract_stale_timer_resets_when_new_data_arrives(tmp_path):
    """Every call to read_new_entries that returns non-empty entries
    must refresh last_new_data_time, so the stale clock restarts. This
    pins the invariant that a chatty agent never trips the fallback.
    """
    cursor = _make_cursor(tmp_path)
    polls = iter([
        [_user(), _assistant_text("chunk 1", stop_reason=None)],
        [_assistant_text("chunk 2", stop_reason=None)],
        [_assistant_text("final", stop_reason="end_turn")],
    ])
    # Walk time forward in 20s increments (well under 30s threshold) so
    # stale never fires. Each poll's returned entries reset last_new.
    # start=0, last_new=0, loop=0, post-read last_new=0, stale=0, sleep
    # loop=20, post-read last_new=20, stale=0, sleep
    # loop=40, post-read last_new=40, stale=0, end_turn -> return
    times = iter([0, 0, 0, 0, 0, 20, 20, 20, 40, 40, 40])
    with patch("chain.read_new_entries", side_effect=lambda c: next(polls, [])), \
         patch("chain.time.sleep"), \
         patch("chain.time.time", side_effect=lambda: next(times, 1000)), \
         patch("chain.is_agent_idle") as mock_idle:
        result = chain.wait_and_extract(
            cursor, timeout=120, pane_target="chain:0.0",
        )

    # end_turn on poll 3 resolves the gate -- fallback never needed.
    assert "final" in result
    mock_idle.assert_not_called()


def test_wait_and_extract_empty_stream_times_out(tmp_path):
    """If read_new_entries always returns [] (no JSONL writes at all),
    wait_and_extract must return "" when the timeout expires -- never
    hang, never raise.
    """
    cursor = _make_cursor(tmp_path)
    # time.time() pattern: start=0, last_new=0, then each loop:
    #   while-check, stale-check (since text is "", fallback skipped), sleep
    # Jump forward aggressively so the loop exits quickly.
    times = iter([0, 0, 0, 0, 100, 100, 100])

    with patch("chain.read_new_entries", return_value=[]), \
         patch("chain.time.sleep"), \
         patch("chain.time.time", side_effect=lambda: next(times, 1000)):
        result = chain.wait_and_extract(cursor, timeout=30)

    assert result == ""


# --- _last_assistant_stop_reason: tail-peek edge cases ---


def test_last_assistant_stop_reason_empty_file(tmp_path):
    """An empty JSONL file must return None, not raise."""
    jf = tmp_path / "empty.jsonl"
    jf.write_text("")
    assert chain._last_assistant_stop_reason(jf) is None


def test_last_assistant_stop_reason_skips_malformed_lines(tmp_path):
    """Malformed lines in the tail must be silently skipped; the helper
    keeps scanning for the most recent parseable assistant entry.
    """
    jf = tmp_path / "mixed.jsonl"
    good = json.dumps(_assistant_text("ok", stop_reason="tool_use"))
    with open(jf, "w") as f:
        f.write(good + "\n")
        f.write("{not valid json\n")
        f.write("garbage line no braces\n")
    assert chain._last_assistant_stop_reason(jf) == "tool_use"


def test_last_assistant_stop_reason_returns_latest_assistant_not_earliest(tmp_path):
    """When the tail contains multiple assistant entries, the latest one
    wins -- the helper scans in reverse order.
    """
    jf = tmp_path / "multi.jsonl"
    _write_lines(jf, [
        _user(),
        _assistant_text("first", stop_reason="end_turn"),
        _assistant_tool_use(),   # stop_reason="tool_use"
    ])
    assert chain._last_assistant_stop_reason(jf) == "tool_use"


def test_last_assistant_stop_reason_skips_non_assistant_tail(tmp_path):
    """If the tail ends with a user/system entry, the helper keeps
    scanning upward for the most recent assistant entry.
    """
    jf = tmp_path / "tail.jsonl"
    _write_lines(jf, [
        _user(),
        _assistant_text("response", stop_reason="end_turn"),
        _system(),
        _user("follow-up"),
    ])
    assert chain._last_assistant_stop_reason(jf) == "end_turn"


def test_last_assistant_stop_reason_handles_missing_stop_reason_field(tmp_path):
    """An assistant entry whose message has no stop_reason key returns
    None for that entry -- the helper does not crash on the missing key.
    """
    jf = tmp_path / "nostop.jsonl"
    entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
    _write_lines(jf, [_user(), entry])
    # dict.get("stop_reason") -> None; the helper returns that None rather
    # than raising. The caller is expected to treat None as "unknown".
    assert chain._last_assistant_stop_reason(jf) is None


def test_last_assistant_stop_reason_large_file_reads_only_tail(tmp_path):
    """The helper reads at most the last 8 KB of the file. A large file
    with a clean assistant entry near the end must still resolve correctly.
    """
    jf = tmp_path / "large.jsonl"
    padding_entry = _assistant_text("padding" * 200, stop_reason="tool_use")
    # ~80 padding lines ~ 160 KB -- well past the 8 KB tail window.
    tail_entry = _assistant_text("final", stop_reason="end_turn")
    _write_lines(jf, [_user()] + [padding_entry] * 80 + [tail_entry])
    assert chain._last_assistant_stop_reason(jf) == "end_turn"


# --- _strip_framing_tags: edge cases ---


def test_strip_framing_tags_removes_multiple_role_blocks(tmp_path):
    """More than one <role> block in the same text must all be stripped,
    not just the first. Non-greedy regex + DOTALL catches each.
    """
    text = (
        "<role>\nfirst role\n</role>\n\n"
        "middle keep me\n\n"
        "<role>\nsecond role\n</role>\n\n"
        "tail keep me"
    )
    out = chain._strip_framing_tags(text)
    assert "middle keep me" in out
    assert "tail keep me" in out
    assert "first role" not in out
    assert "second role" not in out


def test_strip_framing_tags_unclosed_tag_preserved(tmp_path):
    """A malformed framing tag (opening tag with no matching close) is
    NOT a framing block -- the regex requires both sides. The text
    survives verbatim rather than eating the rest of the document.
    """
    text = "<role>\nbuilder prompt (never closed)\n\nActual body here."
    out = chain._strip_framing_tags(text)
    assert "builder prompt" in out
    assert "Actual body here." in out


def test_strip_framing_tags_tag_on_single_line_not_stripped(tmp_path):
    """The stripper requires a newline between the opening tag and its
    body, and another before the close. An inline single-line fragment
    that merely mentions the tag characters is preserved.
    """
    text = "Mentioning <role>inline</role> in prose keeps this intact."
    out = chain._strip_framing_tags(text)
    assert out == text


def test_strip_framing_tags_only_framing_returns_empty(tmp_path):
    """Text that is nothing but framing tags must strip to empty, not
    to the tags themselves. After the regex pass, strip() trims the
    trailing whitespace.
    """
    text = (
        "<role>\nall framing\n</role>\n\n"
        "<project>\nnothing else\n</project>\n\n"
        "<format>\npure frame\n</format>"
    )
    assert chain._strip_framing_tags(text) == ""
