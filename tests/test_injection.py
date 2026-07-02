"""Tests for the injection module — parsing and validation logic.

Note: Full injection tests require a live tmux session. These tests cover
the logic that can be tested without tmux.
"""

from unittest.mock import patch

from janitor.injection import (
    READY_PATTERNS,
    WORKING_PATTERNS,
    is_agent_idle,
    tmux_capture,
)


def test_ready_patterns_are_strings():
    for p in READY_PATTERNS:
        assert isinstance(p, str)
        assert len(p) > 0


def test_working_patterns_are_strings():
    for p in WORKING_PATTERNS:
        assert isinstance(p, str)
        assert len(p) > 0


def test_no_pattern_overlap():
    """Ready and working patterns should not overlap."""
    for rp in READY_PATTERNS:
        for wp in WORKING_PATTERNS:
            assert rp not in wp and wp not in rp


@patch("janitor.injection.tmux_capture")
def test_is_agent_idle_busy(mock_capture):
    mock_capture.return_value = "Thinking about the problem..."
    assert is_agent_idle("test:pane.0") is False


@patch("janitor.injection.tmux_capture")
def test_is_agent_idle_no_ready_pattern(mock_capture):
    mock_capture.return_value = "Some random output with no patterns"
    assert is_agent_idle("test:pane.0") is False


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_stable(mock_time, mock_capture):
    ready_content = "Some output\n\u2502 >\n? for shortcuts\n\u2500\u256f"
    mock_capture.return_value = ready_content
    mock_time.sleep = lambda x: None
    assert is_agent_idle("test:pane.0") is True


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_unstable(mock_time, mock_capture):
    """Content changes between captures - not idle."""
    mock_capture.side_effect = [
        "\u2502 >\n? for shortcuts\n\u2500\u256f",
        "\u2502 > Thinking...\nesc to interrupt",
    ]
    mock_time.sleep = lambda x: None
    assert is_agent_idle("test:pane.0") is False


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_mid_turn_overrides_idle(mock_time, mock_capture):
    """Bug 4: pane looks idle but callback says agent is between tool calls."""
    ready_content = "Some output\n\u2502 >\n? for shortcuts\n\u2500\u256f"
    mock_capture.return_value = ready_content
    mock_time.sleep = lambda x: None
    assert is_agent_idle("test:pane.0", is_mid_turn=lambda: True) is False


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_not_mid_turn_allows_idle(mock_time, mock_capture):
    """Callback returns False — idle detection proceeds normally."""
    ready_content = "Some output\n\u2502 >\n? for shortcuts\n\u2500\u256f"
    mock_capture.return_value = ready_content
    mock_time.sleep = lambda x: None
    assert is_agent_idle("test:pane.0", is_mid_turn=lambda: False) is True


def test_tmux_capture_handles_failure():
    """tmux_capture should return empty string when tmux isn't available."""
    result = tmux_capture("nonexistent:pane.99")
    assert isinstance(result, str)


# --- New patterns + stability window coverage ---


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_new_gerunds_flag_busy(mock_time, mock_capture):
    """Each newly added Claude Code gerund should read as 'busy' in the tail.

    Regression guard: if the status line shows any of these verbs near the
    bottom of the pane, is_agent_idle must refuse to inject even when the
    ready patterns also happen to be visible (e.g. when the prompt box is
    rendered above a still-working status line).
    """
    mock_time.sleep = lambda x: None
    new_verbs = [
        "Brewing", "Forging", "Ruminating", "Marinating", "Concocting",
        "Simmering", "Percolating", "Wrangling", "Whipping", "Musing",
        "Noodling", "Finagling", "Scheming",
    ]
    for verb in new_verbs:
        # Ready box AND a spinner-verb in the last 5 lines -> busy.
        content = (
            "some earlier output\n"
            "\u2502 >\n"
            "? for shortcuts\n"
            "\u2500\u256f\n"
            f"\u2731 {verb}\u2026 (4s \u00b7 esc to interrupt)"
        )
        mock_capture.return_value = content
        assert is_agent_idle("test:pane.0") is False, (
            f"verb {verb!r} should register as busy"
        )


@patch("janitor.injection.tmux_capture")
def test_is_agent_idle_stability_seconds_controls_sleep(mock_capture):
    """is_agent_idle must sleep for exactly the stability_seconds argument.

    The default is 1.0s; callers that expect long tool turns pass a larger
    value. We assert the actual sleep duration so the knob can't silently
    regress back to a hardcoded 1.0.
    """
    ready_content = "\u2502 >\n? for shortcuts\n\u2500\u256f"
    mock_capture.return_value = ready_content
    with patch("janitor.injection.time.sleep") as mock_sleep:
        assert is_agent_idle("test:pane.0", stability_seconds=3.5) is True
        mock_sleep.assert_called_once_with(3.5)


@patch("janitor.injection.tmux_capture")
def test_is_agent_idle_default_stability_is_one_second(mock_capture):
    """Callers that don't pass stability_seconds should still get 1.0s."""
    ready_content = "\u2502 >\n? for shortcuts\n\u2500\u256f"
    mock_capture.return_value = ready_content
    with patch("janitor.injection.time.sleep") as mock_sleep:
        assert is_agent_idle("test:pane.0") is True
        mock_sleep.assert_called_once_with(1.0)


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_busy_pattern_in_second_capture(mock_time, mock_capture):
    """If a working pattern lands between captures we must NOT declare idle.

    Covers the edge case where content1 == content2 (so the equality check
    passes) but a spinner frame has just materialized in the tail. Without
    the post-sleep tail re-check the function used to return True here,
    because the pre-sleep tail was clean and the overall content matched.
    Repro uses two different captures where the second's tail has a verb.
    """
    mock_time.sleep = lambda x: None
    ready_tail_clean = (
        "\u2502 >\n? for shortcuts\n\u2500\u256f\njust idling\n."
    )
    ready_tail_busy = (
        "\u2502 >\n? for shortcuts\n\u2500\u256f\n"
        "\u2731 Brewing\u2026 (1s \u00b7 esc to interrupt)"
    )
    mock_capture.side_effect = [ready_tail_clean, ready_tail_busy]
    # First capture is clean (no verb in tail), second has "Brewing" in tail.
    # The new post-sleep tail check should catch this and return False.
    assert is_agent_idle("test:pane.0") is False


@patch("janitor.injection.tmux_capture")
@patch("janitor.injection.time")
def test_is_agent_idle_esc_to_interrupt_alone_is_enough(mock_time, mock_capture):
    """`esc to interrupt` anywhere in the last 5 lines must block idle.

    The gerund-verb list is defense-in-depth; the interrupt hint is the
    canonical busy signal. This pins that alone-in-a-vacuum behavior so a
    future refactor doesn't accidentally require both hint and verb.
    """
    mock_time.sleep = lambda x: None
    content = (
        "\u2502 >\n? for shortcuts\n\u2500\u256f\n"
        "(3s \u00b7 100 tokens \u00b7 esc to interrupt)"
    )
    mock_capture.return_value = content
    assert is_agent_idle("test:pane.0") is False
