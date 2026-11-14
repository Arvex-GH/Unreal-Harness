"""Mock Unreal bridge - speaks the exact wire protocol of the C++ plugin.

Purpose:
  1. let people try the harness without an Unreal install
  2. regression-test the transport + agent tool loop in CI

Run: python -m tests.mock_ue --port 34567
It implements the state machine of a real editor: assets, blueprints, graphs,
nodes, pins and links, with idempotent behaviour matching the plugin.
"""
from __future__ import annotations

import argparse
import json
import socketserver
import threading
import time
import uuid
from typing import Any


class MockEditor:
    def __init__(self) -> None:
        self.blueprints: dict[str, dict[str, Any]] = {}
        self.actors: list[dict[str, Any]] = [
            {"name": "Floor", "class": "StaticMeshActor", "path": "/Game/Maps/Level.Level:PersistentLevel.Floor"},
            {"name": "PlayerStart", "class": "PlayerStart", "path": "/Game/Maps/Level.Level:PersistentLevel.PlayerStart"},
        ]
        self.logs: list[str] = ["[LogInit] Mock editor booted"]
        self.pie = False
        self.calls = 0

    # ------------------------------------------------------------------ util
    def _log(self, msg: str) -> None:
        self.logs.append(msg)

    def _bp(self, path: str) -> dict[str, Any] | None:
        return self.blueprints.get(path)

    def _find_node(self, bp: dict, node_id: str) -> dict | None:
        for n in bp["nodes"]:
            if n["id"] == node_id:
                return n
        return None

    def _pin(self, node: dict, name: str, direction: str) -> dict | None:
        pool = node["inputs"] if direction == "in" else node["outputs"]
        for p in pool:
            if p["name"].lower() == name.lower():
                return p
        if name.lower() in ("then", "exec", ""):
            for p in pool:
                if p["is_exec"]:
                    return p
        return None

    def _resolve(self, bp: dict, spec: str, direction: str):
        node_id, _, pin_name = spec.partition(":")
        node = self._find_node(bp, node_id)
        if not node:
            return None, None, f"node not found: {node_id}"
        pin = self._pin(node, pin_name, direction)
        if not pin:
            return node, None, f"pin '{pin_name}' not found on {node['title']}"
        return node, pin, None

    # ------------------------------------------------------------- dispatch
    def handle(self, cmd: str, args: dict) -> tuple[bool, dict, str]:
        self.calls += 1
        h = getattr(self, "cmd_" + cmd.replace(".", "_"), None)
        if h is None:
            return False, {}, f"unknown command: {cmd}"
        try:
            return h(args)
        except Exception as e:  # noqa: BLE001
            return False, {}, f"mock error: {e}"

    # --------------------------------------------------------------- verbs
    def cmd_handshake(self, a):
        return True, {
            "plugin": "UEHarness", "protocol": 2, "engine_version": "5.8.4-mock",
            "project": "MockGame", "pie_running": self.pie,
            "verbs": ["editor.console", "editor.log", "bp.create", "bp.add_node",
                      "bp.connect", "bp.build_graph", "bp.compile", "umg.create_widget",
                      "actor.find"],
        }, ""

    cmd_ping = cmd_handshake

    def cmd_editor_log(self, a):
        n = int(a.get("lines", 200))
        f = a.get("filter", "")
        lines = [l for l in self.logs if not f or f.lower() in l.lower()][-n:]
        return True, {"lines": lines}, ""

    def cmd_editor_console(self, a):
        self._log(f"[Console] {a.get('command','')}")
        return True, {"output": "ok"}, ""

    def cmd_editor_save_all(self, a):
        self._log("[AssetRegistry] saved all")
        return True, {"saved": True}, ""

    def cmd_asset_search(self, a):
        q = (a.get("query") or "").lower()
        out = [{"name": p.split("/")[-1], "path": p, "class": "Blueprint"}
               for p in self.blueprints if q in p.lower()]
        return True, {"assets": out, "count": len(out)}, ""

    def cmd_bp_create(self, a):
        path = a["path"]
        if path in self.blueprints:
            return False, {}, f"already exists: {path}"
        self.blueprints[path] = {
            "path": path, "parent": a.get("parent", "Actor"),
            "variables": {}, "graphs": {"EventGraph": []}, "nodes": [], "status": "UpToDate",
        }
        self._log(f"[Blueprint] created {path}")
        return True, {"path": path}, ""

    def cmd_bp_variable_add(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        bp["variables"][a["name"]] = a.get("type", "bool")
        return True, {"name": a["name"]}, ""

    def cmd_bp_list_graphs(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        return True, {"graphs": list(bp["graphs"])}, ""

    def cmd_bp_graph_read(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        g = a.get("graph", "EventGraph")
        nodes = [n for n in bp["nodes"] if n["graph"] == g]
        return True, {"graph": g, "nodes": nodes}, ""

    def cmd_bp_add_node(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        kind = a["kind"]
        graph = a.get("graph", "EventGraph")
        if graph not in bp["graphs"]:
            bp["graphs"][graph] = []

        if kind == "call_function":
            title = a.get("function", "Call")
            if title.startswith("K2_"):
                title = title[3:]
            if title == "PrintString":
                ins = [{"name": "execute", "is_exec": True, "direction": "in"},
                       {"name": "InString", "is_exec": False, "direction": "in", "default": ""}]
                outs = [{"name": "then", "is_exec": True, "direction": "out"}]
            else:
                ins = [{"name": "execute", "is_exec": True, "direction": "in"}]
                outs = [{"name": "then", "is_exec": True, "direction": "out"}]
        elif kind == "event":
            title = a.get("event", "Event")
            ins = []
            outs = [{"name": "then", "is_exec": True, "direction": "out"}]
        elif kind == "branch":
            title = "Branch"
            ins = [{"name": "execute", "is_exec": True, "direction": "in"},
                   {"name": "Condition", "is_exec": False, "direction": "in", "default": "false"}]
            outs = [{"name": "then", "is_exec": True, "direction": "out"},
                    {"name": "else", "is_exec": True, "direction": "out"}]
        else:
            title = kind
            ins = [{"name": "execute", "is_exec": True, "direction": "in"}]
            outs = [{"name": "then", "is_exec": True, "direction": "out"}]

        node = {"id": str(uuid.uuid4()), "class": f"K2Node_{kind}", "title": title,
                "graph": graph, "x": a.get("x", 0), "y": a.get("y", 0),
                "inputs": ins, "outputs": outs}
        bp["nodes"].append(node)
        bp["graphs"][graph].append(node["id"])
        return True, node, ""

    def cmd_bp_connect(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        sn, sp, e1 = self._resolve(bp, a["from"], "out")
        if not sp:
            return False, {}, e1
        tn, tp, e2 = self._resolve(bp, a["to"], "in")
        if not tp:
            return False, {}, e2
        if sp["is_exec"] != tp["is_exec"]:
            return False, {}, "incompatible pins"
        sp.setdefault("links", []).append({"node": tn["id"], "pin": tp["name"]})
        tp["connected"] = True
        return True, {"connected": True}, ""

    def cmd_bp_disconnect(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        sn, sp, e = self._resolve(bp, a["from"], "out")
        if not sp:
            return False, {}, e
        sp["links"] = []
        return True, {}, ""

    def cmd_bp_set_pin_default(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        n, p, e = self._resolve(bp, a["pin"], "in")
        if not p:
            return False, {}, e
        p["default"] = a["value"]
        return True, {}, ""

    def cmd_bp_build_graph(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        refs: dict[str, str] = {}
        node_results, errors, link_results = [], [], []

        for spec in a.get("nodes", []):
            sub = {k: v for k, v in spec.items() if k != "ref"}
            sub.setdefault("bp", a["bp"])
            sub.setdefault("graph", a.get("graph", "EventGraph"))
            ok, res, err = self.cmd_bp_add_node(sub)
            if ok:
                refs[spec.get("ref", "")] = res["id"]
                node_results.append({"ref": spec.get("ref"), "id": res["id"],
                                     "title": res["title"]})
            else:
                errors.append({"ref": spec.get("ref"), "error": err})

        def resolve(spec: str) -> str:
            ref, _, pin = spec.partition(".")
            if ref in refs:
                return f"{refs[ref]}:{pin}"
            return spec

        for spec in a.get("links", []):
            ok, _, err = self.cmd_bp_connect({
                "bp": a["bp"], "graph": a.get("graph", "EventGraph"),
                "from": resolve(spec["from"]), "to": resolve(spec["to"])})
            link_results.append({"from": spec["from"], "to": spec["to"],
                                 "ok": ok, **({"error": err} if not ok else {})})

        for spec in a.get("defaults", []):
            self.cmd_bp_set_pin_default({"bp": a["bp"],
                                         "graph": a.get("graph", "EventGraph"),
                                         "pin": resolve(spec["pin"]),
                                         "value": spec["value"]})

        compiled = self.cmd_bp_compile({"bp": a["bp"]})[0] if a.get("compile", True) else False
        return (len(errors) == 0,
                {"nodes": node_results, "links": link_results, "errors": errors,
                 "compiled": compiled}, "")

    def cmd_bp_compile(self, a):
        bp = self._bp(a["bp"])
        if not bp:
            return False, {}, "blueprint not found"
        has_event = any(n["title"].lower().startswith("receive") for n in bp["nodes"])
        bp["status"] = "UpToDate" if (has_event or not bp["nodes"]) else "Error"
        if bp["status"] == "Error":
            self._log(f"[Blueprint] {a['bp']}: Error: graph has no entry point")
            return False, {"success": False, "status": "Error"}, \
                "blueprint compile failed - read editor.log for details"
        return True, {"success": True, "status": "UpToDate"}, ""

    def cmd_actor_find(self, a):
        c = (a.get("class") or "").lower()
        out = [x for x in self.actors if not c or c in x["class"].lower()]
        return True, {"actors": out}, ""

    def cmd_actor_spawn(self, a):
        cls = a.get("class", "Actor")
        shapes = {"cube", "sphere", "cylinder", "cone", "plane"}
        actual_cls = "StaticMeshActor" if cls.lower() in shapes else cls
        name = f"{actual_cls.split('/')[-1]}_{len(self.actors)}"
        label = a.get("label") or name
        self.actors.append({
            "name": name,
            "label": label,
            "class": actual_cls,
            "shape": cls if cls.lower() in shapes else "",
            "path": f"/Game/Maps/Level.Level:{name}",
            "location": f"X={a.get('x',0)} Y={a.get('y',0)} Z={a.get('z',0)}",
            "rotation": f"P={a.get('pitch',0)} Y={a.get('yaw',0)} R={a.get('roll',0)}",
            "props": {},
        })
        return True, {"name": name, "label": label, "path": self.actors[-1]["path"]}, ""

    def cmd_actor_set_property(self, a):
        target = a.get("actor", "")
        for act in self.actors:
            if act["name"] == target or act.get("label", "").lower() == target.lower():
                act.setdefault("props", {})[a.get("property", "")] = a.get("value", "")
                return True, {"set": True, "target": act["name"]}, ""
        return False, {}, f"actor not found: {target}"

    def cmd_level_new(self, a):
        self.actors = []
        if a.get("add_floor", True):
            self.cmd_actor_spawn({"class": "Cube", "x": 0, "y": 0, "z": -50, "label": "Floor"})
        return True, {"path": a.get("path", "/Game/Maps/NewLevel"),
                      "floor_added": bool(a.get("add_floor", True))}, ""

    def cmd_level_camera(self, a):
        loc = f"X={a.get('x', 420)} Y={a.get('y', -180)} Z={a.get('z', 170)}"
        rot = f"P={a.get('pitch', -10)} Y={a.get('yaw', 200)} R={a.get('roll', 0)}"
        return True, {"location": loc, "rotation": rot}, ""

    def cmd_viewport_screenshot(self, a):
        import tempfile
        from pathlib import Path

        out_dir = Path(getattr(self, "shot_dir", "") or tempfile.gettempdir())
        out_dir.mkdir(parents=True, exist_ok=True)
        name = (a.get("name") or "harness_shot").removesuffix(".png")
        p = out_dir / f"{name}.png"
        # Minimal valid 1x1 PNG
        p.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
            b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return True, {"path": str(p), "width": 1280, "height": 720}, ""

    def cmd_pie_start(self, a):
        self.pie = True
        return True, {}, ""

    def cmd_pie_stop(self, a):
        self.pie = False
        return True, {}, ""

    def cmd_python_exec(self, a):
        code = a.get("code", "")
        return True, {"output": f"[mock python] would run {len(code)} chars"}, ""

    def cmd_umg_create_widget(self, a):
        path = a.get("path", "").split(".")[0].rstrip("/")
        title = a.get("title_text", "")
        existed = path in self.blueprints
        if not existed:
            self.blueprints[path] = {
                "parent": "UserWidget",
                "variables": {},
                "nodes": [],
                "status": "UpToDate",
                "title_text": title,
            }
        return True, {"path": path, "created": not existed, "existed": existed,
                      "title_text": title}, ""


class Handler(socketserver.StreamRequestHandler):
    editor: MockEditor = None  # type: ignore

    def handle(self) -> None:
        for raw in self.rfile:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                self.wfile.write(b'{"ok":false,"error":"malformed json"}\n')
                continue
            ok, result, err = self.editor.handle(req.get("cmd", ""), req.get("args") or {})
            resp: dict[str, Any] = {"id": req.get("id", 0), "ok": ok}
            if ok:
                resp["result"] = result
            else:
                resp["error"] = err
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(port: int = 0) -> tuple[Server, MockEditor]:
    """Start a mock editor. port=0 picks a free port (see srv.server_address[1])."""
    editor = MockEditor()
    handler = type("H", (Handler,), {"editor": editor})
    srv = Server(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, editor


def stop(srv: Server) -> None:
    srv.shutdown()
    srv.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=34567)
    ns = ap.parse_args()
    srv, ed = start(ns.port)
    print(f"mock Unreal bridge on 127.0.0.1:{ns.port} - Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
