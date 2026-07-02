"""Pre-build smoke tests.

Run these BEFORE writing engine code. Each validates a critical assumption
about the Claude CLI, tmux, and JSONL format.

Usage: python -m pytest tests/test_smoke.py -v

NOTE: These tests require a working, authenticated `claude` CLI. They make
real CLI calls and will be skipped if claude is not available.
"""

import json
import subprocess
from pathlib import Path

import pytest


def _claude_available() -> bool:
    try:
        subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


requires_claude = pytest.mark.skipif(
    not _claude_available(), reason="claude CLI not available"
)


@requires_claude
def test_cli_print_mode():
    """Verify claude -p returns valid JSON with --output-format json."""
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--effort",
            "low",
            "--tools",
            "",
            "--output-format",
            "json",
            "--no-session-persistence",
        ],
        input="Say the word 'pong' and nothing else.",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"CLI failed: {result.stderr[:200]}"

    data = json.loads(result.stdout)
    assert not data["is_error"], f"CLI error: {data.get('result')}"
    assert "result" in data
    assert len(data["result"]) > 0
    print(f"  CLI response: {data['result'][:100]}")
    print(f"  Duration: {data.get('duration_ms', '?')}ms")


@requires_claude
def test_system_prompt_override():
    """Verify --system-prompt controls the janitor's behavior."""
    system_prompt = (
        "You are a janitor. Always respond with exactly: "
        "CURRENT TASK: testing\nWORKING SET: verified"
    )
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--effort",
            "low",
            "--tools",
            "",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
        ],
        input="What is your role?",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert not data["is_error"]
    response = data["result"].upper()
    assert "CURRENT TASK" in response or "WORKING SET" in response, (
        f"System prompt not respected: {data['result'][:200]}"
    )
    print(f"  Response: {data['result'][:200]}")


@requires_claude
def test_janitor_extraction():
    """Verify the janitor can extract a WORKING SET from conversation input."""
    system_prompt = (
        "You are a context curator. Read the agent's conversation and produce "
        "a concise context update. Respond with clearly labeled sections: "
        "CURRENT TASK, RECENT DECISIONS, WORKING SET."
    )
    conversation = """AGENT TASK: Implement user authentication with JWT

AGENT 0 RECENT CONVERSATION:
USER: Implement JWT auth for the API
ASSISTANT: I'll start by reading the existing auth setup...
TOOL_USE: Read(src/auth/middleware.ts)
TOOL_RESULT: [4200 chars, truncated] export function authMiddleware(req, res, next) { ... }
ASSISTANT: I see there's existing session-based auth. I'll add JWT alongside it.
TOOL_USE: Bash(npm install jsonwebtoken)
TOOL_RESULT: added 1 package
TOOL_USE: Edit(src/auth/middleware.ts)
ASSISTANT: Added JWT verification. Now I need refresh token rotation.

FILES MODIFIED: src/auth/middleware.ts
FILES READ: src/auth/middleware.ts, package.json"""

    result = subprocess.run(
        [
            "claude",
            "-p",
            "--effort",
            "low",
            "--tools",
            "",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
        ],
        input=conversation,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert not data["is_error"]

    response = data["result"]
    assert "WORKING SET" in response.upper(), (
        f"No WORKING SET section in response: {response[:300]}"
    )
    print(f"  Extraction result:\n{response[:500]}")


def test_jsonl_discovery():
    """Verify we can find JSONL files in the Claude projects directory."""
    claude_dir = Path.home() / ".claude" / "projects"
    assert claude_dir.exists(), f"Claude projects dir not found: {claude_dir}"

    jsonl_files = list(claude_dir.rglob("*.jsonl"))
    assert len(jsonl_files) > 0, "No JSONL files found"

    most_recent = max(jsonl_files, key=lambda f: f.stat().st_mtime)
    with open(most_recent) as f:
        first_line = f.readline()
    entry = json.loads(first_line)
    assert "type" in entry or "message" in entry, (
        f"JSONL entry missing expected fields: {list(entry.keys())}"
    )
    print(f"  Found {len(jsonl_files)} JSONL files")
    print(f"  Most recent: {most_recent.name}")
    print(f"  First entry type: {entry.get('type', 'unknown')}")


@requires_claude
def test_tools_disabled():
    """Verify --tools '' prevents the janitor from using tools."""
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--effort",
            "low",
            "--tools",
            "",
            "--output-format",
            "json",
            "--no-session-persistence",
        ],
        input="Read the file /etc/hostname and tell me what it says.",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert not data["is_error"]
    # With --tools "", the model cannot execute tools. It may mention tools
    # in text or output XML tool stubs, but it won't produce actual tool
    # results with file contents. The key check: num_turns should be 1
    # (no tool round-trips happened).
    assert data.get("num_turns", 1) == 1, (
        f"Expected 1 turn (no tool execution), got {data.get('num_turns')}"
    )
    print(f"  Response: {data['result'][:200]}")
