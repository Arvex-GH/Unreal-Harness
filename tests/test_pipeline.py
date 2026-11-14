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
