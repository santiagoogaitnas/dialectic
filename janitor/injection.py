"""Injection gate and tmux delivery.

Handles two concerns:
1. Detecting when a Claude Code agent is idle (safe to inject)
2. Delivering context updates via tmux paste-buffer

Uses the double-capture stability check: capture pane content, wait 1s,
capture again, confirm nothing changed. This prevents injecting while
the agent is between tool calls or mid-output.

tmux injection uses load-buffer from a temp file (not set-buffer inline)
for binary-safe delivery of large payloads.
"""

import logging
import subprocess
import time

logger = logging.getLogger("janitor.injection")

# Patterns indicating Claude Code is ready for input.
# Verified against Claude Code's terminal UI.
READY_PATTERNS = [
    "│ >",  # Prompt box
    "? for shortcuts",  # Bottom hint
    "─╯",  # Prompt box bottom border
]

# Patterns indicating Claude Code is actively working.
# Claude Code renders Unicode prefixes (✻, ●) but substring matching works.
#
# "esc to interrupt" is the load-bearing signal — it sits in the status
# line for the full duration of any tool call, thinking turn, or long
# output. The gerunds below are Claude Code's rotating status verbs;
# they're redundant with "esc to interrupt" in most cases but protect
# against terminal width wrapping or paging that splits the status line
# so the verb is visible but the interrupt hint is not.
#
# Gerund selection rule: include only verbs unlikely to appear at the
# start of a normal English sentence in agent dialogue. "Pondering",
# "Crafting", "Cooking" are all real status verbs but also natural
# sentence-starters; if a long reply happens to end with such a word
# just above the prompt box we'd flag the pane as busy forever. The
# safer list below favors whimsical, rarely-used verbs.
WORKING_PATTERNS = [
    "esc to interrupt",
    "Bash(",
    "Running",
    "Thinking",
    "Pontificating",
    "Brewing",
    "Forging",
    "Ruminating",
    "Marinating",
    "Concocting",
    "Simmering",
    "Percolating",
    "Wrangling",
    "Whipping",
    "Musing",
    "Noodling",
    "Finagling",
    "Scheming",
]


def tmux_capture(pane_target: str) -> str:
    """Capture visible content of a tmux pane.

    Args:
        pane_target: tmux pane identifier (e.g., "chain-04161119-9733:0.0")

    Returns:
        Captured pane text, or empty string on failure.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_target, "-p"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return ""


def is_agent_idle(
    pane_target: str,
    is_mid_turn=None,
    stability_seconds: float = 1.0,
) -> bool:
    """Double-capture stability check.

    1. Capture pane, check for working patterns (→ busy)
    2. Check for ready patterns (→ might be idle)
    3. Wait `stability_seconds`
    4. Capture again, confirm still idle and content unchanged

    `stability_seconds` controls how long the pane must remain unchanged
    before we believe the agent is really idle. The default (1.0s) is
    right for a fast, visible status line. Callers that know the agent
    is mid-tool-call or mid-long-build can pass a longer window (e.g.
    3.0–5.0s) to avoid a false "idle" during the millisecond gap between
    Claude Code dispatching one tool result and starting the next
    spinner frame. Premature injection there corrupts the JSONL turn.

    Returns True only if the agent is confirmed idle on both captures.
    """
    content1 = tmux_capture(pane_target)

    # Check only the last few lines for working patterns -- historical
    # tool output (e.g. "⏺ Bash(swift build)") matches otherwise.
    tail = "\n".join(content1.rstrip().split("\n")[-5:])
    for pattern in WORKING_PATTERNS:
        if pattern in tail:
            return False

    # Must see at least one ready pattern
    if not any(p in content1 for p in READY_PATTERNS):
        return False

    # Stability check — wait and re-capture
    time.sleep(stability_seconds)
    content2 = tmux_capture(pane_target)

    # Content changed → agent started working between captures
    if content2 != content1:
        return False

    # Confirm still shows ready pattern
    if not any(p in content2 for p in READY_PATTERNS):
        return False

    # Re-check working patterns against the second capture — a new spinner
    # frame or the head of a fresh tool call may have landed in the tail
    # during the sleep even when the full content hash otherwise matches.
    tail2 = "\n".join(content2.rstrip().split("\n")[-5:])
    for pattern in WORKING_PATTERNS:
        if pattern in tail2:
            return False

    # Caller-provided override: e.g. JSONL says agent is mid-turn between tool calls
    if is_mid_turn and is_mid_turn():
        return False

    return True
