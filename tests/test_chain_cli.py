"""Tests for chain.py CLI commands: cmd_list and cmd_stop.

These cover the argparse entry points for --list and --stop:
- cmd_list empty-state message, header rendering, seed truncation
- cmd_list calls cleanup_dead_chains before listing (so dead PIDs surface)
- cmd_stop success and not-found paths (exit code 1 on not-found)
"""

import os
from unittest.mock import patch

import pytest

import chain
import registry as reg
from registry import ChainConfig


def _isolate_registry(tmp_path):
    reg_file = tmp_path / ".registry.json"
    return patch.object(reg, "REGISTRY_FILE", reg_file), \
           patch.object(reg, "WORKSPACE", tmp_path)


def test_cmd_list_empty(tmp_path, capsys):
    """With no chains registered, print a clear empty-state line."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        chain.cmd_list()
    out = capsys.readouterr().out
    assert "No chains registered." in out


def test_cmd_list_populated(tmp_path, capsys):
    """Registered chains should appear with id, session, status, and round."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(
            chain_id="c-abc", session="sess-abc", seed="short seed",
        )
        reg.register_chain(cfg, os.getpid())
        reg.update_chain("c-abc", current_round=7)
        chain.cmd_list()

    out = capsys.readouterr().out
    assert "ID" in out and "Session" in out and "Status" in out
    assert "c-abc" in out
    assert "sess-abc" in out
    assert "running" in out
    assert "7" in out
    assert "short seed" in out


def test_cmd_list_truncates_long_seeds(tmp_path, capsys):
    """Seeds longer than 40 chars should be shortened with an ellipsis."""
    long_seed = "x" * 100
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-long", session="s-long", seed=long_seed)
        reg.register_chain(cfg, os.getpid())
        chain.cmd_list()

    out = capsys.readouterr().out
    assert "..." in out
    # Full seed should NOT appear verbatim (truncated form is 37 x's + "...")
    assert long_seed not in out
    assert "x" * 37 + "..." in out


def test_cmd_list_short_seed_not_truncated(tmp_path, capsys):
    """A seed of exactly 40 chars stays intact (boundary: len > 40 → truncate)."""
    seed_40 = "a" * 40
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-40", session="s-40", seed=seed_40)
        reg.register_chain(cfg, os.getpid())
        chain.cmd_list()

    out = capsys.readouterr().out
    assert seed_40 in out


def test_cmd_list_calls_cleanup_first(tmp_path, capsys):
    """cmd_list runs cleanup_dead_chains so a dead PID is reported as 'dead'."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        # Register with an impossible PID → cleanup should mark it dead
        cfg = ChainConfig(chain_id="c-dead", session="s-dead", seed="t")
        reg.register_chain(cfg, 999_999_999)
        chain.cmd_list()

    out = capsys.readouterr().out
    assert "c-dead" in out
    assert "dead" in out


def test_cmd_stop_success(tmp_path, capsys):
    """A running chain gets stopped and a confirmation is printed."""
    p1, p2 = _isolate_registry(tmp_path)
    # subprocess is imported lazily inside stop_chain, so patch the module
    # attribute directly rather than via a dotted path on registry.
    with p1, p2, \
         patch("subprocess.run"), \
         patch("registry.os.kill"):  # SIGTERM becomes a no-op
        cfg = ChainConfig(chain_id="to-stop", session="s-stop", seed="t")
        reg.register_chain(cfg, os.getpid())
        chain.cmd_stop("to-stop")

    out = capsys.readouterr().out
    assert "Stopped chain to-stop" in out


def test_cmd_stop_not_found_exits_nonzero(tmp_path, capsys):
    """Stopping a missing chain prints not-found and exits with status 1."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        with pytest.raises(SystemExit) as exc_info:
            chain.cmd_stop("no-such-chain")

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "no-such-chain" in out
    assert "not found" in out


# --- Project column (multi-chain disambiguation) -----------------------------


def test_cmd_list_shows_project_column_header(tmp_path, capsys):
    """The header row must include 'Project' so users can scan multi-chain runs."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-h", session="s-h", seed="t")
        reg.register_chain(cfg, os.getpid())
        chain.cmd_list()

    out = capsys.readouterr().out
    assert "Project" in out


def test_cmd_list_no_project_renders_dash(tmp_path, capsys):
    """A registry row with no project (legacy data) shows '-' in the column.

    The CLI now always sets a project at launch, but old rows or directly
    constructed configs still have project=None, and the list view should
    render that gracefully instead of printing 'None' or crashing.
    """
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(chain_id="c-noproj", session="s-np", seed="t")
        reg.register_chain(cfg, os.getpid())
        chain.cmd_list()

    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if "c-noproj" in line)
    assert " - " in row


def test_cmd_list_short_project_path_shown_intact(tmp_path, capsys):
    """A project path that fits the column width is rendered without truncation."""
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(
            chain_id="c-short", session="s-short", seed="t",
            project="/tmp/myproj",
        )
        reg.register_chain(cfg, os.getpid())
        chain.cmd_list()

    out = capsys.readouterr().out
    assert "/tmp/myproj" in out


def test_cmd_list_long_project_path_truncated_from_left(tmp_path, capsys):
    """A long project path is left-truncated so the basename stays visible."""
    long_proj = "/very/long/nested/path/to/a/specific/widget-factory-monorepo"
    p1, p2 = _isolate_registry(tmp_path)
    with p1, p2:
        cfg = ChainConfig(
            chain_id="c-long-proj", session="s-lp", seed="t",
            project=long_proj,
        )
        reg.register_chain(cfg, os.getpid())
        chain.cmd_list()

    out = capsys.readouterr().out
    assert long_proj not in out  # full path doesn't fit
    assert "..." in out
    # The meaningful tail (basename) must survive truncation.
    assert "widget-factory-monorepo" in out


def test_cmd_list_format_project_helper_boundaries():
    """_format_project handles None / empty / boundary widths consistently."""
    f = chain._format_project
    assert f(None) == "-"
    assert f("") == "-"
    assert f("/short", width=30) == "/short"
    # Exactly width chars: no truncation.
    s30 = "/" + ("x" * 29)
    assert f(s30, width=30) == s30
    # One char over width: truncated.
    s31 = "/" + ("x" * 30)
    out = f(s31, width=30)
    assert out.startswith("...")
    assert len(out) == 30
    # Verify the *tail* is preserved (left-truncation, not right).
    assert out.endswith("x" * 27)


# --- --list-roles (role palette) ---------------------------------------------


def test_role_preview_returns_first_non_empty_line(tmp_path):
    """_role_preview skips leading blank/whitespace lines and returns the first real line."""
    path = tmp_path / "role.txt"
    path.write_text("\n   \n\nYou are the first real line.\nsecond line\n", encoding="utf-8")
    assert chain._role_preview(path) == "You are the first real line."


def test_role_preview_empty_file_returns_placeholder(tmp_path):
    """_role_preview on an empty (or whitespace-only) file returns '(empty)'."""
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert chain._role_preview(empty) == "(empty)"

    blanks = tmp_path / "blanks.txt"
    blanks.write_text("\n  \n\t\n", encoding="utf-8")
    assert chain._role_preview(blanks) == "(empty)"


def test_role_preview_truncates_long_lines_with_ellipsis(tmp_path):
    """A line past the preview cap gets cut with `...` — matches cmd_list's style."""
    path = tmp_path / "long.txt"
    path.write_text("x" * 500 + "\n", encoding="utf-8")
    out = chain._role_preview(path, max_len=30)
    assert len(out) == 30
    assert out.endswith("...")
    assert out.startswith("x")


def test_cmd_list_roles_default_includes_builder_and_thinker(capsys):
    """Default ROLES_DIR ships builder.txt + thinker.txt — the two chain defaults.

    Pinning these keeps --list-roles honest if anyone deletes or renames
    the ones wired in as `PROMPT_A = load_role("builder.txt")` etc.
    """
    chain.cmd_list_roles()

    out = capsys.readouterr().out
    assert "builder.txt" in out
    assert "thinker.txt" in out
    # Each listed role should be followed by a preview, not just the name.
    for line in out.splitlines():
        if line.startswith("builder.txt") or line.startswith("thinker.txt"):
            # Filename + two-space separator + at least one preview character.
            assert len(line.split(None, 1)) == 2, (
                f"role row has no preview: {line!r}"
            )


def test_cmd_list_roles_empty_dir_prints_message(tmp_path, capsys, monkeypatch):
    """An empty roles/ directory prints a readable empty-state line."""
    empty_dir = tmp_path / "roles"
    empty_dir.mkdir()
    monkeypatch.setattr(chain, "ROLES_DIR", empty_dir)

    chain.cmd_list_roles()

    out = capsys.readouterr().out
    assert "No role files" in out
    assert str(empty_dir) in out


def test_cmd_list_roles_missing_dir_exits_1(tmp_path, capsys, monkeypatch):
    """A missing roles/ directory exits 1 so scripts can detect the misconfig."""
    missing = tmp_path / "not-there"
    monkeypatch.setattr(chain, "ROLES_DIR", missing)

    with pytest.raises(SystemExit) as exc_info:
        chain.cmd_list_roles()
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "missing" in out.lower() or "not" in out.lower()
    assert str(missing) in out


def test_cmd_list_roles_shows_first_line_preview(tmp_path, capsys, monkeypatch):
    """Each row is `<filename>  <first-non-empty-line>` so the character reads at a glance."""
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "alpha.txt").write_text(
        "\nYou are the alpha. You argue well.\nmore body\n",
        encoding="utf-8",
    )
    (roles / "bravo.txt").write_text(
        "You are the bravo.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(chain, "ROLES_DIR", roles)

    chain.cmd_list_roles()

    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert any(
        l.startswith("alpha.txt") and "You are the alpha" in l for l in lines
    ), f"alpha row missing or wrong preview: {out!r}"
    assert any(
        l.startswith("bravo.txt") and "You are the bravo" in l for l in lines
    ), f"bravo row missing or wrong preview: {out!r}"


def test_cmd_list_roles_rows_sorted_alphabetically(tmp_path, capsys, monkeypatch):
    """Listing is alphabetical so repeated invocations produce stable output."""
    roles = tmp_path / "roles"
    roles.mkdir()
    for name in ("zeta.txt", "alpha.txt", "mid.txt"):
        (roles / name).write_text(f"You are {name}.\n", encoding="utf-8")
    monkeypatch.setattr(chain, "ROLES_DIR", roles)

    chain.cmd_list_roles()

    out = capsys.readouterr().out
    names = [
        line.split(None, 1)[0] for line in out.splitlines() if line.strip()
    ]
    assert names == ["alpha.txt", "mid.txt", "zeta.txt"]


def test_cmd_list_roles_ignores_non_txt_files(tmp_path, capsys, monkeypatch):
    """Stray files (README.md, .DS_Store) don't leak into the listing."""
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "only.txt").write_text("You are only.\n", encoding="utf-8")
    (roles / "README.md").write_text("# ignore me\n", encoding="utf-8")
    (roles / ".DS_Store").write_text("mac noise\n", encoding="utf-8")
    monkeypatch.setattr(chain, "ROLES_DIR", roles)

    chain.cmd_list_roles()

    out = capsys.readouterr().out
    assert "only.txt" in out
    assert "README.md" not in out
    assert ".DS_Store" not in out
