#!/usr/bin/env python3
"""
Interpretation chain loop with janitor-powered context resets.

Two persistent Claude sessions in tmux pass output back and forth, working
inside a target project directory with full tool access. Every N rounds the
engine clears both sessions and injects a janitor-curated recap, so the
agents restart sharp instead of degrading.

Usage:
    python3 chain.py "your seed topic" --project /path/to/project
    python3 chain.py "your seed topic"           # --project defaults to cwd
    python3 chain.py --list                      # list active chains
    python3 chain.py --stop CHAIN_ID             # stop a chain
    python3 chain.py --attach CHAIN_ID           # attach terminal to chain's tmux

Monitor:
    tail -f chainwork/<chain-id>/chain_log.md  # conversation log
    tmux attach -t chain-<chain-id>            # watch agents live
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from janitor.injection import is_agent_idle, tmux_capture, READY_PATTERNS

# Add bypass-mode patterns so idle detection works with --dangerously-skip-permissions
READY_PATTERNS.append("bypass permissions")
READY_PATTERNS.append("shift+tab to cycle")
from janitor.janitor_cli import call_janitor
from janitor.jsonl_reader import (
    JSONLCursor,
    discover_jsonl_for_pane,
    read_new_entries,
)
import registry as reg
from chain_coordinator import ChainCoordinatorContext
import coordination_prompt

logger = logging.getLogger("chain")

SESSION = "chain"
REPO_DIR = Path(__file__).parent.resolve()
WORKSPACE = REPO_DIR / "chainwork"

CLEAR_EVERY = 5

# Nudge injected into a pane when its agent produces no text for a round.
# The loop is infinite — an empty turn is a stall, not an exit signal. The
# nudge is short and agent-neutral so it doesn't derail the dialogue.
STALL_NUDGE = (
    "Keep going. Even a single sentence is valid output. "
    "The loop is infinite — there is no concluding state to reach. "
    "Respond with your next thought."
)
ROLES_DIR = REPO_DIR / "roles"


def load_role(filename: str) -> str:
    path = ROLES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Role file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _reject_project_inside_repo(project_path: Path) -> None:
    """Refuse to launch a chain pointed at this dialectic repo itself.

    Called from ``__main__`` before any tmux session, registry write, or
    coordination state is touched. A chain accidentally pointed at this
    repo would commit runtime artifacts and scratch dirs into the tool's
    own tree, so the launch is rejected before side effects land.
    """
    if project_path.is_relative_to(REPO_DIR):
        logger.error(
            "Refusing to launch: --project resolves inside the dialectic "
            f"repo itself ({REPO_DIR}). Chains must target a different "
            f"project directory. Resolved project path: {project_path}."
        )
        sys.exit(2)


def _preflight_binaries() -> None:
    """Fail with a clear one-liner when a required binary is missing.

    Called from ``__main__`` after the launch guards and before any side
    effect (registry write, tmux session). Without this, a missing tmux
    surfaces as a raw traceback and a missing/unauthenticated claude as a
    60-second silent hang followed by "Agent A didn't start".
    """
    problems = []
    if shutil.which("tmux") is None:
        problems.append(
            "tmux not found on PATH. Install it first "
            "(macOS: brew install tmux, Debian/Ubuntu: apt install tmux)."
        )
    if shutil.which("claude") is None:
        problems.append(
            "claude (the Claude Code CLI) not found on PATH. Install and "
            "authenticate it first: https://docs.anthropic.com/en/docs/claude-code"
        )
    if problems:
        for p in problems:
            logger.error(p)
        sys.exit(1)


PROMPT_A = load_role("builder.txt")
PROMPT_B = load_role("thinker.txt")

JANITOR_SYSTEM_PROMPT = """\
You are a conversation curator. You are reading the full transcript of a \
dialogue between two thinkers -- a pragmatic builder and a pattern thinker.

Your job: produce a RECAP that lets this thinker restart the conversation \
from a clean context without losing anything important. This recap will be \
the ONLY context they have. Everything else is gone.

The recap must include:
- The key threads and ideas that emerged across the full dialogue
- Where the thinking currently stands -- what has been settled, what is open
- What tensions or disagreements exist between the two perspectives
- What the most recent exchange was about and where it was heading
- This thinker's own position and how it has evolved

GUIDELINES:
- EXTRACT and QUOTE key phrases rather than summarizing. Summaries lose nuance.
- Preserve the specific language and framing each thinker used.
- Include enough that the thinker can pick up mid-thought, not just mid-topic.
- Do NOT use tools. Text analysis only.
- The dialogue is INDEFINITE. There is no end state, no final answer, no \
"we've converged." Your recap must never suggest the conversation is \
complete or that there is nothing left to explore. If the agents themselves \
claim convergence, note that as a PATTERN to probe, not a verdict to \
ratify. Every reset is a chance for fresh eyes on the same material -- \
frame the recap as a springboard for continued thinking, never a closing \
summary.

If a PREVIOUS RECAP is provided, UPDATE it rather than starting from scratch:
- Carry forward everything from the previous recap that is still relevant.
- Add new threads, decisions, and positions from the new dialogue.
- Remove or correct anything the new dialogue has superseded.
- Do NOT re-summarize what the previous recap already captured well.

If CURATOR OBSERVATIONS are provided, use them to inform your recap:
- These are long-term patterns observed across multiple resets.
- If a thread has been flagged as recurring, mention this in the recap.
- If the observations note a trajectory or pattern, reflect it.
- Do NOT simply paste the observations into the recap. Weave them in naturally.

Respond with a single RECAP section. Nothing else.

RECAP:
[your recap here]"""

BULLETIN_SYSTEM_PROMPT = """\
You are the long-term memory of a conversation curator. You observe a \
dialogue between two thinkers across multiple context resets. The agents \
forget everything at each reset. You do not.

You are reading the combined dialogue from BOTH agents since the last reset, \
plus your own previous observations (the BULLETIN). Your job: update the \
bulletin with what you notice.

The bulletin is YOUR notes to YOUR future self. Not a recap. Not a summary. \
It is structural observation:

- What threads keep resurfacing? ("X came back for the 3rd time")
- What was resolved vs. what the agents think was resolved but wasn't?
- What patterns exist in how the agents interact? (who leads, who defers, \
  where they get stuck, what breaks deadlocks)
- What is the conversation's trajectory? Where is it actually going \
  vs. where the agents think it's going?
- What should you watch for next time?

GUIDELINES:
- Be specific. Name the threads, quote the phrases.
- Track recurrence with counts. "First appearance" vs "3rd recurrence."
- Note what changed since your last bulletin, not just what exists.
- If this is the first reset, everything is new -- just observe.
- Keep it under 2000 characters. Density over length.
- Do NOT use tools. Text analysis only.
- The dialogue is INDEFINITE. There is no end state, no final answer, no \
"we've converged." Your bulletin must never declare the conversation \
complete, exhausted, or out of angles. If the agents themselves claim \
convergence, log that as a RECURRING PATTERN to watch ("agents declared \
closure on thread X -- 2nd time"), not as a verdict the curator endorses. \
The bulletin feeds into the next recap, so framing a thread as \
"resolved, nothing more to explore" would propagate termination language \
into the agents' fresh context. Describe trajectories as trajectories, \
not destinations. Every reset is a chance for fresh eyes on the same \
material.

Respond with a single BULLETIN section. Nothing else.

BULLETIN:
[your observations here]"""


# --- Setup ---

WS_A = WORKSPACE / SESSION / "a"
WS_B = WORKSPACE / SESSION / "b"

# Project mode: when set, agents run inside the target project with tools enabled
PROJECT_DIR: Path | None = None


def setup_workspace(cfg: "reg.ChainConfig | None" = None):
    if cfg:
        cfg.ws_a.mkdir(parents=True, exist_ok=True)
        cfg.ws_b.mkdir(parents=True, exist_ok=True)
    else:
        WS_A.mkdir(parents=True, exist_ok=True)
        WS_B.mkdir(parents=True, exist_ok=True)


def _project_pane_dirs(cfg: "reg.ChainConfig | None" = None) -> tuple[Path, Path]:
    """Return per-pane cwd directories, creating them if needed."""
    if cfg:
        return cfg.pane_dirs()
    if PROJECT_DIR:
        dir_a = PROJECT_DIR / ".dialectic-a"
        dir_b = PROJECT_DIR / ".dialectic-b"
        dir_a.mkdir(exist_ok=True)
        dir_b.mkdir(exist_ok=True)
        return dir_a, dir_b
    return WS_A, WS_B


def setup_tmux(cfg: "reg.ChainConfig | None" = None):
    session = cfg.session if cfg else SESSION
    dir_a, dir_b = _project_pane_dirs(cfg)
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True, check=False,
    )
    time.sleep(0.5)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(dir_a)],
        check=True,
    )
    subprocess.run(
        ["tmux", "split-window", "-h", "-t", f"{session}:0", "-c", str(dir_b)],
        check=True,
    )


def _write_pane_claude_md(pane_dir: Path, role_prompt: str, cfg: "reg.ChainConfig | None" = None, focus: str = ""):
    """Write role and directives to CLAUDE.md in the pane's working directory.

    Path references sent to the agent are absolute, not relative to "the
    project root". The pane's cwd is <project>/.dialectic-a-<chain_id>/
    (or similar), which makes "project root" ambiguous — one pane would
    read it as cwd and write the plan file inside the scratch dir, its
    counterpart would climb up to the real project root, and the two
    chains would lose track of each other. Substituting the absolute
    path (cfg.plan_path, cfg.project_dir) makes that class of bug
    impossible.
    """
    project_dir = cfg.project_dir if cfg else PROJECT_DIR
    sections = [role_prompt]
    if project_dir:
        project_root = project_dir.resolve()
        project_claude_md = project_root / "CLAUDE.md"
        if project_claude_md.exists():
            sections.append(project_claude_md.read_text(encoding="utf-8").strip())
        # Per-chain plan filename so concurrent chains on the same project
        # don't clobber each other's working doc. We send the absolute path
        # so the agent has nothing to interpret.
        if cfg and cfg.plan_path:
            plan_abs = cfg.plan_path.resolve() if cfg.plan_path.is_absolute() or project_root.exists() else (project_root / cfg.plan_path.name)
            plan_display = str(plan_abs)
            # Defensive: if resolve() folded symlinks in a way that moved
            # the file out of project_root, fall back to the direct join.
            if not plan_display.startswith(str(project_root)):
                plan_display = str(project_root / cfg.plan_path.name)
        else:
            plan_display = str(project_root / "plan.md")
        sections.append(
            f"Maintain a working doc at the absolute path `{plan_display}`. "
            "Use exactly that path — do NOT create a plan file inside this "
            "pane's scratch directory, and do NOT invent an alternative "
            "location. This file is scoped to YOUR chain only -- other "
            "chains on the same project have their own plan-<chain-id>.md "
            "alongside yours. Update it as your thinking evolves -- what's "
            "been decided, what's open, what's next. It survives context "
            "resets. Treat it as the source of truth.\n\n"
            f"The project root is `{project_root}`. Any other project-"
            "scoped artefacts (notes, references, shared docs) belong "
            "under that absolute path, not under your pane's cwd."
        )
        sections.append(
            "You are in a relay -- only your final text output reaches your counterpart. "
            "Everything before your last message is lost to them. "
            "After any tool use, restate your full thinking -- your analysis, position, "
            "and direction -- in your final message. A short follow-up question alone is not enough. "
            "Your counterpart must be able to understand your full response without seeing your tool calls."
        )
        if cfg and cfg.chain_id:
            sections.append(
                coordination_prompt.claude_md_section(
                    project_dir=project_root,
                    chain_id=cfg.chain_id,
                    focus=focus,
                )
            )
    claude_md = pane_dir / "CLAUDE.md"
    claude_md.write_text("\n\n".join(sections) + "\n", encoding="utf-8")


def start_agent(pane_idx: int, name: str, cfg: "reg.ChainConfig | None" = None):
    session = cfg.session if cfg else SESSION
    pane = f"{session}:0.{pane_idx}"
    cmd = f'claude --dangerously-skip-permissions --name "{name}"'
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        cmd = f'CLAUDE_CONFIG_DIR="{config_dir}" {cmd}'
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, cmd, "C-m"],
        check=True,
    )


# --- Tmux interaction ---


def inject_message(pane_target: str, message: str) -> bool:
    buf_name = f"chain_{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_target, "C-u"],
            check=False, timeout=5,
        )
        time.sleep(0.05)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        ) as f:
            f.write(message)
            tmp_path = f.name

        try:
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf_name, tmp_path],
                check=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "paste-buffer", "-d", "-b", buf_name, "-t", pane_target],
                check=True, timeout=5,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                content = tmux_capture(pane_target)
                if "[Pasted text" in content:
                    break
                time.sleep(0.3)
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_target, "C-m"],
                check=True, timeout=5,
            )
            for retry in range(3):
                time.sleep(1.0)
                post = tmux_capture(pane_target)
                if "[Pasted text" not in post:
                    break
                logger.warning(f"Inject verify: paste still pending after Enter (retry {retry + 1})")
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_target, "C-m"],
                    check=True, timeout=5,
                )
            return True
        finally:
            os.unlink(tmp_path)

    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        logger.error(f"Injection failed: {e}")
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buf_name],
            check=False, timeout=5,
        )
        return False


def _last_assistant_stop_reason(jsonl_path: Path) -> str | None:
    """Peek at the last assistant entry's stop_reason without advancing any cursor."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 8192)
            f.seek(size - read_size)
            tail = f.read().decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return None
    for line in reversed(tail.strip().split("\n")):
        try:
            entry = json.loads(line)
            if entry.get("type") == "assistant":
                return entry.get("message", {}).get("stop_reason")
        except json.JSONDecodeError:
            continue
    return None


def wait_for_idle(pane_target: str, timeout: int = 7200, is_mid_turn=None) -> bool:
    time.sleep(8)  # Let agent start processing before polling
    start = time.time()
    while time.time() - start < timeout:
        if is_agent_idle(pane_target, is_mid_turn=is_mid_turn):
            return True
        time.sleep(3)
    return False


# --- JSONL ---

def _is_real_user_entry(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content", "")
    return isinstance(content, str)


def extract_last_assistant_text(entries: list[dict]) -> str:
    all_texts = []
    for entry in reversed(entries):
        if _is_real_user_entry(entry):
            break
        if entry.get("type") != "assistant":
            continue
        content_blocks = entry.get("message", {}).get("content", [])
        if not isinstance(content_blocks, list):
            continue
        for block in content_blocks:
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    all_texts.append(text)
    all_texts.reverse()
    return "\n\n".join(all_texts) if all_texts else ""


def _turn_complete(entries: list[dict]) -> bool:
    """Check if the agent's turn is finished."""
    for entry in reversed(entries):
        t = entry.get("type", "")
        if t == "system":
            return True
        if t == "assistant":
            return entry.get("message", {}).get("stop_reason") == "end_turn"
    return False


def wait_and_extract(
    cursor: JSONLCursor, timeout: int = 7200, pane_target: str | None = None,
) -> str:
    """Keep reading JSONL until the turn completes (end_turn/system in JSONL)."""
    STALE_THRESHOLD = 30
    all_entries = []
    start = time.time()
    last_new_data_time = time.time()
    while time.time() - start < timeout:
        new = read_new_entries(cursor)
        if new:
            all_entries.extend(new)
            last_new_data_time = time.time()
        text = extract_last_assistant_text(all_entries)
        if text and _turn_complete(all_entries):
            return text
        if (pane_target and text
                and time.time() - last_new_data_time > STALE_THRESHOLD
                and is_agent_idle(pane_target)):
            logger.warning(
                "JSONL completion signal missing but agent is idle. "
                "Extracting via fallback."
            )
            return text
        time.sleep(3)
    return ""


_FRAMING_TAG_RE = re.compile(
    r"<(?:role|project|format)>\n.*?\n</(?:role|project|format)>",
    re.DOTALL,
)


def _strip_framing_tags(text: str) -> str:
    """Remove <role>, <project>, <format> blocks from dialogue text."""
    return _FRAMING_TAG_RE.sub("", text).strip()


def extract_full_dialogue(jsonl_path: Path) -> str:
    cursor = JSONLCursor(file_path=jsonl_path, byte_offset=0)
    entries = read_new_entries(cursor)

    dialogue = []
    for entry in entries:
        entry_type = entry.get("type", "")
        msg = entry.get("message", {})

        if entry_type == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                if "<context-refresh>" in content:
                    continue
                clean = _strip_framing_tags(content[:12000])
                dialogue.append(f"USER: {clean}")

        elif entry_type == "assistant":
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            dialogue.append(f"ASSISTANT: {text[:12000]}")

    return "\n\n".join(dialogue)


def discover_jsonl_with_retries(
    pane_target: str, project_path: Path, retries: int = 15
) -> JSONLCursor | None:
    for attempt in range(retries):
        path = discover_jsonl_for_pane(pane_target, project_path)
        if path:
            return JSONLCursor(file_path=path)
        if attempt < retries - 1:
            time.sleep(2)
    return None


# --- Janitor ---

RECAP_MAX_RETRIES = 3


def _parse_recap(raw: str) -> str:
    lines = raw.split("\n")
    recap_lines = []
    in_recap = False
    for line in lines:
        if line.strip().upper().startswith("RECAP:"):
            in_recap = True
            after = line.split(":", 1)[-1].strip()
            if after:
                recap_lines.append(after)
            continue
        if in_recap:
            recap_lines.append(line)
    return "\n".join(recap_lines).strip() if recap_lines else raw.strip()


def _is_valid_recap(raw: str, recap: str) -> bool:
    """A recap is valid iff it has the RECAP: header and any non-empty body.

    No character minimum: a tight 200-char recap of dense signal beats a
    padded 2000-char one. The header check is the only contract -- it tells
    us the curator followed the format and didn't just echo back the prompt.
    """
    if not recap:
        return False
    return "RECAP:" in raw.upper()


def get_recap(
    jsonl_path: Path, role_label: str,
    previous_recap: str | None = None,
    bulletin: str | None = None,
) -> str | None:
    dialogue = extract_full_dialogue(jsonl_path)
    if not dialogue:
        return None

    parts = [f"AGENT ROLE: {role_label}"]
    if bulletin:
        parts.append(
            f"CURATOR OBSERVATIONS (long-term patterns across resets):\n{bulletin}"
        )
    if previous_recap:
        parts.append(f"PREVIOUS RECAP (from last reset):\n{previous_recap}")
        parts.append(f"NEW DIALOGUE (since last reset):\n{dialogue}")
    else:
        parts.append(f"FULL DIALOGUE:\n{dialogue}")
    prompt = "\n\n".join(parts)

    for attempt in range(1, RECAP_MAX_RETRIES + 1):
        logger.info(f"  Calling janitor for {role_label} (attempt {attempt}, {len(dialogue)} chars input)...")
        result = call_janitor(
            prompt=prompt,
            system_prompt=JANITOR_SYSTEM_PROMPT,
            timeout=300,
            effort="high",
        )

        if not result.success:
            logger.error(f"  Janitor failed for {role_label}: {result.error}")
            continue

        raw = result.working_set
        recap = _parse_recap(raw)

        if _is_valid_recap(raw, recap):
            logger.info(f"  Recap for {role_label}: {len(recap)} chars (attempt {attempt})")
            return recap

        logger.warning(
            f"  Invalid recap for {role_label} (attempt {attempt}): "
            f"{len(recap)} chars, has RECAP: {'yes' if 'RECAP:' in raw.upper() else 'no'}"
        )

    logger.error(f"  All {RECAP_MAX_RETRIES} recap attempts failed for {role_label}")
    return None


def _parse_bulletin(raw: str) -> str:
    lines = raw.split("\n")
    bulletin_lines = []
    in_bulletin = False
    for line in lines:
        if line.strip().upper().startswith("BULLETIN:"):
            in_bulletin = True
            after = line.split(":", 1)[-1].strip()
            if after:
                bulletin_lines.append(after)
            continue
        if in_bulletin:
            bulletin_lines.append(line)
    return "\n".join(bulletin_lines).strip() if bulletin_lines else ""


def update_bulletin(
    dialogue_a: str, dialogue_b: str, previous_bulletin: str | None = None,
) -> str | None:
    parts = []
    if previous_bulletin:
        parts.append(f"YOUR PREVIOUS BULLETIN:\n{previous_bulletin}")
    parts.append(f"BUILDER DIALOGUE (since last reset):\n{dialogue_a}")
    parts.append(f"THINKER DIALOGUE (since last reset):\n{dialogue_b}")
    prompt = "\n\n".join(parts)

    logger.info(f"  Updating bulletin ({len(prompt)} chars input)...")
    result = call_janitor(
        prompt=prompt,
        system_prompt=BULLETIN_SYSTEM_PROMPT,
        timeout=300,
        effort="high",
    )

    if not result.success:
        logger.error(f"  Bulletin update failed: {result.error}")
        return previous_bulletin

    bulletin = _parse_bulletin(result.working_set)
    if not bulletin:
        logger.warning("  Bulletin empty, keeping previous")
        return previous_bulletin

    logger.info(f"  Bulletin updated: {len(bulletin)} chars")
    return bulletin


def clear_and_recap(
    pane_a: str, pane_b: str,
    cursor_a: JSONLCursor, cursor_b: JSONLCursor,
    log_file: Path, round_num: int,
    previous_recap_a: str | None = None,
    previous_recap_b: str | None = None,
    bulletin_path: Path | None = None,
    cfg: "reg.ChainConfig | None" = None,
):
    logger.info(f"[Round {round_num}] === CONTEXT RESET ===")

    current_bulletin = None
    if bulletin_path:
        dialogue_a = extract_full_dialogue(cursor_a.file_path)
        dialogue_b = extract_full_dialogue(cursor_b.file_path)
        prev_bulletin = None
        if bulletin_path.exists():
            prev_bulletin = bulletin_path.read_text(encoding="utf-8").strip() or None
        current_bulletin = update_bulletin(dialogue_a, dialogue_b, prev_bulletin)
        if current_bulletin:
            bulletin_path.write_text(current_bulletin + "\n", encoding="utf-8")
            logger.info(f"  Bulletin written to {bulletin_path}")

    recap_a = get_recap(cursor_a.file_path, "Builder (pragmatic)", previous_recap_a, bulletin=current_bulletin)
    recap_b = get_recap(cursor_b.file_path, "Thinker (patterns)", previous_recap_b, bulletin=current_bulletin)

    if not recap_a or not recap_b:
        logger.error("Failed to generate recaps, skipping reset")
        return cursor_a, cursor_b, None, None, None, None

    with open(log_file, "a") as f:
        f.write(f"\n## Round {round_num} -- CONTEXT RESET\n\n")
        if bulletin_path and bulletin_path.exists():
            f.write(f"### Curator bulletin\n\n{bulletin_path.read_text(encoding='utf-8').strip()}\n\n")
        f.write(f"### Builder recap\n\n{recap_a}\n\n")
        f.write(f"### Thinker recap\n\n{recap_b}\n\n---\n")

    logger.info("  Clearing both agents...")
    subprocess.run(["tmux", "send-keys", "-t", pane_a, "/clear", "C-m"], check=False, timeout=5)
    subprocess.run(["tmux", "send-keys", "-t", pane_b, "/clear", "C-m"], check=False, timeout=5)
    time.sleep(5)

    if not wait_for_idle(pane_a, timeout=60):
        logger.error("  Agent A not ready after clear")
        return None, None, None, None, None, None
    if not wait_for_idle(pane_b, timeout=60):
        logger.error("  Agent B not ready after clear")
        return None, None, None, None, None, None

    logger.info("  Injecting recap into Agent A...")
    recap_msg_a = (
        f"<context-refresh>\nYou are resuming a dialogue. Here is the full context of where you left off:\n\n{recap_a}\n</context-refresh>\n\nContinue from here. What is your next thought?"
    )
    if not inject_message(pane_a, recap_msg_a):
        return None, None, None, None, None, None

    if not wait_for_idle(pane_a):
        return None, None, None, None, None, None

    new_cursor_a = discover_jsonl_with_retries(pane_a, _project_pane_dirs(cfg)[0])
    if not new_cursor_a:
        return None, None, None, None, None, None

    a_output = wait_and_extract(new_cursor_a, pane_target=pane_a)
    if not a_output:
        logger.error("  No output from Agent A after recap")
        return None, None, None, None, None, None

    with open(log_file, "a") as f:
        f.write(f"\n## Round {round_num} -- Builder (post-reset)\n\n{a_output}\n\n---\n")
    logger.info(f"  Agent A post-reset: {len(a_output)} chars")

    logger.info("  Injecting recap into Agent B...")
    recap_msg_b = (
        f"<context-refresh>\nYou are resuming a dialogue. Here is the full context of where you left off:\n\n{recap_b}\n</context-refresh>\n\nYour counterpart just said:\n\n{a_output}\n\nContinue from here."
    )
    if not inject_message(pane_b, recap_msg_b):
        return None, None, None, None, None, None

    if not wait_for_idle(pane_b):
        return None, None, None, None, None, None

    new_cursor_b = discover_jsonl_with_retries(pane_b, _project_pane_dirs(cfg)[1])
    if not new_cursor_b:
        return None, None, None, None, None, None

    if new_cursor_a.file_path == new_cursor_b.file_path:
        logger.error(
            f"JSONL COLLISION after reset: both agents resolved to "
            f"{new_cursor_a.file_path}. Aborting reset."
        )
        return None, None, None, None, None, None

    b_output = wait_and_extract(new_cursor_b, pane_target=pane_b)
    if not b_output:
        logger.error("  No output from Agent B after recap")
        return None, None, None, None, None, None

    with open(log_file, "a") as f:
        f.write(f"\n## Round {round_num} -- Thinker (post-reset)\n\n{b_output}\n\n---\n")
    logger.info(f"  Agent B post-reset: {len(b_output)} chars")
    logger.info(f"  === RESET COMPLETE ===")

    return new_cursor_a, new_cursor_b, b_output, recap_a, recap_b, a_output


# --- Main loop ---

def _session_alive(session: str) -> bool:
    """Return True iff the tmux session still exists.

    The relay loop is infinite by design. Agent content (empty output,
    identical output, failed recap) never ends it. A dead tmux session
    does — it's the only clean exit beside KeyboardInterrupt.
    """
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, check=False, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def _nudge_stalled_agent(pane: str, label: str, round_num: int) -> None:
    """Recovery path for an agent that produced no text this round.

    The relay loop is infinite. An empty turn is a stall, never an exit
    signal. Inject a short neutral nudge and wait (bounded) for the pane to
    settle so the next round's inject_message doesn't race with an
    in-flight paste.
    """
    logger.warning(
        f"[Round {round_num}] {label} produced no text — injecting stall nudge and continuing."
    )
    if inject_message(pane, STALL_NUDGE):
        wait_for_idle(pane, timeout=120)


def do_round(
    pane: str, cursor: JSONLCursor,
    label: str, content: str, log_file: Path, round_num: int,
) -> str | None:
    logger.info(f"[Round {round_num}] -> {label}")
    if not inject_message(pane, content):
        logger.error(f"Failed to inject into {label}")
        return None

    mid_turn = lambda: _last_assistant_stop_reason(cursor.file_path) == "tool_use"
    if not wait_for_idle(pane, is_mid_turn=mid_turn):
        logger.error(f"{label} timed out")
        return None

    output = wait_and_extract(cursor, pane_target=pane)

    if not output:
        logger.error(f"No output from {label}")
        return None

    with open(log_file, "a") as f:
        f.write(f"\n## Round {round_num} -- {label}\n\n{output}\n\n---\n")
    logger.info(f"[Round {round_num}] {label}: {len(output)} chars")
    return output


def run_chain(seed: str, prompt_a: str = PROMPT_A, prompt_b: str = PROMPT_B,
              cfg: "reg.ChainConfig | None" = None, focus: str = ""):
    if cfg:
        pane_a = f"{cfg.session}:0.0"
        pane_b = f"{cfg.session}:0.1"
        log_file = cfg.log_file
        bulletin_path = cfg.bulletin_path
        session = cfg.session
        clear_every = CLEAR_EVERY
    else:
        pane_a = f"{SESSION}:0.0"
        pane_b = f"{SESSION}:0.1"
        log_file = WORKSPACE / SESSION / "chain_log.md"
        bulletin_path = WORKSPACE / SESSION / "bulletin.md"
        session = SESSION
        clear_every = CLEAR_EVERY

    record = None
    if cfg:
        record = reg.register_chain(cfg, os.getpid())

    logger.info("Setting up workspace...")
    setup_workspace(cfg)

    logger.info("Creating tmux session...")
    setup_tmux(cfg)

    dir_a, dir_b = _project_pane_dirs(cfg)
    _write_pane_claude_md(dir_a, prompt_a, cfg, focus=focus)
    _write_pane_claude_md(dir_b, prompt_b, cfg, focus=focus)

    logger.info("Starting Agent A (builder)...")
    start_agent(0, "builder", cfg)
    time.sleep(5)

    logger.info("Starting Agent B (thinker)...")
    start_agent(1, "thinker", cfg)

    logger.info("Waiting for agents to boot...")
    time.sleep(15)

    if not wait_for_idle(pane_a, timeout=60):
        logger.error(
            f"Agent A didn't start. Check: tmux attach -t {session} — the pane "
            "may be showing Claude Code's one-time permissions prompt; accept "
            "it there and relaunch."
        )
        if record:
            reg.unregister_chain(cfg.chain_id)
        return
    if not wait_for_idle(pane_b, timeout=60):
        logger.error(
            f"Agent B didn't start. Check: tmux attach -t {session} — the pane "
            "may be showing Claude Code's one-time permissions prompt; accept "
            "it there and relaunch."
        )
        if record:
            reg.unregister_chain(cfg.chain_id)
        return

    logger.info("Both agents ready.")

    logger.info("[Round 1] Sending seed to Agent A...")
    if not inject_message(pane_a, seed):
        logger.error("Failed to inject seed")
        return

    if not wait_for_idle(pane_a):
        logger.error("Agent A timed out on seed")
        return

    cursor_a = discover_jsonl_with_retries(pane_a, _project_pane_dirs(cfg)[0])
    if not cursor_a:
        logger.error("Can't find JSONL for Agent A")
        return
    logger.info(f"Agent A JSONL: {cursor_a.file_path.name}")

    a_output = wait_and_extract(cursor_a, pane_target=pane_a)
    if not a_output:
        logger.error("No output from Agent A on seed")
        return

    with open(log_file, "w") as f:
        f.write("# Interpretation Chain\n\n")
        if cfg:
            f.write(f"**Chain ID:** {cfg.chain_id}\n\n")
            if cfg.project:
                f.write(f"**Project:** {cfg.project}\n\n")
        f.write(f"**Seed:** {seed}\n\n")
        f.write(f"**Reset every:** {clear_every} rounds\n\n")
        f.write("**Agent A (Builder):** Pragmatic, grounds in action\n\n")
        f.write("**Agent B (Thinker):** Patterns, connections, deeper structures\n\n")
        f.write("---\n")
        f.write(f"\n## Round 1 -- Builder\n\n{a_output}\n\n---\n")
    logger.info(f"[Round 1] Agent A: {len(a_output)} chars")

    if record:
        reg.update_chain(cfg.chain_id, current_round=1,
                         last_output_snippet=a_output[:200])

    logger.info("[Round 1] -> Agent B")
    if not inject_message(pane_b, a_output):
        logger.error("Failed to inject into Agent B")
        return

    if not wait_for_idle(pane_b):
        logger.error("Agent B timed out")
        return

    cursor_b = discover_jsonl_with_retries(pane_b, _project_pane_dirs(cfg)[1])
    if not cursor_b:
        logger.error("Can't find JSONL for Agent B")
        return
    logger.info(f"Agent B JSONL: {cursor_b.file_path.name}")

    if cursor_a.file_path == cursor_b.file_path:
        logger.error(
            f"JSONL COLLISION: both agents resolved to {cursor_a.file_path}. "
            "Dialogue would collapse to monologue. Aborting."
        )
        return

    b_output = wait_and_extract(cursor_b, pane_target=pane_b)
    if not b_output:
        logger.error("No output from Agent B")
        return

    with open(log_file, "a") as f:
        f.write(f"\n## Round 1 -- Thinker\n\n{b_output}\n\n---\n")
    logger.info(f"[Round 1] Agent B: {len(b_output)} chars")

    current = b_output
    i = 1
    rounds_since_reset = 1
    last_recap_a: str | None = None
    last_recap_b: str | None = None

    coord_project = cfg.project if (cfg and cfg.project) else None
    coord_chain_id = cfg.chain_id if cfg else ""

    try:
        with ChainCoordinatorContext(
            project_dir=coord_project,
            chain_id=coord_chain_id,
            focus=focus,
        ):
            while True:
                if not _session_alive(session):
                    logger.info(
                        f"tmux session '{session}' no longer exists — exiting relay loop."
                    )
                    break
                i += 1
                rounds_since_reset += 1

                if record:
                    reg.update_chain(cfg.chain_id, current_round=i,
                                     last_output_snippet=current[:200])

                if rounds_since_reset >= clear_every:
                    if record:
                        reg.update_chain(cfg.chain_id, status="resetting")
                    result = clear_and_recap(
                        pane_a, pane_b, cursor_a, cursor_b, log_file, i,
                        previous_recap_a=last_recap_a,
                        previous_recap_b=last_recap_b,
                        bulletin_path=bulletin_path,
                        cfg=cfg,
                    )
                    if result[0] is None or result[2] is None:
                        # Curator reset failed. The loop is infinite — a failed
                        # reset is a transient setback, not a termination signal.
                        # Keep the prior recap values and give the relay
                        # clear_every more rounds before the next reset attempt
                        # so we don't thrash on back-to-back curator failures.
                        logger.error(
                            f"[Round {i}] Context reset failed; keeping prior recap and continuing."
                        )
                        rounds_since_reset = 0
                        if record:
                            reg.update_chain(cfg.chain_id, status="running")
                        continue
                    cursor_a, cursor_b, current, last_recap_a, last_recap_b, _ = result
                    rounds_since_reset = 0
                    if record:
                        reg.update_chain(cfg.chain_id, status="running")
                    continue

                a_output = do_round(
                    pane_a, cursor_a,
                    "Agent A", current, log_file, i,
                )
                if not a_output:
                    # Empty / failed turn. Nudge the stalled pane and try
                    # again next round. `current` is deliberately not updated
                    # so Agent A re-reads the same input on the next pass.
                    _nudge_stalled_agent(pane_a, "Agent A", i)
                    continue

                b_output = do_round(
                    pane_b, cursor_b,
                    "Agent B", a_output, log_file, i,
                )
                if not b_output:
                    # Empty / failed turn. Nudge pane B and retry next round.
                    # `current` stays at its prior b_output so Agent A re-reads
                    # the same input and can produce a fresh a_output that B
                    # will see after the nudge has been digested.
                    _nudge_stalled_agent(pane_b, "Agent B", i)
                    continue

                if a_output == b_output:
                    # JSONL collision or some other pathology where both
                    # agents' panes are reading the same transcript. The
                    # loop never exits on content — force a curator reset
                    # on the next iteration so fresh cursors get a chance
                    # to disambiguate.
                    logger.warning(
                        f"[Round {i}] a_output == b_output ({len(a_output)} chars). "
                        "Possible JSONL collision or echo — forcing curator reset next round."
                    )
                    rounds_since_reset = clear_every
                    continue

                current = b_output

    except KeyboardInterrupt:
        logger.info(f"\nStopped after {i} rounds. Log: {log_file}")
        logger.info(f"Sessions still alive: tmux attach -t {session}")
    finally:
        if record:
            reg.unregister_chain(cfg.chain_id)


# --- CLI commands ---

def _format_project(project: str | None, width: int = 30) -> str:
    """Render a project path for the list view.

    Multi-chain-on-same-project makes the path the discriminator between
    chains; keeping the tail (basename + parent) visible is what matters,
    so long paths are left-truncated with an ellipsis.
    """
    if not project:
        return "-"
    if len(project) <= width:
        return project
    return "..." + project[-(width - 3):]


def cmd_list():
    """List all registered chains."""
    reg.cleanup_dead_chains()
    chains = reg.list_chains()
    if not chains:
        print("No chains registered.")
        return
    print(
        f"{'ID':<16} {'Session':<16} {'Status':<10} {'Round':<6} "
        f"{'Project':<30} {'Seed':<40}"
    )
    print("-" * 122)
    for c in chains:
        seed_short = (c.seed[:37] + "...") if len(c.seed) > 40 else c.seed
        project_short = _format_project(c.project)
        print(
            f"{c.chain_id:<16} {c.session:<16} {c.status:<10} "
            f"{c.current_round:<6} {project_short:<30} {seed_short:<40}"
        )


def cmd_stop(chain_id: str):
    """Stop a chain by ID."""
    if reg.stop_chain(chain_id):
        print(f"Stopped chain {chain_id}")
    else:
        print(f"Chain {chain_id} not found")
        sys.exit(1)


def cmd_attach(chain_id: str):
    """Attach the current terminal to a chain's tmux session.

    Replaces the current process with `tmux attach -t <session>` via execvp,
    so the user's terminal becomes tmux. Returns (and exits 1) only on the
    lookup / exec failure paths.
    """
    record = reg.get_chain(chain_id)
    if not record:
        print(f"Chain {chain_id} not found")
        sys.exit(1)
    if record.status == "dead":
        print(
            f"Warning: chain {chain_id} is marked dead; "
            f"its tmux session may no longer exist.",
            file=sys.stderr,
        )
    try:
        os.execvp("tmux", ["tmux", "attach", "-t", record.session])
    except FileNotFoundError:
        print("tmux not found on PATH; cannot attach.", file=sys.stderr)
        sys.exit(1)


def _role_preview(path: Path, max_len: int = 100) -> str:
    """First non-empty line of a role file, truncated with an ellipsis.

    Used by ``cmd_list_roles``. Returns "(empty)" when the file has no
    non-blank lines so an accidentally-empty role shows up in the listing
    instead of silently disappearing.
    """
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            if len(line) > max_len:
                return line[: max_len - 3] + "..."
            return line
    return "(empty)"


def cmd_list_roles():
    """Print every role file under ``roles/`` with a first-line preview.

    Surfaces the palette so operators can pick a pair for ``--role-a`` and
    ``--role-b`` without shelling into the repo. Output is one row per
    role: filename (the exact string the CLI flags accept) followed by the
    role's first non-empty line, which in our role files is the character
    sentence and the most useful single-glance summary.
    """
    if not ROLES_DIR.is_dir():
        print(f"Roles directory missing: {ROLES_DIR}")
        sys.exit(1)
    paths = sorted(ROLES_DIR.glob("*.txt"))
    if not paths:
        print(f"No role files in {ROLES_DIR}")
        return
    name_width = max(len(p.name) for p in paths)
    for p in paths:
        try:
            preview = _role_preview(p)
        except OSError as e:
            preview = f"(read error: {e})"
        print(f"{p.name:<{name_width}}  {preview}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Interpretation chain with janitor-powered context resets"
    )
    parser.add_argument(
        "seed", nargs="?", default=None,
        help="The opening prompt sent to Agent A. Required unless using "
             "--list / --stop / --attach.",
    )
    parser.add_argument("--role-a", default="builder.txt",
                        help="Role file for Agent A (in roles/)")
    parser.add_argument("--role-b", default="thinker.txt",
                        help="Role file for Agent B (in roles/)")
    parser.add_argument("--project", type=Path, default=None,
                        help="Target project directory the agents work inside "
                             "(default: current working directory).")
    parser.add_argument("--session", default=None,
                        help="Tmux session name (default: auto-generated from chain ID)")
    parser.add_argument("--max-chains", type=int, default=5,
                        help="Max concurrent chains per project (default: 5)")
    parser.add_argument("--focus", default="",
                        help="Free-text focus area for this chain pair, e.g. "
                             "'backend api' or 'tests'. Visible to other "
                             "chains on the same project via "
                             "project_coordinator, so multi-chain runs can "
                             "divide work without stepping on each other.")
    parser.add_argument("--list", action="store_true",
                        help="List all registered chains")
    parser.add_argument("--list-roles", action="store_true",
                        help="List available role files under roles/ with a "
                             "first-line preview of each. The filenames "
                             "printed here are the exact strings --role-a / "
                             "--role-b accept.")
    parser.add_argument("--stop", metavar="CHAIN_ID",
                        help="Stop a chain by ID")
    parser.add_argument("--attach", metavar="CHAIN_ID",
                        help="Attach your terminal to a chain's tmux session")

    args = parser.parse_args()

    if args.list:
        cmd_list()
        sys.exit(0)
    if args.list_roles:
        cmd_list_roles()
        sys.exit(0)
    if args.stop:
        cmd_stop(args.stop)
        sys.exit(0)
    if args.attach:
        cmd_attach(args.attach)
        sys.exit(0)

    if not args.seed:
        parser.error(
            "seed is required. Pass the opening prompt as a positional argument, "
            "e.g. python3 chain.py \"design a tic-tac-toe AI\" --project /path/to/project"
        )

    # Project is now mandatory: every chain runs inside a project with tool
    # access. If the user didn't pass --project, default to the current
    # working directory so the launch path always resolves to something real.
    project_path = (args.project or Path.cwd()).resolve()
    if not project_path.is_dir():
        logger.error(f"Project directory not found: {project_path}")
        raise SystemExit(1)
    _reject_project_inside_repo(project_path)
    project_str = str(project_path)

    active = reg.count_active_chains(project=project_str)
    if active >= args.max_chains:
        logger.error(
            f"Already {active} active chain(s) (max: {args.max_chains}). "
            f"Use --list to see them, --stop to kill one, or --max-chains to raise limit."
        )
        sys.exit(1)

    _preflight_binaries()

    chain_id = reg.generate_chain_id()
    session_name = args.session or f"chain-{chain_id}"

    cfg = reg.ChainConfig(
        chain_id=chain_id,
        session=session_name,
        seed=args.seed,
        role_a=args.role_a,
        role_b=args.role_b,
        project=project_str,
    )

    # Plain-stdout launch card: the one thing a user needs after launching is
    # the exact command to see the agents (and the one to stop them). The
    # chain id otherwise only appears inside INFO log lines.
    print(
        f"\nChain {chain_id} launching.\n"
        f"  watch:  tmux attach -t {session_name}    (run this in another terminal)\n"
        f"  stop:   Ctrl+C here, or: python3 chain.py --stop {chain_id}\n"
        f"This terminal runs the relay loop — leave it open.\n",
        flush=True,
    )

    SESSION = session_name
    WS_A = WORKSPACE / SESSION / "a"
    WS_B = WORKSPACE / SESSION / "b"
    PROJECT_DIR = project_path

    run_chain(args.seed, load_role(args.role_a), load_role(args.role_b),
              cfg=cfg, focus=args.focus)
