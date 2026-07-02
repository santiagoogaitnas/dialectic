"""Tests for the janitor CLI wrapper — parsing logic + mocked call_janitor."""

import json
import subprocess
from unittest.mock import patch

from janitor.janitor_cli import call_janitor


# --- call_janitor: mocked subprocess tests -------------------------------


def _mock_run(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess-like return for subprocess.run mocks."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _ok_json(result_text="WORKING SET:\nstuff", duration_ms=42):
    return json.dumps({
        "result": result_text,
        "duration_ms": duration_ms,
        "is_error": False,
    })


def test_call_janitor_success_parses_json_result():
    with patch("janitor.janitor_cli.subprocess.run") as run:
        run.return_value = _mock_run(stdout=_ok_json("RECAP: hi", 123))
        out = call_janitor(prompt="p", system_prompt="sp", retries=0)

    assert out.success is True
    assert out.working_set == "RECAP: hi"
    assert out.duration_ms == 123
    assert out.error is None


def test_call_janitor_constructs_expected_argv():
    with patch("janitor.janitor_cli.subprocess.run") as run:
        run.return_value = _mock_run(stdout=_ok_json())
        call_janitor(
            prompt="user prompt", system_prompt="SYS",
            effort="high", cli_path="/opt/bin/claude", retries=0,
        )

    argv = run.call_args.args[0]
    assert argv[0] == "/opt/bin/claude"
    assert "-p" in argv
    # --effort takes the value we passed
    assert argv[argv.index("--effort") + 1] == "high"
    # --tools is invoked with empty string (no tools for janitor)
    assert argv[argv.index("--tools") + 1] == ""
    # --model defaults to "opus" so the curator never silently runs on a
    # smaller default. Spec §2 (Curator quality / Best model).
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--no-session-persistence" in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    # System prompt body is passed via flag, not stdin
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    # User prompt is piped in via stdin, not argv
    assert "user prompt" not in argv
    assert run.call_args.kwargs.get("input") == "user prompt"


def test_call_janitor_passes_low_effort_when_requested():
    with patch("janitor.janitor_cli.subprocess.run") as run:
        run.return_value = _mock_run(stdout=_ok_json())
        call_janitor(prompt="p", system_prompt="s", effort="low", retries=0)

    argv = run.call_args.args[0]
    assert argv[argv.index("--effort") + 1] == "low"


def test_call_janitor_passes_model_override():
    """Callers can pin a specific model id (e.g. tests, future-Opus rollout)."""
    with patch("janitor.janitor_cli.subprocess.run") as run:
        run.return_value = _mock_run(stdout=_ok_json())
        call_janitor(
            prompt="p", system_prompt="s",
            model="claude-opus-4-7", retries=0,
        )

    argv = run.call_args.args[0]
    assert argv[argv.index("--model") + 1] == "claude-opus-4-7"


def test_call_janitor_default_model_is_opus():
    """Default model must be 'opus' so the curator runs on the best model."""
    with patch("janitor.janitor_cli.subprocess.run") as run:
        run.return_value = _mock_run(stdout=_ok_json())
        call_janitor(prompt="p", system_prompt="s", retries=0)

    argv = run.call_args.args[0]
    # --model appears exactly once and its value is "opus"
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "opus"


def test_call_janitor_strips_terminal_color_in_env():
    with patch("janitor.janitor_cli.subprocess.run") as run:
        run.return_value = _mock_run(stdout=_ok_json())
        call_janitor(prompt="p", system_prompt="s", retries=0)

    env = run.call_args.kwargs["env"]
    assert env.get("NO_COLOR") == "1"
    assert env.get("TERM") == "dumb"


def test_call_janitor_retries_on_nonzero_exit_then_succeeds():
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep"):
        run.side_effect = [
            _mock_run(returncode=1, stderr="boom"),
            _mock_run(stdout=_ok_json("good")),
        ]
        out = call_janitor(prompt="p", system_prompt="s", retries=2)

    assert out.success is True
    assert out.working_set == "good"
    assert run.call_count == 2


def test_call_janitor_retries_on_invalid_json_then_succeeds():
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep"):
        run.side_effect = [
            _mock_run(stdout="not-json{{"),
            _mock_run(stdout=_ok_json("recovered")),
        ]
        out = call_janitor(prompt="p", system_prompt="s", retries=2)

    assert out.success is True
    assert out.working_set == "recovered"
    assert run.call_count == 2


def test_call_janitor_retries_on_is_error_payload_then_succeeds():
    error_payload = json.dumps({"is_error": True, "result": "rate-limited"})
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep"):
        run.side_effect = [
            _mock_run(stdout=error_payload),
            _mock_run(stdout=_ok_json("ok now")),
        ]
        out = call_janitor(prompt="p", system_prompt="s", retries=2)

    assert out.success is True
    assert out.working_set == "ok now"


def test_call_janitor_returns_failure_when_retries_exhausted():
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep"):
        run.return_value = _mock_run(returncode=2, stderr="still broken")
        out = call_janitor(prompt="p", system_prompt="s", retries=1)

    assert out.success is False
    assert out.error is not None
    assert "still broken" in out.error
    # retries=1 → 2 total attempts (1 initial + 1 retry)
    assert run.call_count == 2


def test_call_janitor_retries_on_timeout():
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep"):
        run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=5),
            _mock_run(stdout=_ok_json("after timeout")),
        ]
        out = call_janitor(prompt="p", system_prompt="s", timeout=5, retries=2)

    assert out.success is True
    assert out.working_set == "after timeout"


def test_call_janitor_does_not_retry_when_binary_missing():
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep") as sleep:
        run.side_effect = FileNotFoundError("no such file")
        out = call_janitor(
            prompt="p", system_prompt="s",
            cli_path="/no/such/claude", retries=5,
        )

    assert out.success is False
    assert out.error is not None
    assert "/no/such/claude" in out.error
    # FileNotFoundError must short-circuit — only one attempt, no sleep
    assert run.call_count == 1
    sleep.assert_not_called()


def test_call_janitor_truncates_long_stderr_in_error():
    long_stderr = "x" * 2000
    with patch("janitor.janitor_cli.subprocess.run") as run, \
         patch("janitor.janitor_cli._time.sleep"):
        run.return_value = _mock_run(returncode=1, stderr=long_stderr)
        out = call_janitor(prompt="p", system_prompt="s", retries=0)

    assert out.success is False
    # error message keeps stderr bounded so it never blows up downstream logs
    assert len(out.error) < 1000
