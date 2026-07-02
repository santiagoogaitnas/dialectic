"""Tests for the web UI server — API endpoints and static serving.

Uses a real HTTPServer bound to localhost for integration-style tests.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import sys

_project_root = str(Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import registry as reg
from registry import ChainConfig

# ui is a package under the project root — import its server module
import ui.server as uisrv
from ui.server import DialecticHandler


def _start_server(port=0, threaded=False):
    """Start a test server on a random available port."""
    cls = ThreadingHTTPServer if threaded else HTTPServer
    server = cls(("127.0.0.1", port), DialecticHandler)
    if threaded:
        server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _get(server, path):
    """GET request to the test server."""
    host, port = server.server_address
    url = f"http://{host}:{port}{path}"
    req = Request(url)
    try:
        resp = urlopen(req, timeout=5)
        return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def _get_json(server, path):
    """GET request, parse JSON response."""
    status, body = _get(server, path)
    return status, json.loads(body)


def _post_json(server, path, data):
    """POST JSON to the test server."""
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


def _delete(server, path):
    """DELETE request."""
    host, port = server.server_address
    url = f"http://{host}:{port}{path}"
    req = Request(url, method="DELETE")
    try:
        resp = urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_index_returns_html():
    import pytest
    index_path = Path(uisrv.STATIC_DIR) / "index.html"
    if not index_path.exists():
        pytest.skip("ui/static/index.html not committed yet")
    server = _start_server()
    try:
        status, body = _get(server, "/")
        assert status == 200
        assert b"Dialectic" in body
        assert b"Chain Dashboard" in body
    finally:
        server.shutdown()


def test_api_roles_lists_txt_files():
    server = _start_server()
    try:
        status, data = _get_json(server, "/api/roles")
        assert status == 200
        assert isinstance(data, list)
        assert "builder.txt" in data
        assert "thinker.txt" in data
        assert all(r.endswith(".txt") for r in data)
    finally:
        server.shutdown()


def test_api_chains_empty(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _get_json(server, "/api/chains")
            assert status == 200
            assert data == []
    finally:
        server.shutdown()


def test_api_chains_with_data(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            cfg = ChainConfig(chain_id="test1", session="s1", seed="hello world")
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains")
            assert status == 200
            assert len(data) == 1
            assert data[0]["chain_id"] == "test1"
    finally:
        server.shutdown()


def test_api_chain_detail(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            cfg = ChainConfig(chain_id="d1", session="sd", seed="detail test")
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains/d1")
            assert status == 200
            assert data["chain_id"] == "d1"
    finally:
        server.shutdown()


def test_api_chain_detail_not_found(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _get_json(server, "/api/chains/nonexistent")
            assert status == 404
    finally:
        server.shutdown()


def test_api_chain_log(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        # Create chain + log file
        log_dir = tmp_path / "logchain"
        log_dir.mkdir()
        (log_dir / "chain_log.md").write_text("# Test Log\n\nHello world.\n")
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            cfg = ChainConfig(chain_id="logchain", session="sl", seed="log test")
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains/logchain/log")
            assert status == 200
            assert "Test Log" in data["log"]
    finally:
        server.shutdown()


def test_api_chain_log_not_found(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _get_json(server, "/api/chains/nope/log")
            assert status == 200
            assert data["log"] == ""
    finally:
        server.shutdown()


def test_api_stop_chain_not_found(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _delete(server, "/api/chains/nonexistent")
            assert status == 404
    finally:
        server.shutdown()


def test_safe_chain_id_rejects_traversal():
    """Path traversal attempts in chain_id must be refused."""
    assert uisrv._safe_chain_id("04161119-9733")
    assert uisrv._safe_chain_id("chain_a_B-1")
    assert not uisrv._safe_chain_id("../etc")
    assert not uisrv._safe_chain_id("a/b")
    assert not uisrv._safe_chain_id("")
    assert not uisrv._safe_chain_id("x" * 100)  # too long


def test_read_chain_log_refuses_bad_id(tmp_path):
    """_read_chain_log returns '' for ids that would escape WORKSPACE."""
    with patch.object(reg, "WORKSPACE", tmp_path):
        # Even if a traversal path has a "real" log, the function must refuse.
        outside = tmp_path.parent / "leak.md"
        outside.write_text("SECRET")
        assert uisrv._read_chain_log("../leak") == ""
        assert uisrv._read_chain_log("a/b") == ""


def test_read_bulletin_refuses_bad_id(tmp_path):
    with patch.object(reg, "WORKSPACE", tmp_path):
        assert uisrv._read_bulletin("../anything") == ""
        assert uisrv._read_bulletin("") == ""


def test_api_chain_bulletin(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            chain_dir = tmp_path / "bl1"
            chain_dir.mkdir()
            (chain_dir / "bulletin.md").write_text("pattern noted\n")
            cfg = ChainConfig(chain_id="bl1", session="sb", seed="b test")
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains/bl1/bulletin")
            assert status == 200
            assert "pattern noted" in data["bulletin"]
    finally:
        server.shutdown()


def test_api_roles_excludes_non_txt(tmp_path, monkeypatch):
    """Only .txt role files should be listed."""
    fake_roles = tmp_path / "roles"
    fake_roles.mkdir()
    (fake_roles / "role_a.txt").write_text("a")
    (fake_roles / "role_b.txt").write_text("b")
    (fake_roles / "notes.md").write_text("not a role")
    monkeypatch.setattr(uisrv, "ROLES_DIR", fake_roles)
    server = _start_server()
    try:
        status, data = _get_json(server, "/api/roles")
        assert status == 200
        assert sorted(data) == ["role_a.txt", "role_b.txt"]
    finally:
        server.shutdown()


def test_threaded_server_does_not_serialize_requests():
    """Regression: before ThreadingHTTPServer, a slow request blocked others.

    We patch reg.list_chains with a function that sleeps for a bit, then fire
    four parallel GETs at /api/chains. If the server serialized them they'd
    take ~4*0.6s; with threading they should finish in roughly one sleep.
    """
    sleep_duration = 0.6

    def slow_list(*args, **kwargs):
        time.sleep(sleep_duration)
        return []

    server = _start_server(threaded=True)
    try:
        with patch.object(reg, "list_chains", side_effect=slow_list):
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = [
                    ex.submit(_get_json, server, "/api/chains")
                    for _ in range(4)
                ]
                results = [f.result() for f in futures]
            elapsed = time.monotonic() - start
        assert all(status == 200 for status, _ in results)
        # 4 sequential calls would be ~2.4s; parallel should be well under 2.0s.
        assert elapsed < 2.0, f"Requests serialized: took {elapsed:.2f}s"
    finally:
        server.shutdown()


def test_api_chain_plan_returns_contents(tmp_path):
    """GET /api/chains/<id>/plan returns the per-chain plan file from the project."""
    reg_file = tmp_path / ".registry.json"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "plan-pl1.md").write_text("# the plan\n\n- step 1\n")
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            cfg = ChainConfig(chain_id="pl1", session="sp", seed="x", project=str(project))
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains/pl1/plan")
            assert status == 200
            assert data["chain_id"] == "pl1"
            assert "step 1" in data["plan"]
    finally:
        server.shutdown()


def test_api_chain_plan_empty_when_file_missing(tmp_path):
    """Registered chain with no plan file yet -> 200 with empty string, not 404.

    The frontend distinguishes 'agents have not written the plan yet' from
    'this chain doesn't exist'. Returning 200 with plan='' preserves that.
    """
    reg_file = tmp_path / ".registry.json"
    project = tmp_path / "proj"
    project.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            cfg = ChainConfig(chain_id="pl2", session="sp", seed="x", project=str(project))
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains/pl2/plan")
            assert status == 200
            assert data["plan"] == ""
    finally:
        server.shutdown()


def test_api_chain_plan_empty_when_no_project(tmp_path):
    """A ChainConfig without a project has no plan file -> empty plan string."""
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            cfg = ChainConfig(chain_id="discplan", session="sd", seed="x")
            reg.register_chain(cfg, os.getpid())
            status, data = _get_json(server, "/api/chains/discplan/plan")
            assert status == 200
            assert data["plan"] == ""
    finally:
        server.shutdown()


def test_api_chain_plan_unknown_chain_returns_404(tmp_path):
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _get_json(server, "/api/chains/never-existed/plan")
            assert status == 404
    finally:
        server.shutdown()


def test_api_chain_plan_rejects_bad_chain_id(tmp_path):
    """Path-traversal-shaped chain IDs are rejected before any file access."""
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            # urllib normalises ../foo, so we use a bracketed token that survives
            # but still fails _safe_chain_id (contains a non-allowed character).
            status, data = _get_json(server, "/api/chains/bad@id/plan")
            assert status == 400
            assert "bad chain_id" in data.get("error", "")
    finally:
        server.shutdown()


def test_single_threaded_server_serializes_requests():
    """Sanity check: non-threaded server does serialize (documents the bug)."""
    sleep_duration = 0.4

    def slow_list(*args, **kwargs):
        time.sleep(sleep_duration)
        return []

    server = _start_server(threaded=False)
    try:
        with patch.object(reg, "list_chains", side_effect=slow_list):
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as ex:
                futures = [
                    ex.submit(_get_json, server, "/api/chains")
                    for _ in range(3)
                ]
                [f.result() for f in futures]
            elapsed = time.monotonic() - start
        # 3 sequential calls of 0.4s each should be at least ~1.1s.
        assert elapsed >= 3 * sleep_duration * 0.9, (
            f"Unthreaded server finished too fast ({elapsed:.2f}s): "
            "either the sleep patch didn't take or the test model is wrong."
        )
    finally:
        server.shutdown()


# --- New endpoints wiring project_view + directory_browser ---

def test_api_projects_empty(tmp_path):
    """GET /api/projects returns [] when nothing is registered."""
    reg_file = tmp_path / ".registry.json"
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            status, data = _get_json(server, "/api/projects")
            assert status == 200
            assert data == []
    finally:
        server.shutdown()


def test_api_projects_groups_by_project(tmp_path):
    """Two chains on the same project collapse to one /api/projects entry."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "sharedproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="p1", session="sp1", seed="a", project=str(proj)),
                os.getpid(),
            )
            reg.register_chain(
                ChainConfig(chain_id="p2", session="sp2", seed="b", project=str(proj)),
                os.getpid(),
            )
            status, data = _get_json(server, "/api/projects")
            assert status == 200
            assert len(data) == 1
            entry = data[0]
            assert entry["project"] == str(proj)
            assert entry["chain_count"] == 2
            chain_ids = sorted(c["chain_id"] for c in entry["chains"])
            assert chain_ids == ["p1", "p2"]
    finally:
        server.shutdown()


def test_api_projects_detail_requires_path(tmp_path):
    """/api/projects/detail without ?path= returns 400."""
    server = _start_server()
    try:
        status, data = _get_json(server, "/api/projects/detail")
        assert status == 400
        assert "path" in data.get("error", "").lower()
    finally:
        server.shutdown()


def test_api_projects_detail_returns_chains_and_summary(tmp_path):
    """Detail surfaces merged chains, claims, activity, and summary text."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "detproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="d1", session="sd1", seed="x", project=str(proj)),
                os.getpid(),
            )
            import project_coordinator as pc
            pc.register_chain(proj, "d1", focus="backend")
            pc.claim_files(proj, "d1", ["src/api.py"])

            status, data = _get_json(
                server, f"/api/projects/detail?path={proj}"
            )
            assert status == 200
            assert data["project"] == str(proj)
            assert any(c["chain_id"] == "d1" for c in data["chains"])
            assert data["claims"].get("src/api.py") == "d1"
            assert isinstance(data["activity"], list) and data["activity"]
            assert "backend" in data.get("summary", "")
            # New: detail payload always carries a conflicts list.
            assert data["conflicts"] == []
    finally:
        server.shutdown()


def test_api_projects_conflicts_requires_path(tmp_path):
    """/api/projects/conflicts without ?path= returns 400."""
    server = _start_server()
    try:
        status, data = _get_json(server, "/api/projects/conflicts")
        assert status == 400
        assert "path" in data.get("error", "").lower()
    finally:
        server.shutdown()


def test_api_projects_conflicts_empty_when_disjoint(tmp_path):
    """Two chains claiming different files produce an empty conflicts list."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "conflictproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="a", session="sa", seed="x", project=str(proj)),
                os.getpid(),
            )
            reg.register_chain(
                ChainConfig(chain_id="b", session="sb", seed="y", project=str(proj)),
                os.getpid(),
            )
            import project_coordinator as pc
            pc.register_chain(proj, "a", focus="x")
            pc.register_chain(proj, "b", focus="y")
            pc.claim_files(proj, "a", ["src/a.py"])
            pc.claim_files(proj, "b", ["src/b.py"])

            status, data = _get_json(
                server, f"/api/projects/conflicts?path={proj}"
            )
            assert status == 200
            assert data["project"] == str(proj)
            assert data["conflicts"] == []
    finally:
        server.shutdown()


def test_api_projects_conflicts_surfaces_overlap(tmp_path):
    """Two chains claiming the same file surfaces via the conflicts endpoint."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "overlapproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="a", session="sa", seed="x", project=str(proj)),
                os.getpid(),
            )
            reg.register_chain(
                ChainConfig(chain_id="b", session="sb", seed="y", project=str(proj)),
                os.getpid(),
            )
            import project_coordinator as pc
            import json as _json
            pc.register_chain(proj, "a", focus="x")
            pc.register_chain(proj, "b", focus="y")
            pc.claim_files(proj, "a", ["src/api.py"])

            # claim_files refuses to add a claim already held by a live peer,
            # so bypass it by editing the JSON directly. This simulates the
            # pathological state the conflict surface is designed to catch.
            coord = pc.coordination_path(proj)
            data = _json.loads(coord.read_text())
            data["chains"]["b"]["files_claimed"] = ["src/api.py"]
            coord.write_text(_json.dumps(data))

            status, data = _get_json(
                server, f"/api/projects/conflicts?path={proj}"
            )
            assert status == 200
            assert data["conflicts"] == [
                {"file": "src/api.py", "chains": ["a", "b"]},
            ]
    finally:
        server.shutdown()


def test_api_projects_carries_conflict_count(tmp_path):
    """The grouped /api/projects listing surfaces conflict_count per project."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "countproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="a", session="sa", seed="x", project=str(proj)),
                os.getpid(),
            )
            reg.register_chain(
                ChainConfig(chain_id="b", session="sb", seed="y", project=str(proj)),
                os.getpid(),
            )
            import project_coordinator as pc
            import json as _json
            pc.register_chain(proj, "a", focus="x")
            pc.register_chain(proj, "b", focus="y")
            pc.claim_files(proj, "a", ["src/api.py"])
            coord = pc.coordination_path(proj)
            state = _json.loads(coord.read_text())
            state["chains"]["b"]["files_claimed"] = ["src/api.py"]
            coord.write_text(_json.dumps(state))

            status, data = _get_json(server, "/api/projects")
            assert status == 200
            entry = next(e for e in data if e["project"] == str(proj))
            assert entry["conflict_count"] == 1
            flags = {c["chain_id"]: c["in_conflict"] for c in entry["chains"]}
            assert flags == {"a": True, "b": True}
    finally:
        server.shutdown()


def test_api_projects_carries_recent_claim_counts(tmp_path):
    """/api/projects exposes recent_claim_total + per-chain recent_claims."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "claimproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="a", session="sa", seed="x", project=str(proj)),
                os.getpid(),
            )
            reg.register_chain(
                ChainConfig(chain_id="b", session="sb", seed="y", project=str(proj)),
                os.getpid(),
            )
            import project_coordinator as pc
            pc.register_chain(proj, "a", focus="x")
            pc.register_chain(proj, "b", focus="y")
            pc.claim_files(proj, "a", ["src/one.py"])
            pc.claim_files(proj, "a", ["src/two.py"])
            # b intentionally makes no claims — should land at 0, the amber
            # "chain is editing without the CLI" signal operators want to see.

            status, data = _get_json(server, "/api/projects")
            assert status == 200
            entry = next(e for e in data if e["project"] == str(proj))
            assert entry["recent_claim_total"] == 2
            assert entry["claim_rate_window_seconds"] == 3600.0
            counts = {c["chain_id"]: c["recent_claims"] for c in entry["chains"]}
            assert counts == {"a": 2, "b": 0}
    finally:
        server.shutdown()


def test_api_projects_detail_carries_claim_rates(tmp_path):
    """/api/projects/detail exposes claim_rates dict + per-chain recent_claims."""
    reg_file = tmp_path / ".registry.json"
    proj = tmp_path / "detailproj"
    proj.mkdir()
    server = _start_server()
    try:
        with patch.object(reg, "REGISTRY_FILE", reg_file), \
             patch.object(reg, "WORKSPACE", tmp_path):
            reg.register_chain(
                ChainConfig(chain_id="a", session="sa", seed="x", project=str(proj)),
                os.getpid(),
            )
            import project_coordinator as pc
            pc.register_chain(proj, "a", focus="x")
            pc.claim_files(proj, "a", ["src/one.py"])
            pc.claim_files(proj, "a", ["src/two.py"])
            pc.claim_files(proj, "a", ["src/three.py"])

            status, data = _get_json(
                server, f"/api/projects/detail?path={proj}"
            )
            assert status == 200
            assert data["claim_rates"] == {"a": 3}
            assert data["claim_rate_window_seconds"] == 3600.0
            (chain,) = data["chains"]
            assert chain["recent_claims"] == 3
    finally:
        server.shutdown()


def test_api_browse_lists_directories(tmp_path):
    """/api/browse returns a directory listing with project markers flagged."""
    sub_proj = tmp_path / "myproj"
    sub_proj.mkdir()
    (sub_proj / ".git").mkdir()  # marker — should flag as project
    plain = tmp_path / "empty"
    plain.mkdir()
    server = _start_server()
    try:
        status, data = _get_json(server, f"/api/browse?path={tmp_path}")
        assert status == 200
        assert data["error"] is None
        names = [e["name"] for e in data["entries"]]
        assert "myproj" in names and "empty" in names
        proj_entry = next(e for e in data["entries"] if e["name"] == "myproj")
        assert proj_entry["is_project"] is True
    finally:
        server.shutdown()


def test_api_browse_rejects_missing_path(tmp_path):
    """Missing path in ?path= surfaces as an error field, not a crash."""
    server = _start_server()
    try:
        missing = tmp_path / "does_not_exist"
        status, data = _get_json(server, f"/api/browse?path={missing}")
        assert status == 200
        assert data["error"] is not None
        assert data["entries"] == []
    finally:
        server.shutdown()


def test_api_browse_defaults_to_home_when_no_path(tmp_path):
    """No ?path= falls back to the user's home directory."""
    server = _start_server()
    try:
        status, data = _get_json(server, "/api/browse")
        assert status == 200
        assert str(Path.home()) in data["path"]
        assert "suggestions" in data
    finally:
        server.shutdown()


def test_api_validate_project_accepts_real_dir(tmp_path):
    """A real directory under the user's tree validates as ok."""
    server = _start_server()
    try:
        status, data = _get_json(server, f"/api/validate_project?path={tmp_path}")
        assert status == 200
        assert data["ok"] is True
        assert data["path"] == str(tmp_path)
    finally:
        server.shutdown()


def test_api_validate_project_rejects_missing(tmp_path):
    """Non-existent path returns ok=False with an explanation."""
    server = _start_server()
    try:
        missing = tmp_path / "no_such_dir"
        status, data = _get_json(server, f"/api/validate_project?path={missing}")
        assert status == 200
        assert data["ok"] is False
        assert "No such directory" in data["reason"]
    finally:
        server.shutdown()


def test_api_validate_project_rejects_system_path(tmp_path):
    """Refused-list paths (e.g. /etc) are rejected with a readable reason."""
    server = _start_server()
    try:
        status, data = _get_json(server, "/api/validate_project?path=/etc")
        assert status == 200
        assert data["ok"] is False
        assert "system path" in data["reason"].lower()
    finally:
        server.shutdown()


def test_api_chains_launch_validates_project_directory(tmp_path):
    """POST /api/chains rejects a bogus project with a readable error, no launch."""
    server = _start_server()
    try:
        launched = []
        def fake_launch(*args, **kwargs):
            launched.append(args)
            return 12345
        with patch.object(uisrv, "_launch_chain_background", side_effect=fake_launch):
            status, data = _post_json(server, "/api/chains", {
                "seed": "hello",
                "project": str(tmp_path / "no_such_dir"),
            })
            assert status == 400
            assert "No such directory" in data.get("error", "")
            assert launched == []
    finally:
        server.shutdown()


def test_api_chains_launch_forwards_focus_to_chain_argv(tmp_path):
    """POST with focus passes --focus VALUE through chain.py's argv.

    chain.py's ChainCoordinatorContext writes the focus into
    project_coordinator on boot, so the UI's job is just to forward the
    value — no polling, no duplicate registration.
    """
    proj = tmp_path / "focused"
    proj.mkdir()
    captured_argv = []

    def fake_popen(cmd, **kwargs):
        captured_argv.append(list(cmd))
        class _P:
            pid = 999999
        return _P()

    server = _start_server()
    try:
        with patch.object(uisrv.subprocess, "Popen", side_effect=fake_popen):
            status, data = _post_json(server, "/api/chains", {
                "seed": "hello",
                "project": str(proj),
                "focus": "tests",
            })
        assert status == 201
        assert data.get("focus_applied") is True
        assert data.get("pid") == 999999
        assert len(captured_argv) == 1
        argv = captured_argv[0]
        assert "--focus" in argv
        assert argv[argv.index("--focus") + 1] == "tests"
        assert "--project" in argv
        assert argv[argv.index("--project") + 1] == str(proj)
    finally:
        server.shutdown()


def test_api_chains_launch_without_focus_omits_focus_flag(tmp_path):
    """When no focus is passed, --focus does not appear in chain.py's argv."""
    proj = tmp_path / "nofocus"
    proj.mkdir()
    captured_argv = []

    def fake_popen(cmd, **kwargs):
        captured_argv.append(list(cmd))
        class _P:
            pid = 777
        return _P()

    server = _start_server()
    try:
        with patch.object(uisrv.subprocess, "Popen", side_effect=fake_popen):
            status, data = _post_json(server, "/api/chains", {
                "seed": "hello",
                "project": str(proj),
            })
        assert status == 201
        assert data.get("focus_applied") is False
        assert len(captured_argv) == 1
        assert "--focus" not in captured_argv[0]
    finally:
        server.shutdown()


def test_api_chains_launch_strips_focus_whitespace(tmp_path):
    """Whitespace-only focus is treated as absent (no --focus flag)."""
    proj = tmp_path / "wsfocus"
    proj.mkdir()
    captured_argv = []

    def fake_popen(cmd, **kwargs):
        captured_argv.append(list(cmd))
        class _P:
            pid = 888
        return _P()

    server = _start_server()
    try:
        with patch.object(uisrv.subprocess, "Popen", side_effect=fake_popen):
            status, data = _post_json(server, "/api/chains", {
                "seed": "hello",
                "project": str(proj),
                "focus": "   ",
            })
        assert status == 201
        assert data.get("focus_applied") is False
        assert "--focus" not in captured_argv[0]
    finally:
        server.shutdown()
