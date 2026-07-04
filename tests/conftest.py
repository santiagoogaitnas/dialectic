"""Shared fixtures for the test suite.

Every test runs with CLAUDE_CONFIG_DIR pointed at a throwaway directory
so nothing in the suite can touch the developer's real ~/.claude.json.
Two production paths write to that file (chain.py seeds first-run keys
at launch; registry.unregister_chain prunes a finished chain's scratch
entries), and several tests drive chain.py's __main__ far enough to
reach them.

test_smoke is exempt: it exercises the real `claude` CLI, which needs
the real, authenticated config.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_claude_config(request, tmp_path_factory, monkeypatch):
    if request.module.__name__.rpartition(".")[2] == "test_smoke":
        yield
        return
    config_dir = tmp_path_factory.mktemp("claude-config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    yield
