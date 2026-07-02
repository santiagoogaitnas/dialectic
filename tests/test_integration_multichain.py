"""Integration tests for multi-chain isolation.

Exercises the contract that two chains sharing a project directory remain
fully isolated at the filesystem level: distinct workspace dirs, distinct
pane (cwd) dirs, distinct logs/bulletins, and registry operations that
don't cross-contaminate.

Also exercises the UI server launch/list/stop flow against an in-process
HTTPServer with the subprocess.Popen call patched out.
"""

import json
import os
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import registry as reg
from registry import ChainConfig, ChainRecord, generate_chain_id
import ui.server as uisrv
from ui.server import DialecticHandler


# --- pane_dirs / workspace isolation ---


def test_two_chains_same_project_have_distinct_pane_dirs(tmp_path):
    """Two chains on the same project must get different pane cwd directories."""
    cfg1 = ChainConfig(chain_id="aa11", session="s1", seed="x", project=str(tmp_path))
    cfg2 = ChainConfig(chain_id="bb22", session="s2", seed="x", project=str(tmp_path))

    a1, b1 = cfg1.pane_dirs()
    a2, b2 = cfg2.pane_dirs()

    assert a1 != a2
    assert b1 != b2
    assert a1 == tmp_path / ".dialectic-a-aa11"
    assert a2 == tmp_path / ".dialectic-a-bb22"
    assert a1.is_dir() and a2.is_dir()
    assert b1.is_dir() and b2.is_dir()


def test_two_chains_workspace_paths_distinct():
    """workspace/ws_a/ws_b/log_file/bulletin_path must differ by chain_id."""
    c1 = ChainConfig(chain_id="first", session="s", seed="x")
    c2 = ChainConfig(chain_id="second", session="s", seed="x")

    assert c1.workspace != c2.workspace
    assert c1.ws_a != c2.ws_a
    assert c1.log_file != c2.log_file
    assert c1.bulletin_path != c2.bulletin_path


def test_pane_dir_writes_do_not_collide(tmp_path):
    """Files written in chain A's pane dir must not appear in chain B's."""
    cfg_a = ChainConfig(chain_id="alpha", session="sa", seed="x", project=str(tmp_path))
    cfg_b = ChainConfig(chain_id="bravo", session="sb", seed="x", project=str(tmp_path))

    dir_a, _ = cfg_a.pane_dirs()
    dir_b, _ = cfg_b.pane_dirs()

    (dir_a / "CLAUDE.md").write_text("role A for alpha\n")
    (dir_b / "CLAUDE.md").write_text("role A for bravo\n")

    assert (dir_a / "CLAUDE.md").read_text() == "role A for alpha\n"
    assert (dir_b / "CLAUDE.md").read_text() == "role A for bravo\n"
    assert not (dir_a / "something-b").exists()
    assert not (dir_b / "something-a").exists()


# --- Registry multi-chain semantics ---


def test_register_two_chains_same_project_both_active(tmp_path):
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        project = "/some/shared/project"
        c1 = ChainConfig(chain_id="m1", session="sm1", seed="x", project=project)
        c2 = ChainConfig(chain_id="m2", session="sm2", seed="x", project=project)
        reg.register_chain(c1, os.getpid())
        reg.register_chain(c2, os.getpid())

        assert reg.count_active_chains() == 2
        assert reg.count_active_chains(project=project) == 2
        assert reg.count_active_chains(project="/other") == 0

        chains = reg.list_chains()
        ids = {c.chain_id for c in chains}
        assert ids == {"m1", "m2"}


def test_stopping_one_chain_does_not_affect_other(tmp_path):
    """Unregistering chain A must leave chain B running."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        c1 = ChainConfig(chain_id="stay", session="ss", seed="x")
        c2 = ChainConfig(chain_id="go", session="sg", seed="x")
        reg.register_chain(c1, os.getpid())
        reg.register_chain(c2, os.getpid())

        reg.unregister_chain("go")

        stay = reg.get_chain("stay")
        gone = reg.get_chain("go")

        assert stay.status == "running"
        assert gone.status == "stopped"
        assert reg.count_active_chains() == 1


def test_update_chain_isolated_per_id(tmp_path):
    """Updating chain A's round/snippet must not mutate chain B."""
    reg_file = tmp_path / ".registry.json"
    with patch.object(reg, "REGISTRY_FILE", reg_file), \
         patch.object(reg, "WORKSPACE", tmp_path):
        reg.register_chain(ChainConfig(chain_id="u1", session="s1", seed="x"), os.getpid())
        reg.register_chain(ChainConfig(chain_id="u2", session="s2", seed="x"), os.getpid())

        reg.update_chain("u1", current_round=9, last_output_snippet="nine")

        u1 = reg.get_chain("u1")
        u2 = reg.get_chain("u2")
        assert u1.current_round == 9
        assert u2.current_round == 0
        assert u1.last_output_snippet == "nine"
        assert u2.last_output_snippet == ""


def test_generate_chain_id_has_timestamp_and_suffix():
    """Chain ID format: MMDDHHmm-xxxx (8-char timestamp, 4 hex suffix)."""
    cid = generate_chain_id()
    ts, suffix = cid.split("-")
    assert len(ts) == 8 and ts.isdigit()
    assert len(suffix) == 4 and all(c in "0123456789abcdef" for c in suffix)


# --- UI server against real HTTP + mocked subprocess ---


def _start_server():
    server = HTTPServer(("127.0.0.1", 0), DialecticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _post_json(server, path, data):
    host, port = server.server_address
    url = f"http://{host}:{port}{path}"
    body = json.dumps(data).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _get_json(server, path):
    host, port = server.server_address
    url = f"http://{host}:{port}{path}"
    try:
        resp = urlopen(Request(url), timeout=5)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def _delete_json(server, path):
    host, port = server.server_address
    url = f"http://{host}:{port}{path}"
    try:
        resp = urlopen(Request(url, method="DELETE"), timeout=5)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_ui_launch_builds_expected_cli_command(tmp_path):
    """POST /api/chains must invoke chain.py with seed, roles, project, flags.

    --no-curator was removed along with discussion mode; this fence ensures
    it never comes back.
    """
    reg_file = tmp_path / ".registry.json"
    fake_proc = MagicMock()
    fake_proc.pid = 31337
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path), \
             patch("ui.server.subprocess.Popen", return_value=fake_proc) as popen:
            project_dir = tmp_path / "myproj"
            project_dir.mkdir()
            status, data = _post_json(server, "/api/chains", {
                "seed": "hello world",
                "role_a": "builder.txt",
                "role_b": "thinker.txt",
                "project": str(project_dir),
            })
            assert status == 201
            assert data["pid"] == 31337
            assert data["status"] == "launched"

            popen.assert_called_once()
            argv = popen.call_args.args[0]
            assert argv[1].endswith("chain.py")
            assert "hello world" in argv
            assert "--role-a" in argv and "builder.txt" in argv
            assert "--role-b" in argv and "thinker.txt" in argv
            assert "--project" in argv and str(project_dir) in argv
            assert "--no-curator" not in argv
    finally:
        server.shutdown()


def test_ui_launch_rejects_bad_project(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _post_json(server, "/api/chains", {
                "seed": "x",
                "project": str(tmp_path / "does-not-exist"),
            })
            assert status == 400
            # The UI routes launches through
            # directory_browser.validate_project_dir, which returns
            # "No such directory: {path}" for a missing path. Earlier this
            # check was a softer "not found" match; pinning the exact
            # phrase keeps any future wording regression visible here.
            assert "no such directory" in data["error"].lower()
    finally:
        server.shutdown()


def test_ui_launch_rejects_empty_seed(tmp_path):
    server = _start_server()
    try:
        status, data = _post_json(server, "/api/chains", {"seed": "   "})
        assert status == 400
        assert "seed" in data["error"].lower()
    finally:
        server.shutdown()


def test_ui_lists_multi_chain_state(tmp_path):
    """UI list endpoint should return both chains when two are registered."""
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(ChainConfig(chain_id="ui1", session="u1", seed="a"), os.getpid())
            reg.register_chain(ChainConfig(chain_id="ui2", session="u2", seed="b"), os.getpid())

            status, data = _get_json(server, "/api/chains")
            assert status == 200
            ids = {c["chain_id"] for c in data}
            assert ids == {"ui1", "ui2"}
            sessions = {c["session"] for c in data}
            assert sessions == {"u1", "u2"}
    finally:
        server.shutdown()


def test_ui_log_endpoint_reads_chain_specific_file(tmp_path):
    """Logs must be read from chainwork/<chain_id>/chain_log.md, not shared."""
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            for cid, body in (("logA", "log for A"), ("logB", "log for B")):
                (tmp_path / cid).mkdir()
                (tmp_path / cid / "chain_log.md").write_text(body + "\n")
                reg.register_chain(ChainConfig(chain_id=cid, session=cid, seed="x"), os.getpid())

            status_a, data_a = _get_json(server, "/api/chains/logA/log")
            status_b, data_b = _get_json(server, "/api/chains/logB/log")

            assert status_a == 200 and status_b == 200
            assert "log for A" in data_a["log"]
            assert "log for B" in data_b["log"]
            assert "log for B" not in data_a["log"]
            assert "log for A" not in data_b["log"]
    finally:
        server.shutdown()


# --- UI DELETE flow + launch-flag forwarding ---


def test_ui_delete_chain_stops_running_chain(tmp_path):
    """DELETE /api/chains/<id> must call stop_chain and mark the record stopped.

    Uses the current process PID (so the liveness check in get_chain keeps the
    record 'running'), then patches os.kill so stop_chain's SIGTERM doesn't
    actually signal the test runner. subprocess.run is patched at the
    subprocess module level because registry imports it inside stop_chain.
    """
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path), \
             patch("registry.os.kill") as mock_kill, \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            cfg = ChainConfig(chain_id="delme", session="sdel", seed="x")
            reg.register_chain(cfg, os.getpid())

            # Sanity: running before DELETE (os.kill mocked so liveness passes).
            pre = reg.get_chain("delme")
            assert pre is not None and pre.status == "running"

            status, data = _delete_json(server, "/api/chains/delme")
            assert status == 200
            assert data == {"status": "stopped", "chain_id": "delme"}

            # SIGTERM was sent to the chain's pid.
            sigterm_calls = [
                c for c in mock_kill.call_args_list
                if len(c.args) == 2 and c.args[0] == os.getpid()
                and c.args[1] not in (0,)  # exclude liveness-check calls
            ]
            assert sigterm_calls, "stop_chain should have signalled SIGTERM"

            # tmux kill-session was invoked for the chain's session.
            kill_calls = [
                c for c in mock_run.call_args_list
                if c.args and "kill-session" in c.args[0]
            ]
            assert kill_calls, "stop_chain should have called tmux kill-session"
            assert "sdel" in kill_calls[0].args[0]

            # Registry now reflects stopped state.
            post = reg.get_chain("delme")
            assert post is not None
            assert post.status == "stopped"
    finally:
        server.shutdown()


def test_ui_launch_forwards_session_flag(tmp_path):
    """POST body session='named-run' must land as --session named-run in argv."""
    reg_file = tmp_path / ".registry.json"
    fake_proc = MagicMock()
    fake_proc.pid = 42
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path), \
             patch("ui.server.subprocess.Popen", return_value=fake_proc) as popen:
            status, _ = _post_json(server, "/api/chains", {
                "seed": "hi",
                "session": "named-run",
            })
            assert status == 201

            argv = popen.call_args.args[0]
            assert "--session" in argv
            # --session is immediately followed by its value.
            assert argv[argv.index("--session") + 1] == "named-run"
    finally:
        server.shutdown()


def test_ui_launch_omits_session_flag_when_not_set(tmp_path):
    """When body omits session, --session must NOT appear in argv.

    Regression guard for the auto-generated session name: chain.py derives
    `chain-<chain_id>` when no --session is passed, and the UI must preserve
    that auto-name path rather than forcing an empty --session.
    """
    reg_file = tmp_path / ".registry.json"
    fake_proc = MagicMock()
    fake_proc.pid = 43
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path), \
             patch("ui.server.subprocess.Popen", return_value=fake_proc) as popen:
            status, _ = _post_json(server, "/api/chains", {"seed": "hi"})
            assert status == 201
            argv = popen.call_args.args[0]
            assert "--session" not in argv
    finally:
        server.shutdown()


def test_ui_launch_forwards_default_max_chains(tmp_path):
    """Default max_chains=5 in the POST body must land as --max-chains 5 in argv.

    This pins the contract that the UI does enforce a concurrency cap by default
    (and it's the same 5 the CLI defaults to), so launching many chains via the
    UI hits the guard rather than silently bypassing it.
    """
    reg_file = tmp_path / ".registry.json"
    fake_proc = MagicMock()
    fake_proc.pid = 44
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path), \
             patch("ui.server.subprocess.Popen", return_value=fake_proc) as popen:
            status, _ = _post_json(server, "/api/chains", {"seed": "hi"})
            assert status == 201
            argv = popen.call_args.args[0]
            assert "--max-chains" in argv
            assert argv[argv.index("--max-chains") + 1] == "5"
    finally:
        server.shutdown()


def test_ui_launch_omits_project_flag_when_unset(tmp_path):
    """Without body.project, argv must NOT contain --project.

    chain.py defaults --project to its own cwd when the flag is absent, and
    the UI must let that default kick in instead of inventing a path. Sending
    an empty --project would make argparse swallow the next token as a path.
    """
    reg_file = tmp_path / ".registry.json"
    fake_proc = MagicMock()
    fake_proc.pid = 45
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path), \
             patch("ui.server.subprocess.Popen", return_value=fake_proc) as popen:
            status, _ = _post_json(server, "/api/chains", {
                "seed": "just chat",
                "role_a": "builder.txt",
                "role_b": "thinker.txt",
            })
            assert status == 201
            argv = popen.call_args.args[0]
            assert "--project" not in argv
            assert "just chat" in argv
    finally:
        server.shutdown()
