"""Tests for extract_last_assistant_text.

Reproduces the bug where tool-heavy turns return short fragments
instead of substantive text.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from chain import extract_last_assistant_text, _turn_complete


def _make_entry(type, text_blocks=None, tool_use_count=0, stop_reason=None):
    content = []
    for t in (text_blocks or []):
        content.append({"type": "text", "text": t})
    for _ in range(tool_use_count):
        content.append({"type": "tool_use", "id": "x", "name": "Read", "input": {}})
    return {"type": type, "message": {"content": content, "stop_reason": stop_reason}}


def _make_user_entry(text="hello"):
    return {"type": "user", "message": {"content": text}}


def _make_tool_result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


def test_simple_text():
    """Basic case — single assistant entry with text."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["This is a long substantive response about the problem."]),
    ]
    result = extract_last_assistant_text(entries)
    assert len(result) > 40


def test_tool_then_short_summary():
    """Bug case from calendar run: long text, tool call, then short ending.

    Agent A: [text 2077 chars] → [tool_use write plan.md] → [tool_result] → [text 133 chars]
    Current code returns the 133-char entry. Should return the 2077-char one (or both).
    """
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["A" * 2077], tool_use_count=1),  # real analysis + tool call
        _make_tool_result(),
        _make_entry("assistant", ["Short follow-up question here."]),  # 30 chars
    ]
    result = extract_last_assistant_text(entries)
    assert len(result) > 500, f"Got {len(result)} chars, expected >500"


def test_many_tool_calls_short_text():
    """Bug case from calendar run Agent B: almost all tool calls, tiny text blocks.

    Agent B: [text 68] → [18 tool calls] → [text 112] → [tool call] → [text 47]
    Should return all text combined, not just the 47-char ending.
    """
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Initial thought about the problem, reading files."]),
        _make_entry("assistant", tool_use_count=1),
        _make_tool_result(),
        _make_entry("assistant", tool_use_count=1),
        _make_tool_result(),
        _make_entry("assistant", tool_use_count=1),
        _make_tool_result(),
        _make_entry("assistant", ["Here is my full analysis of what I found in the codebase. " * 5]),
        _make_entry("assistant", tool_use_count=1),
        _make_tool_result(),
        _make_entry("assistant", ["Updated plan.md with findings."]),
    ]
    result = extract_last_assistant_text(entries)
    assert len(result) > 100, f"Got {len(result)} chars, expected >100"


def test_no_regression_simple_case():
    """Original behavior: last entry has the good text. Should still work."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", tool_use_count=1),
        _make_tool_result(),
        _make_entry("assistant", ["This is the substantive response after reading. " * 10], stop_reason="end_turn"),
    ]
    result = extract_last_assistant_text(entries)
    assert len(result) > 400


# --- _turn_complete tests ---

def test_turn_complete_end_turn():
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Response."], stop_reason="end_turn"),
    ]
    assert _turn_complete(entries) is True


def test_turn_complete_mid_turn_text():
    """The calendar bug: text emitted mid-turn with stop_reason=None."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Long analysis here."], stop_reason=None),
    ]
    assert _turn_complete(entries) is False


def test_turn_complete_tool_use():
    entries = [
        _make_user_entry(),
        _make_entry("assistant", tool_use_count=1, stop_reason="tool_use"),
    ]
    assert _turn_complete(entries) is False


def test_turn_complete_skips_non_assistant():
    """end_turn assistant followed by system/metadata entries."""
    entries = [
        _make_entry("assistant", ["Done."], stop_reason="end_turn"),
        {"type": "system", "message": {}},
    ]
    assert _turn_complete(entries) is True


def test_turn_complete_system_entry():
    """Thinker case: stop_reason=None but system entry follows."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Full response here."], stop_reason=None),
        {"type": "system", "message": {}},
    ]
    assert _turn_complete(entries) is True


def test_turn_complete_no_entries():
    assert _turn_complete([]) is False


# --- extract_last_assistant_text edge cases ---

def test_extract_empty_entries_returns_empty():
    assert extract_last_assistant_text([]) == ""


def test_extract_only_user_entries_returns_empty():
    """No assistant entries at all -> empty string, not a crash."""
    entries = [_make_user_entry("hi"), _make_user_entry("still there?")]
    assert extract_last_assistant_text(entries) == ""


def test_extract_only_tool_use_returns_empty():
    """Assistant turn that's purely tool_use (no text block) -> empty.

    The relay uses this signal to wait or fall back; it must not fabricate
    content from the tool_use input.
    """
    entries = [
        _make_user_entry(),
        _make_entry("assistant", tool_use_count=3),
    ]
    assert extract_last_assistant_text(entries) == ""


def test_extract_skips_thinking_blocks():
    """Extended-thinking content ('thinking' blocks) is internal reasoning.

    Only 'text' blocks are user-facing output. Mixing a thinking block with
    a text block should return only the text, never the thinking payload.
    """
    entries = [
        _make_user_entry(),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "SECRET private reasoning"},
                    {"type": "text", "text": "Public answer here."},
                ],
                "stop_reason": "end_turn",
            },
        },
    ]
    out = extract_last_assistant_text(entries)
    assert "Public answer here." in out
    assert "SECRET" not in out


def test_extract_whitespace_only_text_ignored():
    """A text block whose payload is only whitespace must not be kept.

    Otherwise the extractor would produce cosmetic blanks that get injected
    into the other agent and look like real content.
    """
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["   \n\t  "], stop_reason="end_turn"),
    ]
    assert extract_last_assistant_text(entries) == ""


def test_extract_content_none_does_not_crash():
    """Malformed/legacy entries with content=None must be skipped silently.

    The JSONL format occasionally emits odd shapes on truncation; the relay
    can't afford to crash mid-round over one bad line.
    """
    entries = [
        _make_user_entry(),
        {"type": "assistant", "message": {"content": None}},
        _make_entry("assistant", ["Fallback text that should be returned."], stop_reason="end_turn"),
    ]
    out = extract_last_assistant_text(entries)
    assert "Fallback text" in out


def test_extract_content_string_on_assistant_skipped():
    """Assistant entries with content-as-string (not a block list) are skipped.

    Real assistant entries always carry a block list; a string there is an
    out-of-band shape we shouldn't try to interpret as text.
    """
    entries = [
        _make_user_entry(),
        {"type": "assistant", "message": {"content": "not a list"}},
        _make_entry("assistant", ["Real text block here."], stop_reason="end_turn"),
    ]
    out = extract_last_assistant_text(entries)
    assert "Real text block" in out
    assert "not a list" not in out


def test_extract_multiple_text_blocks_in_same_entry_joined():
    """Two text blocks in one assistant entry are both preserved, newline-joined."""
    entries = [
        _make_user_entry(),
        _make_entry(
            "assistant",
            ["First paragraph.", "Second paragraph."],
            stop_reason="end_turn",
        ),
    ]
    out = extract_last_assistant_text(entries)
    assert "First paragraph." in out
    assert "Second paragraph." in out


def test_extract_stops_at_real_user_entry():
    """A real user message ends the turn; earlier assistant text is discarded.

    Only the *current* turn's output should be injected into the other agent.
    """
    entries = [
        _make_user_entry("old prompt"),
        _make_entry("assistant", ["PRIOR_TURN_CONTENT"], stop_reason="end_turn"),
        _make_user_entry("new prompt"),
        _make_entry("assistant", ["NEW_TURN_CONTENT"], stop_reason="end_turn"),
    ]
    out = extract_last_assistant_text(entries)
    assert "NEW_TURN_CONTENT" in out
    assert "PRIOR_TURN_CONTENT" not in out


def test_extract_tool_result_user_does_not_break_loop():
    """tool_result is emitted as a 'user' entry with list content. It must not be
    treated as a real user message — otherwise extraction would stop early and
    miss the assistant text that followed the tool_use earlier in the same turn.
    """
    entries = [
        _make_user_entry("start"),
        _make_entry("assistant", ["EARLY_TEXT"], tool_use_count=1),
        _make_tool_result(),  # 'user' type with list content — NOT a real user
        _make_entry("assistant", ["LATE_TEXT"], stop_reason="end_turn"),
    ]
    out = extract_last_assistant_text(entries)
    assert "EARLY_TEXT" in out
    assert "LATE_TEXT" in out


# --- _turn_complete edge cases: every stop_reason shape ---

def test_turn_complete_max_tokens_is_not_done():
    """stop_reason='max_tokens' means the model got cut off mid-response.
    The turn is NOT complete — the agent would continue if re-prompted.
    """
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Half a thought, then..."],
                    stop_reason="max_tokens"),
    ]
    assert _turn_complete(entries) is False


def test_turn_complete_stop_sequence_is_not_done():
    """stop_reason='stop_sequence' is not end_turn; don't inject yet."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Text."], stop_reason="stop_sequence"),
    ]
    assert _turn_complete(entries) is False


def test_turn_complete_refusal_is_not_done():
    """Refusal stop — still not end_turn."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["I can't help with that."],
                    stop_reason="refusal"),
    ]
    assert _turn_complete(entries) is False


def test_turn_complete_latest_assistant_wins():
    """With multiple assistant entries, only the newest one's stop_reason counts.

    A prior end_turn followed by a mid-turn continuation means the turn is
    once again in progress.
    """
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Earlier done."], stop_reason="end_turn"),
        _make_entry("assistant", ["Now continuing..."], stop_reason=None),
    ]
    assert _turn_complete(entries) is False


def test_turn_complete_only_user_entries():
    """No assistant at all -> not complete. Prevents false idle on a fresh pane."""
    entries = [_make_user_entry(), _make_user_entry()]
    assert _turn_complete(entries) is False


def test_turn_complete_tool_use_then_end_turn():
    """A tool_use turn followed by a final end_turn entry should read as complete."""
    entries = [
        _make_user_entry(),
        _make_entry("assistant", ["Let me read."], tool_use_count=1,
                    stop_reason="tool_use"),
        _make_tool_result(),
        _make_entry("assistant", ["Answer after reading."],
                    stop_reason="end_turn"),
    ]
    assert _turn_complete(entries) is True
