"""Safe directory browsing for the UI project-directory picker.

The dashboard's "Project directory" field is the most important input a user
provides — it's where the agents will read and write files. Making users
paste an absolute path by hand is how they mistype into nowhere. This
module backs a folder-picker UI: list the contents of a directory, mark
entries that look like project roots (have `.git/`, a common manifest, or
similar), and answer "is this path safe to point agents at?"

Scope
-----

- **Browse**: return directory entries (dirs only by default) with a small
  set of project-shape hints so the picker can highlight good candidates.
- **Validate**: called when the user submits a path — does it exist, is it
  a directory, is it not a system path we should refuse to hand to an
  agent? Returns `(ok, reason)`.
- **Suggestions**: small helper to seed the picker with common starting
  points (home, current project ancestor, etc).

What this module does NOT do
----------------------------

- It doesn't enforce a jail. A power user running this locally should be
  able to pick any directory they own. We refuse only a small, explicitly
  dangerous list (root of the filesystem, `/etc`, `/private/etc`, etc) to
  prevent a one-click disaster, not to sandbox the tool.
- It doesn't invoke tmux, launch chains, or write files. Read-only by
  design — mutation is the caller's responsibility.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Directories an operator would never want to hand to a dialectic chain.
# Selecting them in the picker should return (False, reason) from
# validate_project_dir() rather than being silently accepted. This isn't
# a security boundary — a determined user can type the path directly into
# the CLI — it's a foot-gun guard on the happy-path UI submit flow.
#
# Intentionally narrow. Paths like /var (macOS tmp lives under /var/folders),
# /usr/local (homebrew), /Library (app data) are NOT in this list: users
# legitimately have code there. We only refuse paths whose selection would
# be almost-certainly a mistake.
_REFUSED_PROJECT_DIRS: frozenset[str] = frozenset({
    "/",
    "/bin", "/sbin",
    "/etc", "/private/etc",
    "/System",
    "/dev", "/proc", "/sys",
    "/boot",
})

# Markers that hint "this is a project root." Used only to sort/highlight in
# the picker; they do NOT affect validation — picking a non-project directory
# is still allowed, it just won't get the badge.
_PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "requirements.txt",
    "Gemfile",
    "composer.json",
    "CLAUDE.md",
    "README.md",
)

MAX_ENTRIES_DEFAULT = 500


@dataclass(frozen=True)
class DirEntry:
    """One row in a directory listing."""

    name: str
    path: str                 # absolute path
    is_dir: bool
    is_project: bool          # has a marker file/dir inside
    is_hidden: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "is_project": self.is_project,
            "is_hidden": self.is_hidden,
        }


def _expand(path: str | Path) -> Path:
    """User-entered paths often contain `~` — expand before inspecting."""
    return Path(os.path.expanduser(str(path))).resolve()


def is_safe_path(path: str | Path) -> bool:
    """True when `path` is safely project-assignable per our refused list.

    Safe in this context means "not on the refused list and not a child of
    a refused directory" — e.g. `/etc/mything` is still refused because any
    dialectic edit there would be disastrous. Everything inside the user's
    home is always allowed even though `/Users` might look system-y.
    """
    p = _expand(path)
    p_str = str(p)
    if p_str in _REFUSED_PROJECT_DIRS:
        return False
    for refused in _REFUSED_PROJECT_DIRS:
        if p_str == refused:
            return False
        # Special-case root: every path descends from "/", but we don't want
        # every path refused. Only treat non-root refused dirs as parents.
        if refused != "/" and p_str.startswith(refused + "/"):
            return False
    return True


def validate_project_dir(path: str | Path) -> tuple[bool, str]:
    """Boundary check for a submitted project directory.

    Returns (True, "") on acceptance, or (False, reason) where `reason` is a
    human-readable explanation the picker can show inline. We check in order
    most-specific → least so the reason matches what the user sees wrong:
    missing path before unsafe, unsafe before not-a-directory.
    """
    if not path or not str(path).strip():
        return False, "Project directory is required."
    p = _expand(path)
    if not p.exists():
        return False, f"No such directory: {p}"
    if not p.is_dir():
        return False, f"Not a directory: {p}"
    if not is_safe_path(p):
        return False, (
            f"Refusing system path: {p}. Pick a directory inside your home."
        )
    # A readable directory is the minimum — agents need to at least ls it.
    if not os.access(str(p), os.R_OK):
        return False, f"Not readable: {p}"
    return True, ""


def _has_project_marker(p: Path) -> bool:
    """Fast check: does `p` contain any marker that looks like a project root?

    Uses direct path existence rather than iterdir() because this runs once
    per entry in a listing — O(len(_PROJECT_MARKERS)) stats vs one readdir
    is usually cheaper and always bounded.
    """
    for marker in _PROJECT_MARKERS:
        if (p / marker).exists():
            return True
    return False


def browse(
    path: str | Path,
    include_hidden: bool = False,
    include_files: bool = False,
    max_entries: int = MAX_ENTRIES_DEFAULT,
) -> dict:
    """List the contents of a directory in picker-friendly shape.

    Return shape:
        {"path": "/abs/expanded/path",
         "parent": "/abs/parent",   # None at filesystem root
         "entries": [DirEntry.to_dict()...],
         "truncated": bool,         # True if we stopped at max_entries
         "error": None | "message"} # set on read failure; entries stays []

    `include_hidden` defaults False because a picker is for finding projects,
    not for exploring dotfiles. `include_files` likewise False — pickers
    show folders. Both are opt-in for flexibility.

    `max_entries` caps the payload so a `browse('/tmp')` on a machine with
    tens of thousands of tmpfiles doesn't stall the UI. The `truncated`
    flag tells the caller to prompt the user to narrow the search.
    """
    try:
        p = _expand(path)
    except (OSError, ValueError) as e:
        return {"path": str(path), "parent": None, "entries": [],
                "truncated": False, "error": f"Invalid path: {e}"}

    if not p.exists():
        return {"path": str(p), "parent": None, "entries": [],
                "truncated": False, "error": f"No such directory: {p}"}
    if not p.is_dir():
        return {"path": str(p), "parent": str(p.parent), "entries": [],
                "truncated": False, "error": f"Not a directory: {p}"}

    parent = str(p.parent) if p.parent != p else None

    entries: list[DirEntry] = []
    truncated = False
    try:
        for child in p.iterdir():
            if len(entries) >= max_entries:
                truncated = True
                break
            name = child.name
            is_hidden = name.startswith(".")
            if is_hidden and not include_hidden:
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                # Broken symlink, permission denied — skip silently rather
                # than blowing up the whole listing.
                continue
            if not is_dir and not include_files:
                continue
            is_project = is_dir and _has_project_marker(child)
            entries.append(DirEntry(
                name=name,
                path=str(child.resolve()),
                is_dir=is_dir,
                is_project=is_project,
                is_hidden=is_hidden,
            ))
    except PermissionError as e:
        return {"path": str(p), "parent": parent, "entries": [],
                "truncated": False, "error": f"Permission denied: {e}"}
    except OSError as e:
        return {"path": str(p), "parent": parent, "entries": [],
                "truncated": False, "error": f"Could not read: {e}"}

    # Sort: project roots first, then directories, then files, alphabetical within.
    entries.sort(key=lambda e: (
        not e.is_project,
        not e.is_dir,
        e.name.lower(),
    ))

    return {
        "path": str(p),
        "parent": parent,
        "entries": [e.to_dict() for e in entries],
        "truncated": truncated,
        "error": None,
    }


def suggestions(extra: Optional[Iterable[str | Path]] = None) -> list[str]:
    """Seed paths to show as starting points in the picker.

    Home directory is always first (most users will want to browse from
    there). Callers can pass `extra` to prepend context-specific anchors,
    e.g. the current project's parent so users can pick a sibling.
    Non-existent suggestions are filtered out so the picker never shows a
    dead link.
    """
    candidates: list[str] = []
    if extra:
        for item in extra:
            candidates.append(str(_expand(item)))
    home = str(Path.home())
    if home not in candidates:
        candidates.append(home)
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if Path(c).exists():
            out.append(c)
    return out
