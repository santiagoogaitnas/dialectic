"""Tests for chain._preflight_binaries — the missing-binary launch check.

The preflight runs in ``__main__`` after the launch guards and before any
side effect. Its contract: exit 1 with a one-line actionable error per
missing binary; do nothing when both tmux and claude resolve on PATH.
"""

import pytest

import chain


def _fake_which(available):
    """Return a shutil.which stand-in that resolves only names in `available`."""
    def which(name):
        return f"/usr/bin/{name}" if name in available else None
    return which


def test_preflight_passes_when_both_binaries_present(monkeypatch):
    monkeypatch.setattr(chain.shutil, "which", _fake_which({"tmux", "claude"}))
    chain._preflight_binaries()  # must not raise


def test_preflight_exits_when_tmux_missing(monkeypatch, caplog):
    monkeypatch.setattr(chain.shutil, "which", _fake_which({"claude"}))
    with caplog.at_level("ERROR", logger="chain"):
        with pytest.raises(SystemExit) as exc:
            chain._preflight_binaries()
    assert exc.value.code == 1
    assert any("tmux" in r.message for r in caplog.records)


def test_preflight_exits_when_claude_missing(monkeypatch, caplog):
    monkeypatch.setattr(chain.shutil, "which", _fake_which({"tmux"}))
    with caplog.at_level("ERROR", logger="chain"):
        with pytest.raises(SystemExit) as exc:
            chain._preflight_binaries()
    assert exc.value.code == 1
    assert any("claude" in r.message for r in caplog.records)


def test_preflight_reports_both_when_both_missing(monkeypatch, caplog):
    monkeypatch.setattr(chain.shutil, "which", _fake_which(set()))
    with caplog.at_level("ERROR", logger="chain"):
        with pytest.raises(SystemExit):
            chain._preflight_binaries()
    messages = " ".join(r.message for r in caplog.records)
    assert "tmux" in messages and "claude" in messages
