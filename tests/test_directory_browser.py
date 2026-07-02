"""Tests for ui/directory_browser.py — directory picker backend.

Everything runs against tmp_path (real filesystem) so path-handling edges
(symlinks, permission denial, non-existent parents) are exercised for real.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = str(Path(__file__).parent.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from ui import directory_browser as db


# ---------- is_safe_path / validate_project_dir ----------


def test_is_safe_path_rejects_known_system_dirs():
    for bad in ("/", "/etc", "/bin", "/sbin", "/System", "/private/etc", "/dev"):
        assert not db.is_safe_path(bad), f"{bad} should be refused"


def test_is_safe_path_rejects_children_of_system_dirs():
    """Subpaths of refused dirs are refused too — e.g. /etc/myproject."""
    assert not db.is_safe_path("/etc/something")
    assert not db.is_safe_path("/bin/anything")


def test_is_safe_path_accepts_usr_local_and_var_folders():
    """/usr/local and /var are not refused — users legitimately work there."""
    # We don't require these to exist; is_safe_path is a string-boundary
    # check, so the function should say "safe" for these shapes.
    assert db.is_safe_path("/usr/local/myproj")
    assert db.is_safe_path("/var/folders/x/y/z")


def test_is_safe_path_accepts_home_and_tmp(tmp_path):
    home = str(Path.home())
    assert db.is_safe_path(home)
    assert db.is_safe_path(str(tmp_path))


def test_is_safe_path_expands_tilde():
    """`~/foo` should be treated the same as its expanded form."""
    assert db.is_safe_path("~")


def test_validate_empty_path():
    ok, reason = db.validate_project_dir("")
    assert ok is False
    assert "required" in reason.lower()


def test_validate_nonexistent(tmp_path):
    ok, reason = db.validate_project_dir(tmp_path / "does-not-exist")
    assert ok is False
    assert "no such" in reason.lower()


def test_validate_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hello")
    ok, reason = db.validate_project_dir(f)
    assert ok is False
    assert "not a directory" in reason.lower()


def test_validate_refuses_system_path():
    ok, reason = db.validate_project_dir("/etc")
    assert ok is False
    assert "system path" in reason.lower()


def test_validate_accepts_real_directory(tmp_path):
    ok, reason = db.validate_project_dir(tmp_path)
    assert ok is True
    assert reason == ""


def test_validate_unreadable_directory(tmp_path):
    """A 000-mode directory should be refused with a readable reason."""
    d = tmp_path / "locked"
    d.mkdir()
    os.chmod(d, 0o000)
    try:
        ok, reason = db.validate_project_dir(d)
        assert ok is False
        assert "readable" in reason.lower()
    finally:
        os.chmod(d, 0o755)


# ---------- browse ----------


def test_browse_lists_directories_only_by_default(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("x")
    result = db.browse(tmp_path)
    assert result["error"] is None
    names = [e["name"] for e in result["entries"]]
    assert "src" in names and "docs" in names
    assert "README.md" not in names


def test_browse_includes_files_when_requested(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x")
    result = db.browse(tmp_path, include_files=True)
    names = [e["name"] for e in result["entries"]]
    assert "src" in names and "README.md" in names


def test_browse_hides_dotfiles_by_default(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible").mkdir()
    result = db.browse(tmp_path)
    names = [e["name"] for e in result["entries"]]
    assert ".hidden" not in names
    assert "visible" in names


def test_browse_shows_dotfiles_when_requested(tmp_path):
    (tmp_path / ".hidden").mkdir()
    result = db.browse(tmp_path, include_hidden=True)
    names = [e["name"] for e in result["entries"]]
    assert ".hidden" in names


def test_browse_marks_project_roots(tmp_path):
    """A directory containing .git/ is flagged is_project=True."""
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".git").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()

    result = db.browse(tmp_path)
    entries_by_name = {e["name"]: e for e in result["entries"]}
    assert entries_by_name["myproj"]["is_project"] is True
    assert entries_by_name["plain"]["is_project"] is False


def test_browse_project_markers_variety(tmp_path):
    """Any of several markers is enough to flag a directory as a project."""
    for marker in ("pyproject.toml", "package.json", "Cargo.toml", "README.md"):
        d = tmp_path / f"p-{marker}"
        d.mkdir()
        (d / marker).write_text("x")

    result = db.browse(tmp_path)
    for e in result["entries"]:
        assert e["is_project"] is True, f"{e['name']} should be flagged as project"


def test_browse_sort_projects_first_then_alpha(tmp_path):
    # plain (not a project) + zproj (project via .git) — zproj should come first.
    plain = tmp_path / "aaa-plain"
    zproj = tmp_path / "zzz-proj"
    plain.mkdir()
    zproj.mkdir()
    (zproj / ".git").mkdir()

    result = db.browse(tmp_path)
    names = [e["name"] for e in result["entries"]]
    assert names == ["zzz-proj", "aaa-plain"]


def test_browse_nonexistent_path():
    result = db.browse("/definitely/does/not/exist/xyz")
    assert result["entries"] == []
    assert "no such" in result["error"].lower()


def test_browse_file_instead_of_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    result = db.browse(f)
    assert result["entries"] == []
    assert "not a directory" in result["error"].lower()


def test_browse_max_entries_truncates(tmp_path):
    for i in range(30):
        (tmp_path / f"d{i:02d}").mkdir()
    result = db.browse(tmp_path, max_entries=10)
    assert len(result["entries"]) == 10
    assert result["truncated"] is True


def test_browse_not_truncated_under_limit(tmp_path):
    (tmp_path / "a").mkdir()
    result = db.browse(tmp_path, max_entries=10)
    assert result["truncated"] is False


def test_browse_reports_parent_path(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    result = db.browse(child)
    assert result["parent"] == str(tmp_path)


def test_browse_parent_is_none_at_filesystem_root():
    """Root has no navigable parent; `parent` is None, not a dup of path."""
    result = db.browse("/")
    assert result["parent"] is None


def test_browse_permission_denied(tmp_path):
    """A 000-mode directory reports a permission error, empty entries."""
    d = tmp_path / "locked"
    d.mkdir()
    (d / "inside").mkdir()  # add content so we're sure iterdir has work
    os.chmod(d, 0o000)
    try:
        result = db.browse(d)
        assert result["entries"] == []
        assert result["error"] is not None
        assert "permission" in result["error"].lower() or "could not read" in result["error"].lower()
    finally:
        os.chmod(d, 0o755)


def test_browse_expands_tilde():
    """~/... paths are expanded before browsing."""
    result = db.browse("~")
    # Home should exist and be readable on any dev machine.
    assert result["error"] is None
    assert result["path"] == str(Path.home())


def test_browse_ignores_broken_symlinks(tmp_path):
    """A broken symlink in the listing should be skipped, not blow up."""
    target = tmp_path / "target"
    target.mkdir()
    broken = tmp_path / "broken-link"
    try:
        broken.symlink_to(tmp_path / "missing")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    result = db.browse(tmp_path)
    names = [e["name"] for e in result["entries"]]
    assert "target" in names
    assert "broken-link" not in names  # skipped, not promoted/demoted


# ---------- suggestions ----------


def test_suggestions_includes_home():
    assert str(Path.home()) in db.suggestions()


def test_suggestions_preserves_order_and_dedupes(tmp_path):
    extra = [tmp_path, tmp_path, str(Path.home())]
    out = db.suggestions(extra=extra)
    # tmp_path first (passed), home second.
    assert out[0] == str(tmp_path)
    # No duplicate of home even though home appears in extras.
    assert out.count(str(Path.home())) == 1


def test_suggestions_filters_nonexistent(tmp_path):
    """Dead paths drop out so the picker never shows stale anchors."""
    missing = tmp_path / "vanished"
    out = db.suggestions(extra=[missing])
    assert str(missing) not in out
