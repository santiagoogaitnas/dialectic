"""JSONL file discovery and incremental reading.

Discovers JSONL conversation logs for Claude Code sessions running in tmux
panes, and reads new entries incrementally using byte-offset cursors.

JSONL path pattern: ~/.claude/projects/{slugified-project-path}/{session-uuid}.jsonl
Slug computation: /home/user/my/project → -home-user-my-project (/ and spaces become -)
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("janitor.jsonl")


@dataclass
class JSONLCursor:
    """Tracks read position in a JSONL file.

    The cursor advances forward only. If the file is truncated (new session
    after /clear), the cursor resets to 0.
    """

    file_path: Path
    byte_offset: int = 0
    last_mtime: float = 0.0

    def has_new_data(self) -> bool:
        """Check if the JSONL file has grown since last read."""
        try:
            stat = self.file_path.stat()
            return stat.st_size > self.byte_offset
        except FileNotFoundError:
            return False


def slugify_project_path(project_path: Path) -> str:
    """Convert a project path to Claude Code's slug format.

    /Users/alice/my project → -Users-alice-my-project
    Replaces / and spaces with -, ensures leading -.
    """
    slug = str(project_path).replace("/", "-").replace(" ", "-").replace("_", "-").replace(".", "-")
    if not slug.startswith("-"):
        slug = "-" + slug
    return slug


def discover_jsonl_for_pane(
    pane_target: str, project_path: Path
) -> Optional[Path]:
    """Find the JSONL file for the Claude Code session in a tmux pane.

    Strategy 1: PID-based discovery via lsof (precise but may fail).
    Strategy 2: Most recently modified .jsonl file in the project slug dir.

    Args:
        pane_target: tmux pane identifier (e.g., "chain-04161119-9733:0.0")
        project_path: project root directory

    Returns:
        Path to the JSONL file, or None if not found.
    """
    slug = slugify_project_path(project_path)
    jsonl_dir = Path.home() / ".claude" / "projects" / slug

    if not jsonl_dir.exists():
        logger.warning(f"JSONL directory not found: {jsonl_dir}")
        return None

    # Strategy 1: PID-based lsof discovery
    jsonl_path = _discover_via_lsof(pane_target, jsonl_dir)
    if jsonl_path:
        return jsonl_path

    # Strategy 2: Most recently modified file
    return _discover_via_mtime(jsonl_dir)


def _get_all_descendants(pid: str) -> list[str]:
    """Recursively find all descendant PIDs of a process."""
    descendants = []
    try:
        children = subprocess.run(
            ["pgrep", "-P", pid],
            capture_output=True, text=True, timeout=5,
        )
        for child in children.stdout.strip().split("\n"):
            child = child.strip()
            if child:
                descendants.append(child)
                descendants.extend(_get_all_descendants(child))
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass
    return descendants


def _discover_via_lsof(pane_target: str, jsonl_dir: Path) -> Optional[Path]:
    """Find JSONL file by tracing the process in the tmux pane."""
    try:
        # Get PID of the process in the pane
        pid_result = subprocess.run(
            ["tmux", "list-panes", "-t", pane_target, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if pid_result.returncode != 0:
            return None

        pane_pid = pid_result.stdout.strip()
        if not pane_pid:
            return None

        # Walk the full process tree (Claude Code nests several levels deep)
        all_pids = _get_all_descendants(pane_pid)

        for pid in all_pids:
            try:
                lsof_result = subprocess.run(
                    ["lsof", "-p", pid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in lsof_result.stdout.split("\n"):
                    if ".jsonl" in line and str(jsonl_dir) in line:
                        parts = line.split()
                        for part in parts:
                            if part.endswith(".jsonl") and str(jsonl_dir) in part:
                                candidate = Path(part)
                                if candidate.exists():
                                    return candidate
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                continue
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass

    return None


def _discover_via_mtime(jsonl_dir: Path) -> Optional[Path]:
    """Find the most recently modified JSONL file in the directory."""
    jsonl_files = sorted(
        jsonl_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not jsonl_files:
        logger.warning(f"No JSONL files in {jsonl_dir}")
        return None
    return jsonl_files[0]


def read_new_entries(cursor: JSONLCursor) -> list[dict]:
    """Read new JSONL entries since the cursor's last position.

    Only processes complete lines (up to last newline). Handles:
    - Partial writes (waits for complete line)
    - File truncation/rotation (resets cursor)
    - Malformed JSON (skips with debug log)

    Updates cursor.byte_offset and cursor.last_mtime in place.
    """
    if not cursor.file_path.exists():
        return []

    try:
        file_size = cursor.file_path.stat().st_size
    except OSError:
        return []

    # File truncated or rotated — reset cursor
    if file_size < cursor.byte_offset:
        logger.info(f"JSONL file truncated/rotated, resetting cursor: {cursor.file_path}")
        cursor.byte_offset = 0

    if file_size <= cursor.byte_offset:
        return []

    entries = []
    with open(cursor.file_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(cursor.byte_offset)
        raw = f.read()

    # Only process up to the last complete line
    last_newline = raw.rfind("\n")
    if last_newline == -1:
        return []  # No complete lines yet

    complete = raw[: last_newline + 1]
    cursor.byte_offset += len(complete.encode("utf-8"))
    cursor.last_mtime = cursor.file_path.stat().st_mtime

    for line in complete.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            entries.append(entry)
        except json.JSONDecodeError:
            logger.debug(f"Skipping malformed JSONL line: {line[:100]}")

    return entries
