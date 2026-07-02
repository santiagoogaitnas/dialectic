"""Tests for the recap flow — extract_full_dialogue, get_recap, and context-refresh filtering.

These are the tests for Bug 4 (zero test coverage on the core innovation).
They lock in the fixes for:
  - Bug 1: <context-refresh> tag must exclude recap injections from re-extraction
  - Bug 2: 12000-char truncation limit (not 3000)
  - Bug 3: Agent B asymmetry — whole message excluded when it contains <context-refresh>
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from chain import (
    extract_full_dialogue, get_recap, _strip_framing_tags,
    _parse_bulletin, update_bulletin, BULLETIN_SYSTEM_PROMPT,
)
from janitor.types import JanitorResult


def _write_jsonl(path: Path, entries: list[dict]):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _user_entry(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _assistant_entry(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    }


# --- extract_full_dialogue: basic extraction ---


def test_extract_basic_dialogue(tmp_path):
    """Extracts user and assistant messages into labeled strings."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("What is tmux?"),
        _assistant_entry("tmux is a terminal multiplexer."),
    ])
    result = extract_full_dialogue(jf)
    assert "USER: What is tmux?" in result
    assert "ASSISTANT: tmux is a terminal multiplexer." in result


def test_extract_empty_file(tmp_path):
    """Empty JSONL returns empty string."""
    jf = tmp_path / "test.jsonl"
    jf.write_text("")
    assert extract_full_dialogue(jf) == ""


def test_extract_skips_empty_content(tmp_path):
    """Messages with empty or whitespace-only content are skipped."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("   "),
        _user_entry(""),
        _assistant_entry("Real response."),
    ])
    result = extract_full_dialogue(jf)
    assert "USER:" not in result
    assert "ASSISTANT: Real response." in result


def test_extract_non_string_user_content(tmp_path):
    """User messages with list content (tool_result) are skipped."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
        ]}},
        _assistant_entry("Response."),
    ])
    result = extract_full_dialogue(jf)
    lines = [l for l in result.split("\n\n") if l.strip()]
    assert len(lines) == 2  # USER + ASSISTANT, not 3


# --- extract_full_dialogue: <context-refresh> filter (Bug 1 fix) ---


def test_context_refresh_filter_excludes_tagged_message(tmp_path):
    """Messages containing <context-refresh> tags are excluded entirely."""
    jf = tmp_path / "test.jsonl"
    recap_content = "<context-refresh>\nYou are resuming...\nRECAP content here\n</context-refresh>\n\nContinue."
    _write_jsonl(jf, [
        _user_entry(recap_content),
        _assistant_entry("I'll continue from the recap."),
        _user_entry("Normal follow-up question."),
        _assistant_entry("Normal response."),
    ])
    result = extract_full_dialogue(jf)
    assert "<context-refresh>" not in result
    assert "resuming" not in result
    assert "ASSISTANT: I'll continue from the recap." in result
    assert "USER: Normal follow-up question." in result


def test_context_refresh_filter_agent_b_excludes_a_output(tmp_path):
    """Agent B's recap injection includes a_output outside the tags.

    The substring check at line 333 excludes the ENTIRE message if it
    contains <context-refresh> anywhere. This means a_output (which is
    outside the tags) is also excluded. This is the known trade-off
    from Bug 3 — test it as expected behavior.
    """
    jf = tmp_path / "test.jsonl"
    agent_b_recap = (
        "<context-refresh>\nRecap for Thinker...\n</context-refresh>\n\n"
        "Your counterpart just said:\n\n"
        "Agent A's post-reset response with important analysis."
    )
    _write_jsonl(jf, [
        _user_entry(agent_b_recap),
        _assistant_entry("Responding to A's points."),
    ])
    result = extract_full_dialogue(jf)
    # The entire recap message is excluded, including a_output
    assert "Agent A's post-reset response" not in result
    # But the assistant's reply IS visible
    assert "ASSISTANT: Responding to A's points." in result


def test_context_refresh_only_affects_user_messages(tmp_path):
    """Assistant messages are never checked for <context-refresh>."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Normal question."),
        _assistant_entry("I see the <context-refresh> tag in the code."),
    ])
    result = extract_full_dialogue(jf)
    # Assistant message should NOT be filtered even though it mentions the tag
    assert "ASSISTANT:" in result
    assert "<context-refresh>" in result


# --- extract_full_dialogue: truncation limit (Bug 2 fix) ---


def test_truncation_at_12000_not_3000(tmp_path):
    """Content up to 12000 chars is preserved (regression test for Bug 2)."""
    jf = tmp_path / "test.jsonl"
    long_content = "A" * 8000  # Would be truncated at 3000, preserved at 12000
    _write_jsonl(jf, [
        _user_entry(long_content),
    ])
    result = extract_full_dialogue(jf)
    # Extract the content after "USER: "
    user_text = result.replace("USER: ", "")
    assert len(user_text) == 8000


def test_truncation_clips_at_12000(tmp_path):
    """Content beyond 12000 chars IS truncated."""
    jf = tmp_path / "test.jsonl"
    long_content = "B" * 15000
    _write_jsonl(jf, [
        _user_entry(long_content),
    ])
    result = extract_full_dialogue(jf)
    user_text = result.replace("USER: ", "")
    assert len(user_text) == 12000


def test_assistant_truncation_at_12000(tmp_path):
    """Assistant messages are also truncated at 12000."""
    jf = tmp_path / "test.jsonl"
    long_response = "C" * 15000
    _write_jsonl(jf, [
        _user_entry("Question."),
        _assistant_entry(long_response),
    ])
    result = extract_full_dialogue(jf)
    assert f"ASSISTANT: {'C' * 12000}" in result
    assert "C" * 12001 not in result


# --- Framing tag stripping ---


def test_strip_framing_tags_removes_role():
    text = "<role>\nYou are the builder.\n</role>\n\nHere is what I think."
    assert _strip_framing_tags(text) == "Here is what I think."


def test_strip_framing_tags_removes_all_three():
    text = (
        "<role>\nBuilder prompt\n</role>\n\n"
        "<project>\nMaintain plan.md\n</project>\n\n"
        "<format>\nRelay format\n</format>\n\n"
        "Actual content here."
    )
    assert _strip_framing_tags(text) == "Actual content here."


def test_strip_framing_tags_preserves_plain_text():
    text = "No tags here. Just content."
    assert _strip_framing_tags(text) == text


def test_extract_full_dialogue_strips_role_from_user_messages(tmp_path):
    """User messages with <role> tags should have them stripped before extraction."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("<role>\nYou are the builder.\n</role>\n\nWhat should we build?"),
        _assistant_entry("Let's build a bridge."),
    ])
    result = extract_full_dialogue(jf)
    assert "<role>" not in result
    assert "What should we build?" in result
    assert "Let's build a bridge." in result


# --- get_recap: curator flow ---


def test_get_recap_extracts_recap_section(tmp_path):
    """get_recap parses the RECAP: prefix from janitor output."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        _assistant_entry("World"),
    ])

    recap_body = (
        "The agents discussed greetings and explored the hello/world pattern "
        "as a metaphor for initial contact between systems. Key thread: the "
        "builder proposed a minimal handshake protocol while the thinker drew "
        "parallels to linguistic greeting rituals across cultures. The current "
        "position is that a two-phase approach works best."
    )
    mock_result = JanitorResult(
        success=True,
        working_set=f"RECAP:\n{recap_body}",
    )

    with patch("chain.call_janitor", return_value=mock_result):
        recap = get_recap(jf, "Builder (pragmatic)")

    assert recap == recap_body


def test_get_recap_rejects_missing_recap_header(tmp_path):
    """If janitor never includes RECAP: prefix, all retries fail."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        _assistant_entry("World"),
    ])

    mock_result = JanitorResult(
        success=True,
        working_set="The agents discussed greetings. No formal RECAP header.",
    )

    with patch("chain.call_janitor", return_value=mock_result):
        recap = get_recap(jf, "Thinker (patterns)")

    assert recap is None, "Missing RECAP: header should fail validation"


def test_get_recap_accepts_short_recap_with_header(tmp_path):
    """Short recaps with the RECAP: header are valid -- density beats length."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        _assistant_entry("World"),
    ])

    mock_result = JanitorResult(
        success=True,
        working_set="RECAP:\nOK, got it.",
    )

    with patch("chain.call_janitor", return_value=mock_result):
        recap = get_recap(jf, "Builder (pragmatic)")

    assert recap == "OK, got it."


def test_get_recap_retries_on_invalid_then_succeeds(tmp_path):
    """First attempt missing the RECAP: header, second attempt valid."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        _assistant_entry("World"),
    ])

    valid_body = (
        "The agents discussed greetings and explored the hello/world pattern "
        "as a metaphor for initial contact between systems."
    )
    # No RECAP: prefix -- header check should reject this attempt.
    bad_result = JanitorResult(
        success=True,
        working_set="The agents discussed greetings. No formal header.",
    )
    good_result = JanitorResult(success=True, working_set=f"RECAP:\n{valid_body}")

    with patch("chain.call_janitor", side_effect=[bad_result, good_result]):
        recap = get_recap(jf, "Builder (pragmatic)")

    assert recap == valid_body


def test_get_recap_returns_none_on_empty_dialogue(tmp_path):
    """get_recap returns None when JSONL has no extractable dialogue."""
    jf = tmp_path / "test.jsonl"
    jf.write_text("")

    recap = get_recap(jf, "Builder (pragmatic)")
    assert recap is None


def test_get_recap_returns_none_on_janitor_failure(tmp_path):
    """get_recap returns None when the janitor subprocess fails."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        _assistant_entry("World"),
    ])

    mock_result = JanitorResult(success=False, error="CLI timed out")

    with patch("chain.call_janitor", return_value=mock_result):
        recap = get_recap(jf, "Builder (pragmatic)")

    assert recap is None


def test_get_recap_passes_role_label_in_prompt(tmp_path):
    """The role label is included in the prompt sent to the janitor."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Hello"),
        _assistant_entry("World"),
    ])

    mock_result = JanitorResult(success=True, working_set="RECAP:\nTest recap.")

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        get_recap(jf, "Builder (pragmatic)")

    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "Builder (pragmatic)" in prompt_arg


# --- Integration: recap injection -> extraction round-trip ---


def test_recap_not_recompressed_across_reset(tmp_path):
    """Simulates the full round-trip: recap injection -> new dialogue -> extraction.

    After a reset, the JSONL contains:
    1. The recap injection (tagged with <context-refresh>)
    2. Agent's response to the recap
    3. New dialogue rounds

    When extract_full_dialogue runs at the NEXT reset, it should
    skip (1) and only see (2) and (3). This prevents cascading compression.
    """
    jf = tmp_path / "post_reset.jsonl"

    # Simulate what the JSONL looks like after a reset + 2 rounds
    recap_injection = (
        "<context-refresh>\n"
        "You are resuming a dialogue. Here is the full context:\n\n"
        "## RECAP\n"
        "The agents analyzed 7 projects. Key finding: tmux 500-char limit.\n"
        "Builder position: ship fixes first.\n"
        "Thinker position: measure before shipping.\n"
        "</context-refresh>\n\n"
        "Continue from here. What is your next thought?"
    )

    _write_jsonl(jf, [
        _user_entry(recap_injection),  # Recap injection — should be filtered
        _assistant_entry("Continuing from the recap. I agree we should ship fixes."),
        _user_entry("What about the cumulative curator idea?"),
        _assistant_entry("The cumulative curator would take ~30 min to implement."),
        _user_entry("Let's do tests first."),
        _assistant_entry("Agreed. Writing tests for extract_full_dialogue."),
    ])

    result = extract_full_dialogue(jf)

    # The recap injection is excluded
    assert "tmux 500-char limit" not in result
    assert "Builder position" not in result

    # But the new dialogue IS included
    assert "Continuing from the recap" in result
    assert "cumulative curator" in result
    assert "Writing tests" in result

    # Count: 1 assistant (post-recap) + 2 user + 2 assistant = 5 entries
    user_count = result.count("USER:")
    assistant_count = result.count("ASSISTANT:")
    assert user_count == 2, f"Expected 2 USER entries, got {user_count}"
    assert assistant_count == 3, f"Expected 3 ASSISTANT entries, got {assistant_count}"


# --- get_recap: cumulative curator (Fix 3) ---


def test_get_recap_cumulative_includes_previous_recap(tmp_path):
    """When previous_recap is provided, the prompt includes it."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("New dialogue after reset."),
        _assistant_entry("Continuing the work."),
    ])

    mock_result = JanitorResult(
        success=True,
        working_set="RECAP:\nUpdated recap with old and new content.",
    )

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        recap = get_recap(jf, "Builder (pragmatic)", previous_recap="Previous state was X.")

    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "PREVIOUS RECAP (from last reset):" in prompt_arg
    assert "Previous state was X." in prompt_arg
    assert "NEW DIALOGUE (since last reset):" in prompt_arg


def test_get_recap_first_reset_has_no_previous(tmp_path):
    """First reset (no previous_recap) uses original FULL DIALOGUE format."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("First ever dialogue."),
        _assistant_entry("First response."),
    ])

    mock_result = JanitorResult(
        success=True,
        working_set="RECAP:\nFirst recap ever.",
    )

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        recap = get_recap(jf, "Builder (pragmatic)", previous_recap=None)

    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "FULL DIALOGUE:" in prompt_arg
    assert "PREVIOUS RECAP" not in prompt_arg


def test_get_recap_includes_bulletin_when_provided(tmp_path):
    """When bulletin is passed, it appears in the recap curator's prompt."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Dialogue text."),
        _assistant_entry("Response text."),
    ])

    recap_body = (
        "The agents continued their discussion with awareness of long-term "
        "patterns. The builder noted this was the third time the automation "
        "thread surfaced. The thinker acknowledged the recurrence and proposed "
        "a different framing to break the cycle."
    )
    mock_result = JanitorResult(success=True, working_set=f"RECAP:\n{recap_body}")

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        recap = get_recap(
            jf, "Builder (pragmatic)",
            bulletin="Thread X: 3rd recurrence. Agents keep reopening it.",
        )

    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "CURATOR OBSERVATIONS" in prompt_arg
    assert "3rd recurrence" in prompt_arg
    assert recap == recap_body


def test_get_recap_omits_bulletin_when_none(tmp_path):
    """When no bulletin exists, the prompt doesn't mention curator observations."""
    jf = tmp_path / "test.jsonl"
    _write_jsonl(jf, [
        _user_entry("Dialogue text."),
        _assistant_entry("Response text."),
    ])

    recap_body = (
        "Standard recap without bulletin influence. The agents discussed "
        "the topic at hand without any cross-reset pattern awareness. "
        "The builder proposed action items and the thinker identified "
        "underlying structural parallels."
    )
    mock_result = JanitorResult(success=True, working_set=f"RECAP:\n{recap_body}")

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        recap = get_recap(jf, "Builder (pragmatic)", bulletin=None)

    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "CURATOR OBSERVATIONS" not in prompt_arg


# --- Bulletin: persistent curator memory ---


def test_parse_bulletin_extracts_section():
    raw = "BULLETIN:\nThread X resurfaced 3 times. Builder leads on concrete decisions."
    assert _parse_bulletin(raw) == "Thread X resurfaced 3 times. Builder leads on concrete decisions."


def test_parse_bulletin_empty_on_missing_header():
    raw = "Just some text without a bulletin header."
    assert _parse_bulletin(raw) == ""


def test_parse_bulletin_multiline():
    raw = (
        "BULLETIN:\n"
        "Thread X: 3rd recurrence. Agents keep reopening it.\n"
        "Thread Y: resolved in round 4, hasn't come back.\n"
        "Pattern: builder defers on abstract questions, thinker defers on scope."
    )
    result = _parse_bulletin(raw)
    assert "3rd recurrence" in result
    assert "builder defers" in result


def test_update_bulletin_first_reset():
    """First bulletin (no previous). Should produce observations."""
    bulletin_text = (
        "BULLETIN:\n"
        "First observation: builder is focused on the parser rewrite. "
        "Thinker is drawing parallels to factory automation. "
        "Key thread: recurring disagreement about caching. "
        "Both agree multiplier is about leverage, not hours."
    )
    mock_result = JanitorResult(success=True, working_set=bulletin_text)

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        result = update_bulletin("dialogue A text", "dialogue B text", None)

    assert "recurring disagreement about caching" in result
    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "BUILDER DIALOGUE" in prompt_arg
    assert "THINKER DIALOGUE" in prompt_arg
    assert "YOUR PREVIOUS BULLETIN" not in prompt_arg


def test_update_bulletin_with_previous():
    """Second+ bulletin includes previous observations."""
    bulletin_text = (
        "BULLETIN:\n"
        "Thread X: 2nd recurrence (was 1st in previous). "
        "New thread: agents disagreeing on scope of automation. "
        "Pattern: thinker introduces abstractions, builder narrows them."
    )
    mock_result = JanitorResult(success=True, working_set=bulletin_text)

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        result = update_bulletin(
            "dialogue A", "dialogue B",
            previous_bulletin="Previous: thread X first appeared.",
        )

    assert "2nd recurrence" in result
    prompt_arg = mock_call.call_args.kwargs.get("prompt") or mock_call.call_args[0][0]
    assert "YOUR PREVIOUS BULLETIN" in prompt_arg
    assert "thread X first appeared" in prompt_arg


def test_update_bulletin_keeps_previous_on_failure():
    """If curator fails, return previous bulletin unchanged."""
    mock_result = JanitorResult(success=False, error="timeout")

    with patch("chain.call_janitor", return_value=mock_result):
        result = update_bulletin("dialogue A", "dialogue B", "Previous bulletin content.")

    assert result == "Previous bulletin content."


def test_update_bulletin_keeps_previous_on_empty_response():
    """If curator returns no parseable bulletin body, keep the previous one."""
    # No BULLETIN: header at all -> _parse_bulletin returns "" -> previous wins.
    mock_result = JanitorResult(success=True, working_set="No bulletin produced.")

    with patch("chain.call_janitor", return_value=mock_result):
        result = update_bulletin("dialogue A", "dialogue B", "Previous bulletin content.")

    assert result == "Previous bulletin content."


def test_update_bulletin_accepts_short_bulletin():
    """A terse bulletin with the header is accepted -- no character minimum."""
    mock_result = JanitorResult(success=True, working_set="BULLETIN:\nOK.")

    with patch("chain.call_janitor", return_value=mock_result):
        result = update_bulletin("dialogue A", "dialogue B", "Previous bulletin content.")

    assert result == "OK."


def test_update_bulletin_uses_bulletin_system_prompt():
    """Bulletin calls use BULLETIN_SYSTEM_PROMPT, not JANITOR_SYSTEM_PROMPT."""
    bulletin_text = (
        "BULLETIN:\n"
        "Observation about the dialogue that is long enough to pass validation "
        "and contains meaningful structural analysis of the conversation patterns."
    )
    mock_result = JanitorResult(success=True, working_set=bulletin_text)

    with patch("chain.call_janitor", return_value=mock_result) as mock_call:
        update_bulletin("dialogue A", "dialogue B")

    sys_prompt = mock_call.call_args.kwargs.get("system_prompt")
    assert "long-term memory" in sys_prompt
    assert "structural observation" in sys_prompt
