"""Janitor CLI wrapper — calls Claude Code in print mode to curate context.

This is the ONLY place the engine talks to an LLM. Everything else is
Python + filesystem + tmux.

The janitor is a short-lived subprocess, not a persistent session.
Each call is stateless and isolated.
"""

import json
import logging
import os
import subprocess
import time as _time
from pathlib import Path

from .types import JanitorResult

logger = logging.getLogger("janitor.cli")


def call_janitor(
    prompt: str,
    system_prompt: str,
    timeout: int = 120,
    effort: str = "high",
    cli_path: str = "claude",
    retries: int = 2,
    model: str = "opus",
) -> JanitorResult:
    """Call Claude Code CLI in print mode to curate context.

    Invokes: claude -p --model opus --effort high --tools ""
             --output-format json --no-session-persistence
             --system-prompt "..."

    Default effort is "high" and default model is "opus" -- the curator is
    the spine of the loop and a worse recap silently degrades every
    subsequent agent turn. Pinning the alias (rather than a versioned id)
    means the curator auto-tracks future Opus releases. Callers can override
    (e.g. tests) but the defaults reflect intent.

    NOT using --bare (breaks OAuth-based CLI auth).

    Args:
        prompt: The user prompt (preprocessed context + metadata)
        system_prompt: The rendered system prompt text (template vars already filled)
        timeout: Subprocess timeout in seconds
        effort: --effort flag value ("low", "medium", "high")
        cli_path: Path to the claude binary
        retries: Number of retry attempts on failure
        model: --model flag value (alias like "opus" or full id like
            "claude-opus-4-7")

    Returns:
        JanitorResult with success status, working set text, and metadata
    """
    cmd = [
        cli_path,
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--tools",
        "",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--system-prompt",
        system_prompt,
    ]

    # Environment: suppress color codes and terminal escape sequences
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "TERM": "dumb",
    }

    last_error = None

    for attempt in range(1 + retries):
        if attempt > 0:
            _time.sleep(2)  # Simple fixed delay between retries
            logger.info(f"Janitor retry {attempt}/{retries}")

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                env=env,
            )

            if result.returncode != 0:
                last_error = f"CLI exited {result.returncode}: {result.stderr[:500]}"
                logger.warning(f"Janitor attempt {attempt}: {last_error}")
                continue

            # Parse JSON output
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = f"Invalid JSON from CLI: {result.stdout[:500]}"
                logger.warning(f"Janitor attempt {attempt}: {last_error}")
                continue

            if data.get("is_error"):
                last_error = f"CLI error: {data.get('result', 'unknown')}"
                logger.warning(f"Janitor attempt {attempt}: {last_error}")
                continue

            # Success
            return JanitorResult(
                success=True,
                working_set=data.get("result", ""),
                raw_response=result.stdout,
                duration_ms=data.get("duration_ms", 0),
            )

        except subprocess.TimeoutExpired:
            last_error = f"Janitor timed out after {timeout}s"
            logger.warning(f"Janitor attempt {attempt}: {last_error}")

        except FileNotFoundError:
            last_error = f"'{cli_path}' command not found in PATH"
            logger.error(last_error)
            break  # No point retrying if binary doesn't exist

    return JanitorResult(success=False, error=last_error)
