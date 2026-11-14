"""End-to-end tests against the mock editor bridge.

Run: python -m pytest tests/ -q      (or: python tests/test_pipeline.py)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.config import Config
from harness.tools.base import registry
from harness.ue.transport import UnrealClient
from tests import mock_ue


class Ctx:
    def __init__(self, cfg, ue, uectl=None, installer=None):
        self.cfg = cfg
        self.ue = ue
        self.uectl = uectl
        self.installer = installer


def setup(bp="EventGraph"):
    srv, editor = mock_ue.start(0)
    port = srv.server_address[1]
    home = tempfile.mkdtemp()
    os.environ["UNREAL_HARNESS_HOME"] = home
    cfg = Config()
    cfg.bridge_port = port
    cfg.native_tool_calls = True
    ue = UnrealClient(cfg)
    return srv, editor, cfg, ue


def test_bridge_handshake():
    srv, editor, cfg, ue = setup()
    st = ue.status()
    assert st["transport"] == "bridge", st
    assert st["editor_connected"] is True
    assert st["bridge"]["handshake"]["project"] == "MockGame"
    mock_ue.stop(srv)
    print("✓ handshake / transport detection")


def test_blueprint_full_flow():
    srv, editor, cfg, ue = setup()
    ctx = Ctx(cfg, ue)

    r = registry.execute_sync = None  # not used; keep tools honest
    import asyncio

    async def run():
        return await registry.execute("bp_create", {"path": "/Game/Blueprints/BP_Floater",
                                                    "parent": "Actor"}, ctx)
    res = asyncio.run(run())
    assert res["ok"], res
    print("✓ bp_create")

    async def run2():
        return await registry.execute("bp_add_variable",
                                      {"bp": "/Game/Blueprints/BP_Floater",
                                       "name": "Speed", "type": "float"}, ctx)
    assert asyncio.run(run2())["ok"]
    print("✓ bp_add_variable")

    # the money path: one call, whole graph
    nodes = [
        {"ref": "begin", "kind": "event", "event": "ReceiveBeginPlay",
         "x": 0, "y": 0},
        {"ref": "print", "kind": "call_function", "function": "PrintString",
         "x": 300, "y": 0},
    ]
    links = [{"from": "begin.then", "to": "print.execute"}]
    defaults = [{"pin": "print.InString", "value": "Floating!"}]

    async def run3():
        return await registry.execute("bp_build", {
            "bp": "/Game/Blueprints/BP_Floater", "nodes": nodes,
            "links": links, "defaults": defaults, "compile": True}, ctx)
    res = asyncio.run(run3())
    assert res["ok"], res
    assert res["compiled"] is True
    assert len(res["nodes"]) == 2 and len(res["links"]) == 1
    assert res["errors"] == []
    print("✓ bp_build (2 nodes, 1 link, 1 default, compiled) in one round trip")

    # verify the graph really is wired
    async def run4():
        return await registry.execute("bp_read_graph",
                                      {"bp": "/Game/Blueprints/BP_Floater"}, ctx)
    g = asyncio.run(run4())
    assert g["ok"]
    print_node = [n for n in g["nodes"] if n["title"] == "PrintString"][0]
    instr = [p for p in print_node["inputs"] if p["name"] == "InString"][0]
    assert instr["default"] == "Floating!", instr
    exec_pin = [p for p in print_node["inputs"] if p["is_exec"]][0]
    assert exec_pin["connected"] is True, exec_pin
    print("✓ bp_read_graph confirms pin default + exec link")

    # error path: connect exec to data must fail cleanly
    async def run5():
        return await registry.execute("bp_connect", {
            "bp": "/Game/Blueprints/BP_Floater",
            "from": f"{print_node['id']}:then",
            "to": f"{print_node['id']}:InString"}, ctx)
    bad = asyncio.run(run5())
    assert bad["ok"] is False and "incompatible" in bad["error"], bad
    print("✓ invalid connection rejected with a readable error")

    mock_ue.stop(srv)


def test_batch_pipelining():
    srv, editor, cfg, ue = setup()
    import asyncio

    ctx = Ctx(cfg, ue)
    calls = [{"cmd": "bp.create", "args": {"path": f"/Game/BP_{i}", "parent": "Actor"}}
             for i in range(20)]

    async def run():
        return await registry.execute("ue_run_batch", {"calls": calls}, ctx)
    res = asyncio.run(run())
    assert res["ok"], res
    assert len(res["results"]) == 20
    print("✓ ue_run_batch: 20 commands in one socket round trip")
    mock_ue.stop(srv)


def test_actor_tools():
    srv, editor, cfg, ue = setup()
    import asyncio

    ctx = Ctx(cfg, ue)

    async def run():
        a = await registry.execute("level_find_actors", {"limit": 10}, ctx)
        b = await registry.execute("level_spawn", {"class": "StaticMeshActor",
                                                  "x": 100, "y": 0, "z": 0}, ctx)
        return a, b
    found, spawned = asyncio.run(run())
    assert found["ok"] and found["actors"]
    assert spawned["ok"] and spawned["name"].startswith("StaticMeshActor")
    print(f"✓ level_find_actors ({len(found['actors'])}) + level_spawn ({spawned['name']})")
    mock_ue.stop(srv)


def test_fs_tools_are_sandboxed():
    srv, editor, cfg, ue = setup()
    import asyncio

    cfg.project_path = str(Path(tempfile.mkdtemp()) / "Game.uproject")
    Path(cfg.project_path).write_text("{}")
    ctx = Ctx(cfg, ue)

    async def run():
        w = await registry.execute("fs_write", {"path": "Source/Test.h",
                                                "content": "// hi\n"}, ctx)
        r = await registry.execute("fs_read", {"path": "Source/Test.h"}, ctx)
        escape = await registry.execute("fs_write", {"path": "/etc/passwd",
                                                     "content": "nope"}, ctx)
        patch = await registry.execute("fs_patch", {"path": "Source/Test.h",
                                                    "old": "// hi", "new": "// bye"}, ctx)
        return w, r, escape, patch
    w, r, escape, patch = asyncio.run(run())
    assert w["ok"] and r["content"].strip() == "// hi"
    assert escape["ok"] is False and "refusing" in escape["error"]
    assert patch["ok"] and patch["replaced"] == 1
    print("✓ fs write/read/patch work, sandbox blocks writes outside project")
    mock_ue.stop(srv)


def test_prune():
    from harness.agent import prune

    msgs = [{"role": "system", "content": "sys"}]
    for i in range(60):
        msgs.append({"role": "tool", "content": "x" * 2000, "tool_call_id": str(i)})
        msgs.append({"role": "assistant", "content": "ok " * 50})
    out = prune(msgs, 4000)
    total = sum(len(m.get("content") or "") // 4 for m in out)
    assert total <= 4000 * 1.3, total
    assert out[0]["role"] == "system"
    assert len(out) < len(msgs)
    print(f"✓ context pruning: {len(msgs)} -> {len(out)} messages, ~{total} tokens")


def test_tool_schemas_strict_json_schema():
    from tests.mock_lmstudio import validate_tool_schemas

    cfg = Config()
    cfg.allow_shell = True
    cfg.allow_network = True
    schemas = registry.schemas(cfg)
    assert len(schemas) == 49, f"expected 49 tools, got {len(schemas)}"
    err = validate_tool_schemas(schemas)
    assert err is None, err

    # Specifically verify the previously broken tools now have proper defaults & object items
    by_name = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in schemas}
    assert "enum" not in by_name["fs_list"]["path"] and by_name["fs_list"]["path"]["default"] == "."
    assert "enum" not in by_name["fs_grep"]["path"] and by_name["fs_grep"]["path"]["default"] == "."
    assert "enum" not in by_name["ue_search_assets"]["path"] and by_name["ue_search_assets"]["path"]["default"] == "/Game"
    assert by_name["bp_build"]["nodes"]["items"]["type"] == "object"
    assert by_name["ue_run_batch"]["calls"]["items"]["type"] == "object"
    print(f"✓ all {len(schemas)} tool JSON schemas pass strict llama.cpp grammar validation")


def test_uplugin_version_and_sync():
    from harness import ROOT
    from harness.config import discover_engine
    from harness.ue.project import Installer

    uplugin = json.loads((ROOT / "plugin" / "UEHarness" / "UEHarness.uplugin").read_text("utf-8"))
    assert "EngineVersion" not in uplugin, f"EngineVersion must be omitted, got {uplugin}"
    assert uplugin["Version"] >= 3

    cpp_srv = (ROOT / "plugin/UEHarness/Source/UEHarness/Private/UEHarnessServer.cpp").read_text("utf-8")
    cpp_cmd = (ROOT / "plugin/UEHarness/Source/UEHarness/Private/UEHarnessCommands.cpp").read_text("utf-8")
    assert "GetSynchronicEvent" not in cpp_srv and "GetSynchEventFromPool" in cpp_srv
    assert "BS_DBeingCreated" not in cpp_cmd and "Guid->ToString()" not in cpp_cmd
    assert "ImportText_Direct" in cpp_cmd and '"umg.create_widget"' in cpp_cmd

    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = Path(tmp) / "MyGame"
        (proj_dir / "Source").mkdir(parents=True)
        uproj = proj_dir / "MyGame.uproject"
        uproj.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": "5.8"}), "utf-8")

        # Simulate stale 5.1.0 plugin already copied into user's project
        stale_dir = proj_dir / "Plugins" / "UEHarness"
        stale_dir.mkdir(parents=True)
        (stale_dir / "UEHarness.uplugin").write_text(
            json.dumps({"FileVersion": 3, "Version": 2, "VersionName": "1.0.1", "EngineVersion": "5.1.0"}),
            "utf-8",
        )

        # Also verify EngineAssociation "5.8" (len == 3) probes cleanly when fake engine dir exists
        fake_eng = Path(tmp) / "UE_5.8"
        (fake_eng / "Engine").mkdir(parents=True)
        uproj_abs = proj_dir / "Abs.uproject"
        uproj_abs.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": str(fake_eng)}), "utf-8")
        assert discover_engine(str(uproj_abs)) == fake_eng

        cfg = Config()
        cfg.project_path = str(uproj)
        inst = Installer(cfg)
        sync1 = inst.sync_existing_install()
        assert sync1.get("plugin_updated"), sync1
        synced_desc = json.loads((stale_dir / "UEHarness.uplugin").read_text("utf-8"))
        assert "EngineVersion" not in synced_desc
        assert synced_desc["VersionName"] == uplugin["VersionName"]

        # Verify DefaultRemoteControl.ini has bEnableRemotePythonExecution=True and is idempotent
        inst.sync_existing_install()
        rc_ini = (proj_dir / "Config" / "DefaultRemoteControl.ini").read_text("utf-8")
        assert rc_ini.count("[/Script/RemoteControlCommon.RemoteControlSettings]") == 1, rc_ini
        assert "bEnableRemotePythonExecution=True" in rc_ini
        assert "bAllowConsoleCommandRemoteExecution=True" in rc_ini
        init_py = (proj_dir / "Content" / "Python" / "init_unreal.py").read_text("utf-8")
        assert "enable_remote_python_execution" in init_py
    print("✓ UE 5.8.4 .uplugin compatibility, C++ bridge fixes, and automatic project sync")


def test_umg_create_widget_rc_and_bridge():
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    # 1) Native bridge path
    srv, editor, cfg, ue = setup()
    ctx = Ctx(cfg, ue)
    res_bridge = asyncio.run(
        registry.execute("umg_create_widget", {"path": "/Game/UI/StartMenu", "title_text": "Start Menu"}, ctx)
    )
    assert res_bridge["ok"] and res_bridge["created"] is True, res_bridge
    assert res_bridge["title_text"] == "Start Menu"
    mock_ue.stop(srv)

    # 2) Remote Control path when Default__PythonScriptLibrary is BLOCKED (RC 400) AND when enabled
    rc_state = {"python_enabled": False, "assets": set(), "default_root": "None", "calls": []}

    class MockRCHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/remote/info":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"HttpRoutes":[]}')
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self):
            ln = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(ln) or b"{}")
            rc_state["calls"].append((self.path, body))
            if self.path == "/remote/object/property":
                val = (body.get("propertyValue") or {}).get(body.get("propertyName", ""))
                rc_state["default_root"] = val
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            if self.path == "/remote/object/call":
                obj = body.get("objectPath", "")
                fn = body.get("functionName", "")
                params = body.get("parameters") or {}
                if "Default__PythonScriptLibrary" in obj:
                    if not rc_state["python_enabled"]:
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(
                            b'{"errorMessage":"Object Default__PythonScriptLibrary cannot be accessed remotely, check remote control project settings."}'
                        )
                        return
                    out = '__HARNESS__' + json.dumps({
                        "_ok": True, "path": "/Game/UI/MainHUD", "created": True,
                        "existed": False, "title_text": "HUD",
                    })
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"ReturnValue": {"CommandResult": "", "LogOutput": out}}).encode())
                    return
                if fn == "DoesAssetExist":
                    rv = params.get("AssetPath") in rc_state["assets"]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"ReturnValue": rv}).encode())
                    return
                if fn == "GetAssetTools":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ReturnValue":"/Engine/Transient.AssetToolsImpl_0"}')
                    return
                if fn == "CreateAsset":
                    pkg_path = f"{params['PackagePath']}/{params['AssetName']}"
                    rc_state["assets"].add(pkg_path)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"ReturnValue": f"{pkg_path}.{params['AssetName']}"}).encode())
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ReturnValue":true}')
                return
            self.send_response(404)
            self.end_headers()

    rc_srv = HTTPServer(("127.0.0.1", 0), MockRCHandler)
    threading.Thread(target=rc_srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            uproj = Path(tmp) / "RCGame.uproject"
            uproj.write_text('{"FileVersion": 3, "EngineAssociation": "5.8"}', "utf-8")
            rc_cfg = Config()
            rc_cfg.project_path = str(uproj)
            rc_cfg.bridge_port = 1  # unreachable -> falls back to RC
            rc_cfg.rc_port = rc_srv.server_address[1]
            rc_ue = UnrealClient(rc_cfg)
            rc_ctx = Ctx(rc_cfg, rc_ue)

            # While Default__PythonScriptLibrary returns 400, umg_create_widget STILL succeeds!
            res_rc_blocked = asyncio.run(
                registry.execute("umg_create_widget", {"path": "/Game/UI/StartMenu", "title_text": "Start Menu"}, rc_ctx)
            )
            assert res_rc_blocked.get("ok") is True, res_rc_blocked
            assert res_rc_blocked.get("created") is True
            assert res_rc_blocked.get("path") == "/Game/UI/StartMenu"
            assert "/Game/UI/StartMenu" in rc_state["assets"]
            # And DefaultRemoteControl.ini was auto-written so next editor launch enables Python!
            rc_ini = (Path(tmp) / "Config" / "DefaultRemoteControl.ini").read_text("utf-8")
            assert "bEnableRemotePythonExecution=True" in rc_ini

            # Once Python is enabled, RC Python path works & parses __HARNESS__ JSON
            rc_state["python_enabled"] = True
            res_rc_py = asyncio.run(
                registry.execute("umg_create_widget", {"path": "/Game/UI/MainHUD", "title_text": "HUD"}, rc_ctx)
            )
            assert res_rc_py.get("ok") is True and res_rc_py.get("title_text") == "HUD", res_rc_py
    finally:
        rc_srv.shutdown()
        rc_srv.server_close()
    print("✓ umg_create_widget succeeds via native bridge, RC Python, AND RC direct fallback on 400")


def test_ui_scroll_css_and_controls():
    from harness import ROOT

    html = (ROOT / "harness" / "static" / "index.html").read_text("utf-8")
    assert "grid-template-rows:minmax(0,1fr)" in html
    assert ".main{display:flex;flex-direction:column;min-width:0;min-height:0;height:100%;overflow:hidden" in html
    assert ".scroll{flex:1 1 0%;min-height:0;overflow-y:auto" in html
    assert "id=\"jumpBottom\"" in html and "updateJumpButton" in html
    assert "$('#btnUpdate').onclick" in html
    print("✓ web UI CSS Grid/Flex min-height:0 chat scroll + jump-to-bottom + update button")


a: list = []
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
