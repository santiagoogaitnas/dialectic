"""Tests for JSONL reader — cursor, slug, and incremental reads."""

import json
import tempfile
from pathlib import Path

from janitor.jsonl_reader import JSONLCursor, read_new_entries, slugify_project_path


def test_slugify_standard_path():
    assert slugify_project_path(Path("/home/user/my/project")) == "-home-user-my-project"


def test_slugify_path_with_spaces():
    assert slugify_project_path(Path("/home/user/my project")) == "-home-user-my-project"


def test_slugify_already_has_leading_dash():
    result = slugify_project_path(Path("/foo"))
    assert result.startswith("-")
    assert result == "-foo"


def test_cursor_has_new_data_empty_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path, byte_offset=0)
        assert cursor.has_new_data() is False
    finally:
        path.unlink()


def test_cursor_has_new_data_with_content():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"type": "user"}\n')
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path, byte_offset=0)
        assert cursor.has_new_data() is True
    finally:
        path.unlink()


def test_cursor_has_new_data_caught_up():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        content = '{"type": "user"}\n'
        f.write(content)
        path = Path(f.name)
    try:
        cursor = JSONLCursor(
            file_path=path, byte_offset=len(content.encode("utf-8"))
        )
        assert cursor.has_new_data() is False
    finally:
        path.unlink()


def test_cursor_missing_file():
    cursor = JSONLCursor(file_path=Path("/nonexistent/file.jsonl"))
    assert cursor.has_new_data() is False


def test_read_new_entries_basic():
    entries = [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path)
        result = read_new_entries(cursor)
        assert len(result) == 2
        assert result[0]["type"] == "user"
        assert result[1]["type"] == "assistant"
        assert cursor.byte_offset > 0
    finally:
        path.unlink()


def test_read_new_entries_incremental():
    """Reading twice: first read gets entries, second read gets nothing."""
    entry = {"type": "user", "message": {"content": "hello"}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(entry) + "\n")
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path)
        first = read_new_entries(cursor)
        assert len(first) == 1

        second = read_new_entries(cursor)
        assert len(second) == 0
    finally:
        path.unlink()


def test_read_new_entries_appended_data():
    """Simulate a growing file: read, append, read again."""
    entry1 = {"type": "user", "message": {"content": "first"}}
    entry2 = {"type": "user", "message": {"content": "second"}}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(entry1) + "\n")
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path)
        first = read_new_entries(cursor)
        assert len(first) == 1

        with open(path, "a") as f:
            f.write(json.dumps(entry2) + "\n")

        second = read_new_entries(cursor)
        assert len(second) == 1
        assert second[0]["message"]["content"] == "second"
    finally:
        path.unlink()


def test_read_new_entries_handles_truncation():
    """If file shrinks (new session), cursor resets."""
    entry = {"type": "user", "message": {"content": "hello"}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(entry) + "\n")
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path, byte_offset=99999)
        result = read_new_entries(cursor)
        assert len(result) == 1
        assert cursor.byte_offset > 0
    finally:
        path.unlink()


def test_read_new_entries_skips_malformed():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"type": "user"}\n')
        f.write("this is not json\n")
        f.write('{"type": "assistant"}\n')
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path)
        result = read_new_entries(cursor)
        assert len(result) == 2
    finally:
        path.unlink()


def test_read_new_entries_partial_line():
    """Incomplete last line should not be read."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"type": "complete"}\n')
        f.write('{"type": "incompl')
        path = Path(f.name)
    try:
        cursor = JSONLCursor(file_path=path)
        result = read_new_entries(cursor)
        assert len(result) == 1
        assert result[0]["type"] == "complete"
    finally:
        path.unlink()


def test_read_new_entries_missing_file():
    cursor = JSONLCursor(file_path=Path("/nonexistent.jsonl"))
    result = read_new_entries(cursor)
    assert result == []


# --- slug edge cases the production pane paths actually hit ---


def test_slugify_leading_dot_dir():
    """Per-chain pane dirs are `.dialectic-a-<chain_id>`; the leading dot on
    the last path component must be flattened to a dash so that the dot
    stacks on top of the '/' separator's dash, producing a `--dialectic`
    double-dash segment. ~/.claude/projects/ stores the JSONL under exactly
    this slug, so a regression where dots stop being replaced would silently
    break JSONL discovery for every project-mode chain.
    """
    assert (
        slugify_project_path(Path("/Users/alice/myproj/.dialectic-a-04161200-abcd"))
        == "-Users-alice-myproj--dialectic-a-04161200-abcd"
    )


def test_slugify_underscore_replaced():
    """Underscores flatten to dashes. Matches Claude Code's slug rule so
    `/home/user/my_project` → `-home-user-my-project` (not `-home-user-my_project`).
    """
    assert (
        slugify_project_path(Path("/home/user/my_project"))
        == "-home-user-my-project"
    )


def test_slugify_dot_and_underscore_together():
    """Path containing both dots (`v1.2`) and underscores (`sub_dir`) should
    flatten both to dashes in the same slug. Regression guard: if the
    replacement order changed (say, underscores last), the intermediate
    slug would still need to pick up underscores — this pins the whole
    compound behavior in a single assertion.
    """
    assert (
        slugify_project_path(Path("/home/user/v1.2/sub_dir"))
        == "-home-user-v1-2-sub-dir"
    )


def test_slugify_matches_claude_projects_dir_convention(tmp_path):
    """The slug must resolve to an actual directory under ~/.claude/projects
    when Claude Code creates one for a pane's cwd. We can't touch the real
    home dir, but we can prove that the slug is a valid single-segment
    directory name (no '/' sneaking through) so it's usable as a subdir.
    """
    paths = [
        Path("/Users/alice/projects/sample/sample-production"),
        Path("/home/a/b/.dialectic-b-04161200-abcd"),
        Path("/tmp/dialectic test/project"),
    ]
    for p in paths:
        slug = slugify_project_path(p)
        assert "/" not in slug, f"slug {slug!r} still contains /"
        assert " " not in slug, f"slug {slug!r} still contains space"
        assert slug.startswith("-"), f"slug {slug!r} missing leading dash"
        # Must be usable as an actual subdirectory name:
        subdir = tmp_path / slug
        subdir.mkdir()
        assert subdir.is_dir()


