"""Tests for the loop-sacredness invariant in chain.run_chain.

The relay loop is the entire product. Once run_chain() enters its
``while True``, the only exits allowed are (a) ``KeyboardInterrupt`` from
the operator and (b) a tmux session that no longer exists. Nothing about
agent content — empty output, identical outputs, curator-recap failure —
may terminate the loop. Those are stalls, to be recovered from.

An earlier revision had four ``break`` statements in the main loop that
each exited the infinite relay on agent content. This file exists so those
never come back: each test patches run_chain's loop dependencies to
reproduce the specific stall condition, lets the loop run a bounded number
of iterations, and asserts the loop kept going (recovered) rather than
exiting.

Strategy
--------

run_chain is tmux-heavy but its loop body is pure-Python-glue. We stub
the boot path (setup_workspace, setup_tmux, start_agent, wait_for_idle,
inject_message, wait_and_extract, discover_jsonl_with_retries, time.sleep)
so run_chain reaches the ``while True`` without touching tmux, then drive
``do_round`` / ``clear_and_recap`` with side_effects that produce the
pathological values and raise a sentinel exception after a few
iterations — if the loop had broken on content, the iteration count would
be below the sentinel trigger and the sentinel would never fire.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import chain
import registry as reg
from registry import ChainConfig


# --- Harness ------------------------------------------------------------------


class _LoopStop(Exception):
    """Sentinel raised from a patched callable to abort the loop under test.

    Distinct from KeyboardInterrupt so the test can tell at the end that
    the loop ran long enough for the patched callable to hit its
    iteration budget (== loop recovered after the earlier stall).
    """


class _FakeCoordCtx:
    """Stand-in for ChainCoordinatorContext used in tests.

    The real context opens / fcntl.flock()s coordination.json via
    project_coordinator, which blows up under the blanket
    ``patch('builtins.open')`` the behavioural tests use. Substituting a
    pure-Python stub keeps run_chain's ``with`` block intact without
    booting project_coordinator's on-disk state.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_cfg(tmp_path: Path, chain_id: str = "sacredness", project: Path | None = None):
    return ChainConfig(
        chain_id=chain_id,
        session=f"sess-{chain_id}",
        seed="seed text",
        project=str(project) if project else None,
    )


def _distinct_cursors(cfg: ChainConfig):
    pane_a_dir, pane_b_dir = cfg.pane_dirs()
    return [
        MagicMock(file_path=Path(pane_a_dir) / "a.jsonl"),
        MagicMock(file_path=Path(pane_b_dir) / "b.jsonl"),
    ]


@contextmanager
def _boot_patched(cfg: ChainConfig, do_round_side_effect=None,
                  clear_and_recap_side_effect=None):
    """Patch everything run_chain touches before/inside the main loop.

    ``do_round_side_effect`` and ``clear_and_recap_side_effect`` are the
    only knobs — tests drive loop behaviour through those. Defaults are
    sane successful values so only the stall under test matters.
    """
    # Cursors: two distinct files so the initial JSONL-collision guard
    # outside the loop passes.
    cursors = _distinct_cursors(cfg)

    patches = [
        patch.object(chain, "setup_workspace", MagicMock()),
        patch.object(chain, "setup_tmux", MagicMock()),
        patch.object(chain, "_write_pane_claude_md", MagicMock()),
        patch.object(chain, "start_agent", MagicMock()),
        patch("time.sleep", MagicMock()),
        patch.object(chain, "wait_for_idle", MagicMock(return_value=True)),
        patch.object(chain, "inject_message", MagicMock(return_value=True)),
        patch.object(chain, "wait_and_extract",
                     MagicMock(return_value="seed response")),
        patch.object(chain, "discover_jsonl_with_retries",
                     MagicMock(side_effect=cursors)),
        # Keep the loop alive across iterations. The only legitimate loop
        # exit besides KeyboardInterrupt is a dead tmux session; we keep
        # the session "alive" so loop-content behaviour is what the tests
        # actually measure.
        patch.object(chain, "_session_alive", MagicMock(return_value=True)),
        # Stub ChainCoordinatorContext — the real one opens/locks
        # coordination.json via project_coordinator, which fights the
        # blanket builtins.open patch below.
        patch.object(chain, "ChainCoordinatorContext", _FakeCoordCtx),
        patch.object(reg, "register_chain", MagicMock(return_value=None)),
        patch.object(reg, "unregister_chain", MagicMock()),
        patch.object(reg, "update_chain", MagicMock()),
        patch("builtins.open", MagicMock()),
    ]

    if do_round_side_effect is not None:
        patches.append(
            patch.object(chain, "do_round",
                         MagicMock(side_effect=do_round_side_effect))
        )
    if clear_and_recap_side_effect is not None:
        patches.append(
            patch.object(chain, "clear_and_recap",
                         MagicMock(side_effect=clear_and_recap_side_effect))
        )
    else:
        # The loop increments rounds_since_reset on every iteration (stall
        # recoveries included), so CLEAR_EVERY=5 means reset fires on
        # round 5 regardless of stalls. Without this default stub the real
        # clear_and_recap runs, tries to write bulletin.md under
        # chainwork/<chain_id>/, and crashes tests that only care about
        # empty / duplicate-output recovery.
        fresh_cursor_a = MagicMock(file_path=Path(cfg.pane_dirs()[0]) / "ra.jsonl")
        fresh_cursor_b = MagicMock(file_path=Path(cfg.pane_dirs()[1]) / "rb.jsonl")
        patches.append(
            patch.object(
                chain, "clear_and_recap",
                MagicMock(return_value=(
                    fresh_cursor_a, fresh_cursor_b, "post-reset",
                    "recap-a", "recap-b", "a-post-reset",
                )),
            )
        )

    started = [p.start() for p in patches]
    try:
        yield started
    finally:
        for p in reversed(patches):
            try:
                p.stop()
            except RuntimeError:
                pass


# --- Empty A output recovery --------------------------------------------------


def test_loop_continues_on_empty_agent_a_output(tmp_path, monkeypatch):
    """Empty output from Agent A must nudge pane A and continue — never break."""
    cfg = _make_cfg(tmp_path, chain_id="emptyA", project=tmp_path)

    # Round 1: inside the seed path (pre-loop) returns "seed response" via
    # wait_and_extract. do_round is called starting at round 2.
    # Side-effect plan: two None returns (empty A) to prove the loop
    # survives them, then raise _LoopStop so the test finishes.
    call_log: list[tuple[str, str]] = []

    def _side_effect(pane, cursor, label, content, log_file, round_num):
        call_log.append((label, content[:40]))
        if label == "Agent A" and len(call_log) <= 3:
            return None  # stall
        # After three empty-A rounds, raise so the test ends deterministically.
        raise _LoopStop

    with _boot_patched(cfg, do_round_side_effect=_side_effect):
        with pytest.raises(_LoopStop):
            chain.run_chain(seed="seed", cfg=cfg, focus="")

    # Three empty rounds ran + one final call that raised the sentinel.
    # If the loop had broken on the first empty, call_log would be length 1.
    assert len(call_log) == 4, (
        f"Loop broke early. Expected 4 Agent A calls (3 stalls + 1 sentinel "
        f"trigger), got {len(call_log)}: {call_log!r}"
    )
    assert all(lbl == "Agent A" for lbl, _ in call_log), (
        "Agent B should never be called when Agent A stalls — no output to "
        "feed. Call log: " + repr(call_log)
    )


def test_loop_nudges_pane_a_with_stall_text(tmp_path):
    """On empty Agent A output the loop must inject STALL_NUDGE into pane A."""
    cfg = _make_cfg(tmp_path, chain_id="nudgeA", project=tmp_path)

    counter = {"calls": 0}

    def _side_effect(pane, cursor, label, content, log_file, round_num):
        counter["calls"] += 1
        if counter["calls"] == 1:
            return None
        raise _LoopStop

    with _boot_patched(cfg, do_round_side_effect=_side_effect) as _:
        inject = chain.inject_message  # currently a MagicMock
        with pytest.raises(_LoopStop):
            chain.run_chain(seed="seed", cfg=cfg, focus="")

        pane_a_target = f"{cfg.session}:0.0"
        nudge_calls = [
            call for call in inject.call_args_list
            if len(call.args) >= 2 and call.args[1] == chain.STALL_NUDGE
        ]
        assert nudge_calls, (
            f"STALL_NUDGE was never injected. All inject_message calls: "
            f"{inject.call_args_list!r}"
        )
        assert any(call.args[0] == pane_a_target for call in nudge_calls), (
            f"STALL_NUDGE should target pane A ({pane_a_target}); got calls: "
            f"{[c.args[0] for c in nudge_calls]!r}"
        )


# --- Empty B output recovery --------------------------------------------------


def test_loop_continues_on_empty_agent_b_output(tmp_path):
    """Empty output from Agent B must nudge pane B and continue — never break."""
    cfg = _make_cfg(tmp_path, chain_id="emptyB", project=tmp_path)

    call_log: list[tuple[str, int]] = []

    def _side_effect(pane, cursor, label, content, log_file, round_num):
        call_log.append((label, round_num))
        if label == "Agent A":
            return "A speaks round " + str(round_num)
        # Agent B: first two turns stall, then sentinel.
        b_calls = sum(1 for lbl, _ in call_log if lbl == "Agent B")
        if b_calls <= 2:
            return None
        raise _LoopStop

    with _boot_patched(cfg, do_round_side_effect=_side_effect):
        with pytest.raises(_LoopStop):
            chain.run_chain(seed="seed", cfg=cfg, focus="")

    b_calls = [lbl for lbl, _ in call_log if lbl == "Agent B"]
    # Three B calls: 2 stalls + 1 sentinel.
    assert len(b_calls) == 3, (
        f"Loop broke on empty Agent B output. Expected 3 B calls, got "
        f"{len(b_calls)}: {call_log!r}"
    )


def test_loop_nudges_pane_b_with_stall_text(tmp_path):
    """On empty Agent B output the loop must inject STALL_NUDGE into pane B."""
    cfg = _make_cfg(tmp_path, chain_id="nudgeB", project=tmp_path)

    counter = {"a": 0, "b": 0}

    def _side_effect(pane, cursor, label, content, log_file, round_num):
        if label == "Agent A":
            counter["a"] += 1
            return "A says something"
        counter["b"] += 1
        if counter["b"] == 1:
            return None
        raise _LoopStop

    with _boot_patched(cfg, do_round_side_effect=_side_effect):
        inject = chain.inject_message
        with pytest.raises(_LoopStop):
            chain.run_chain(seed="seed", cfg=cfg, focus="")

        pane_b_target = f"{cfg.session}:0.1"
        nudge_calls = [
            call for call in inject.call_args_list
            if len(call.args) >= 2
            and call.args[1] == chain.STALL_NUDGE
            and call.args[0] == pane_b_target
        ]
        assert nudge_calls, (
            f"STALL_NUDGE was never injected into pane B. inject calls: "
            f"{inject.call_args_list!r}"
        )


# --- Identical outputs recovery (forces reset, never exits) -------------------


def test_loop_continues_on_identical_outputs(tmp_path):
    """a_output == b_output is treated as a reset trigger, not an exit."""
    cfg = _make_cfg(tmp_path, chain_id="same", project=tmp_path)

    do_round_calls: list[str] = []
    recap_calls: list[int] = []

    # clear_and_recap returns a full successful tuple so the loop takes the
    # reset branch cleanly and keeps going.
    fresh_cursor_a = MagicMock(file_path=tmp_path / "reset-a.jsonl")
    fresh_cursor_b = MagicMock(file_path=tmp_path / "reset-b.jsonl")
    successful_reset = (
        fresh_cursor_a, fresh_cursor_b, "post-reset message",
        "recap-a", "recap-b", "a-post-reset",
    )

    def _clear_and_recap(*args, **kwargs):
        recap_calls.append(args[5] if len(args) > 5 else -1)
        if len(recap_calls) >= 2:
            raise _LoopStop  # end the test after the second reset fires
        return successful_reset

    def _side_effect(pane, cursor, label, content, log_file, round_num):
        do_round_calls.append(label)
        # Return the exact same string for both A and B — triggers the
        # a_output == b_output branch every round.
        return "identical text"

    with _boot_patched(
        cfg,
        do_round_side_effect=_side_effect,
        clear_and_recap_side_effect=_clear_and_recap,
    ):
        with pytest.raises(_LoopStop):
            chain.run_chain(seed="seed", cfg=cfg, focus="")

    # The old code would break on the first a == b and recap_calls would
    # be empty. The new code forces a reset on the next iteration — so
    # clear_and_recap fires at least once even though no natural reset
    # cadence was reached.
    assert len(recap_calls) >= 1, (
        "Loop broke on a_output == b_output instead of forcing a curator "
        f"reset on the next iteration. do_round calls: {do_round_calls!r}"
    )


# --- Curator-failure recovery -------------------------------------------------


def test_loop_continues_on_curator_recap_failure(tmp_path):
    """clear_and_recap returning the all-None tuple must not break the loop."""
    cfg = _make_cfg(tmp_path, chain_id="recapfail", project=tmp_path)

    # CLEAR_EVERY defaults to 5: force a reset attempt by letting the
    # relay run 5 rounds, fail the recap, then keep going.
    do_round_count = {"n": 0}
    recap_count = {"n": 0}

    def _side_effect(pane, cursor, label, content, log_file, round_num):
        do_round_count["n"] += 1
        if do_round_count["n"] > 40:
            # Safety: if the loop truly never exits in this test harness
            # (no sentinel ever raised) we still want to fail fast rather
            # than hang. The assert below will trigger on recap_count.
            raise _LoopStop
        return f"round {round_num} {label}"

    def _clear_and_recap(*args, **kwargs):
        recap_count["n"] += 1
        if recap_count["n"] <= 2:
            # Both first and second attempt fail — prove the loop survives
            # back-to-back curator failures.
            return (None, None, None, None, None, None)
        raise _LoopStop  # third attempt: end the test

    with _boot_patched(
        cfg,
        do_round_side_effect=_side_effect,
        clear_and_recap_side_effect=_clear_and_recap,
    ):
        with pytest.raises(_LoopStop):
            chain.run_chain(seed="seed", cfg=cfg, focus="")

    assert recap_count["n"] >= 3, (
        "Loop must survive a curator failure and attempt another reset "
        f"later. Only saw {recap_count['n']} reset attempt(s)."
    )


# --- Source-level invariant ---------------------------------------------------


def test_run_chain_main_loop_has_no_content_termination_branches():
    """Static guard: the only ``break`` inside run_chain's ``while True``
    block is the dead-tmux exit — nothing on agent content.

    The loop exits via KeyboardInterrupt or a dead tmux session. Those are
    the only two. Any ``break`` that isn't guarded by ``_session_alive``
    is a regression of the four removed content-termination exits and
    must fail this test immediately.
    """
    src = Path(chain.__file__).read_text(encoding="utf-8").splitlines()
    # Find the start of the main while True line inside run_chain.
    in_run_chain = False
    while_line_idx: int | None = None
    outer_indent: int | None = None
    for i, line in enumerate(src):
        stripped = line.lstrip()
        if stripped.startswith("def run_chain("):
            in_run_chain = True
            continue
        if in_run_chain and stripped.startswith("def "):
            # Next top-level def: we're past run_chain.
            break
        if in_run_chain and stripped.startswith("while True:"):
            while_line_idx = i
            outer_indent = len(line) - len(stripped)
            break
    assert while_line_idx is not None and outer_indent is not None, (
        "Couldn't locate the `while True:` inside run_chain — test fixture "
        "is out of date with chain.py."
    )

    # Walk forward from the while line. Any line indented deeper than
    # outer_indent belongs to the while body. Stop when we hit a line at
    # or below outer_indent that isn't blank/comment (that's the dedent
    # ending the while block).
    body_lines: list[tuple[int, str]] = []
    for j in range(while_line_idx + 1, len(src)):
        line = src[j]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= outer_indent and line.strip() and not line.lstrip().startswith("#"):
            break
        body_lines.append((j + 1, line))

    # A ``break`` is allowed iff the preceding non-blank line is the
    # dead-tmux logger.info() call — i.e. it's the body of the
    # ``if not _session_alive(session):`` guard. Anything else is
    # a content-termination regression.
    offending: list[tuple[int, str]] = []
    for idx, (ln, text) in enumerate(body_lines):
        if not text.lstrip().startswith("break"):
            continue
        # Look backwards through the body for the nearest non-blank line
        # with indent strictly less than this break — that's the guarding
        # ``if`` line (or a log call immediately preceding it).
        preceding_block = "\n".join(bl[1] for bl in body_lines[max(0, idx - 6):idx])
        if "_session_alive" in preceding_block:
            continue
        offending.append((ln, text))

    assert not offending, (
        "run_chain's `while True:` block must not contain any `break` "
        "except the dead-tmux exit guarded by `_session_alive`. "
        "Content-termination exits (empty output, duplicate output, "
        "recap failure) are regressions. Found: " +
        "; ".join(f"line {ln}: {text.strip()}" for ln, text in offending)
    )


@pytest.mark.parametrize(
    "prompt_name,prompt_text",
    [
        ("JANITOR_SYSTEM_PROMPT", chain.JANITOR_SYSTEM_PROMPT),
        ("BULLETIN_SYSTEM_PROMPT", chain.BULLETIN_SYSTEM_PROMPT),
    ],
)
def test_curator_prompts_forbid_convergence_language(prompt_name, prompt_text):
    """Both curator prompts must tell the model the dialogue is indefinite.

    The recap (JANITOR_SYSTEM_PROMPT) and the bulletin (BULLETIN_SYSTEM_PROMPT)
    are both curator outputs that feed the agents' next-round context. If
    either drifts toward "they've converged, nothing left to explore", that
    language propagates: the bulletin feeds into the recap as CURATOR
    OBSERVATIONS, and the recap is the ONLY context the agents have after a
    reset. A convergence verdict written in either prompt's output tells the
    agents the loop has a valid terminal state — which is the exact
    regression the loop-sacredness fix exists to prevent.

    Pin both prompts to the "indefinite / no end state / no we've converged"
    guardrail. Phrased as a presence check rather than an exact-string
    match so minor wording edits don't break the test, but any removal of
    the core invariant fails it loudly.
    """
    lowered = prompt_text.lower()
    # The three phrases the loop-sacredness fix introduced. Any one of
    # them disappearing from a prompt is a regression of the hardening.
    assert "indefinite" in lowered, (
        f"{prompt_name} must describe the dialogue as INDEFINITE so the "
        "curator doesn't narrate toward a concluding state."
    )
    assert "no end state" in lowered, (
        f"{prompt_name} must explicitly say there is no end state. "
        "Removing this invites the curator to declare the conversation done."
    )
    assert "converged" in lowered, (
        f"{prompt_name} must name the 'we've converged' failure mode so "
        "the curator knows to treat agent-side convergence claims as "
        "patterns to probe, not verdicts to ratify."
    )
