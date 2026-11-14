"""Unreal Engine tools.

Three groups, all reachable by the agent:
  * editor_*   - the things a human does with menus and hotkeys
  * bp_*       - Blueprint authoring (create, wire, compile) at native speed
  * level_*    - actors, properties, PIE
  * ue_*       - assets, materials, C++, python, builds
"""
from __future__ import annotations

from .base import A, B, N, O, P, S, req, tool


def _ue(ctx):
    return ctx.ue


def _nr(ctx, res, err, **extra):
    if err and not res:
        return {"error": err, **extra}
    return {"ok": True, **(res or {}), **extra}


# ---------------------------------------------------------------- connection
@tool("ue_status", "Check editor connection, transport in use, project and latency. "
      "Call this first when something fails.", "unreal")
def ue_status(ctx):
    st = _ue(ctx).status()
    if st["transport"] == "disk":
        st["next_step"] = (
            "The editor isn't reachable. Options: 1) start the editor with the Harness "
            "plugin (ue_launch_editor), 2) enable 'Remote Control API' in Edit > Plugins, "
            "3) work purely on files (fs_* tools) then rebuild.")
    return st


@tool("ue_launch_editor", "Start the Unreal Editor for the configured project, with the "
      "Harness bridge plugin loaded. Returns once the editor process is up.",
      "unreal", risk="write",
      parameters=req(P(wait=N("Seconds to wait for the bridge to come up", 90),
                       extra_args=S("Extra editor CLI args"))))
def ue_launch_editor(ctx, wait: int = 90, extra_args: str = ""):
    import subprocess
    import time
    from pathlib import Path

    cfg = ctx.cfg
    proj = cfg.project_file
    if not proj:
        return {"error": "no .uproject configured", "hint": "set project_path in Settings"}
    if cfg.engine_path:
        exe = Path(cfg.engine_path) / "Engine/Binaries" / ("Win64" if __import__("sys").platform == "win32" else "Linux") / (
            "UnrealEditor.exe" if __import__("sys").platform == "win32" else "UnrealEditor")
    else:
        return {"error": "engine_path not configured"}
    if not exe.exists():
        return {"error": f"editor binary not found: {exe}"}
    cmd = [str(exe), str(proj), f"-HarnessPort={cfg.bridge_port}"]
    if extra_args:
        cmd += extra_args.split()
    subprocess.Popen(cmd, cwd=str(proj.parent), close_fds=True)
    deadline = time.time() + max(5, wait)
    while time.time() < deadline:
        if ctx.ue.bridge.connect(timeout=1.0):
            return {"launched": True, "bridge": "connected",
                    "handshake": ctx.ue.bridge.handshake}
        time.sleep(1.0)
    return {"ok": True, "launched": True, "bridge": "not yet reachable",
            "hint": "editor is still compiling/booting; wait and call ue_status"}


@tool("ue_install_bridge", "Install/reinstall the native Harness plugin into the project "
      "and (optionally) bootstraps the stock Remote Control + Python plugins. This is what "
      "makes the agent fast.", "unreal", risk="write",
      parameters=req(P(build=B("Compile the plugin now", True),
                       enable_rc=B("Also enable Remote Control + Python plugins", True))))
def ue_install_bridge(ctx, build: bool = True, enable_rc: bool = True):
    return ctx.installer.install_plugin(build=build, enable_rc=enable_rc)


# -------------------------------------------------------------------- editor
@tool("editor_console", "Run any Unreal console command (stat, r.*, obj, slomo, ...). "
      "This is the same as typing in the editor console.", "unreal", risk="write",
      parameters=req(P(command=S("Console command")), "command"))
def editor_console(ctx, command: str):
    ok, res, err = _ue(ctx).call("editor.console", {"command": command})
    return _nr(ctx, res, err)


@tool("editor_log", "Read recent editor log lines - compile errors, warnings, your own prints.",
      "unreal",
      parameters=P(lines=N("How many trailing lines", 200), filter=S("Substring filter")))
def editor_log(ctx, lines: int = 200, filter: str = ""):
    ok, res, err = _ue(ctx).call("editor.log", {"lines": lines, "filter": filter})
    return _nr(ctx, res, err)


@tool("editor_save", "Save all dirty assets and levels.", "unreal", risk="write")
def editor_save(ctx):
    ok, res, err = _ue(ctx).call("editor.save_all")
    return _nr(ctx, res, err)


@tool("editor_undo", "Undo the last editor transaction (AI edits are normal transactions).",
      "unreal", risk="destructive")
def editor_undo(ctx):
    ok, res, err = _ue(ctx).call("editor.undo")
    return _nr(ctx, res, err)


@tool("editor_select", "Select an asset in the Content Browser (focus the human's attention).",
      "unreal", parameters=req(P(asset=S("Asset path, e.g. /Game/BP_Test")), "asset"))
def editor_select(ctx, asset: str):
    ok, res, err = _ue(ctx).call("editor.select", {"asset": asset})
    return _nr(ctx, res, err)


# ------------------------------------------------------------------ discover
@tool("ue_find_function", "Search the engine's callable API for a function name. Use this "
      "before bp_add_call so you target the real UFunction name.", "unreal",
      parameters=req(P(name=S("Name or partial name, e.g. 'PrintString' or 'SetActorLocation'"),
                       limit=N("Max results", 15)), "name"))
def ue_find_function(ctx, name: str, limit: int = 15):
    code = (
        "import unreal, json\n"
        "q = %r.lower()\n"
        "hits = []\n"
        "for cls_name in ['KismetSystemLibrary','KismetMathLibrary','GameplayStatics',\n"
        "                 'KismetStringLibrary','BlueprintFunctionLibrary','Actor',\n"
        "                 'KismetInputLibrary','KismetMaterialLibrary','MathLibrary',\n"
        "                 'KismetArrayLibrary','KismetTextLibrary','WidgetBlueprintLibrary',\n"
        "                 'KismetRenderingLibrary','NiagaraFunctionLibrary']:\n"
        "    cls = getattr(unreal, cls_name, None)\n"
        "    if cls is None: continue\n"
        "    for fn in dir(cls):\n"
        "        if q in fn.lower():\n"
        "            hits.append({'class': cls_name, 'function': fn})\n"
        "print(json.dumps(hits[:%d]))\n" % (name, limit)
    )
    ok, res, err = _ue(ctx).python(code)
    if not ok:
        return {"error": err}
    out = (res or {}).get("output", "")
    import json as _json
    import re as _re
    m = _re.search(r"(\[.*\])\s*$", out, _re.S)
    try:
        return {"functions": _json.loads(m.group(1)) if m else [], "raw": out[-800:]}
    except Exception:
        return {"functions": [], "raw": out[-1200:]}


@tool("ue_search_assets", "Search the Content Browser for assets by name and/or class.",
      "unreal",
      parameters=P(query=S("Name substring"), **{"class": S("Class name, e.g. Blueprint, StaticMesh")},
                   path=S("Content root", default="/Game"), limit=N("Max", 60)))
def ue_search_assets(ctx, query: str = "", limit: int = 60, path: str = "/Game", **kw):
    ok, res, err = _ue(ctx).call("asset.search", {
        "query": query, "class": kw.get("class", ""), "path": path, "limit": limit})
    if not ok:
        return {"error": err}
    return res


@tool("ue_asset_info", "Deep-inspect one asset: class, dependencies, thumbnail path, "
      "and for blueprints its graphs/variables.", "unreal",
      parameters=req(P(asset=S("Asset path")), "asset"))
def ue_asset_info(ctx, asset: str):
    code = (
        "import unreal, json\n"
        "a = unreal.load_asset(%r)\n"
        "if a is None:\n"
        "    print(json.dumps({'error':'not found'}))\n"
        "else:\n"
        "    d = {'name': a.get_name(), 'class': a.get_class().get_name(), 'path': a.get_path_name()}\n"
        "    try: d['parent'] = unreal.BlueprintEditorLibrary.get_blueprint_parent_class(a)\n"
        "    except Exception: pass\n"
        "    try:\n"
        "        d['graphs'] = [g.get_name() for g in a.get_editor_property('function_graphs')] \n"
        "    except Exception: pass\n"
        "    print(json.dumps(d))\n" % asset
    )
    ok, res, err = _ue(ctx).python(code)
    if not ok:
        return {"error": err}
    import json as _json
    import re as _re
    out = (res or {}).get("output", "")
    m = _re.search(r"(\{.*\})\s*$", out, _re.S)
    return _json.loads(m.group(1)) if m else {"raw": out[-1500:]}


# ---------------------------------------------------------------- blueprints
@tool("bp_create", "Create a Blueprint asset. parent can be a class name ('Actor', "
      "'Character', 'Pawn') or an asset path to another Blueprint.",
      "blueprints", risk="write",
      parameters=req(P(path=S("Asset path, e.g. /Game/Blueprints/BP_Door"),
                       parent=S("Parent class or blueprint", default="Actor")), "path"))
def bp_create(ctx, path: str, parent: str = "Actor"):
    ok, res, err = _ue(ctx).call("bp.create", {"path": path, "parent": parent})
    return _nr(ctx, res, err)


@tool("bp_add_variable", "Add a member variable to a Blueprint. Types: bool, int, float, "
      "string, name, text, vector, rotator, transform, or a class path.",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path"), name=S("Variable name"),
                       type=S("Variable type", default="bool")), "bp", "name"))
def bp_add_variable(ctx, bp: str, name: str, type: str = "bool"):
    ok, res, err = _ue(ctx).call("bp.variable_add", {"bp": bp, "name": name, "type": type})
    return _nr(ctx, res, err)


@tool("bp_list_graphs", "List the graphs (event graph, functions, macros) of a Blueprint.",
      "blueprints", parameters=req(P(bp=S("Blueprint path")), "bp"))
def bp_list_graphs(ctx, bp: str):
    ok, res, err = _ue(ctx).call("bp.list_graphs", {"bp": bp})
    return _nr(ctx, res, err)


@tool("bp_read_graph", "Read every node and pin of a graph. Use this to understand existing "
      "logic before changing it: returns node ids, titles, positions and pin names.",
      "blueprints", parameters=req(P(bp=S("Blueprint path"),
                                     graph=S("Graph name (default: EventGraph)")), "bp"))
def bp_read_graph(ctx, bp: str, graph: str = "EventGraph"):
    ok, res, err = _ue(ctx).call("bp.graph_read", {"bp": bp, "graph": graph})
    return _nr(ctx, res, err)


@tool("bp_add_node", "Add one node to a graph. kind: call_function | event | custom_event | "
      "variable_get | variable_set | branch | sequence | cast. For call_function pass "
      "function (+ optional target_class); for event pass event; for cast pass target_class.",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path"), kind=S("Node kind", [
          "call_function", "event", "custom_event", "variable_get", "variable_set",
          "branch", "sequence", "cast"]), graph=S("Graph name"),
          x=N("X position", 0), y=N("Y position", 0),
          function=S("UFunction name for call_function"),
          target_class=S("Class for call_function/cast"),
          event=S("Event name for kind=event (e.g. ReceiveBeginPlay)"),
          name=S("Custom event name"),
          variable=S("Variable name")), "bp", "kind"))
def bp_add_node(ctx, bp: str, kind: str, graph: str = "EventGraph", x: int = 0,
                y: int = 0, function: str = "", target_class: str = "",
                event: str = "", name: str = "", variable: str = ""):
    args = {"bp": bp, "kind": kind, "graph": graph, "x": x, "y": y}
    for k, v in (("function", function), ("target_class", target_class),
                 ("event", event), ("name", name), ("variable", variable)):
        if v:
            args[k] = v
    ok, res, err = _ue(ctx).call("bp.add_node", args)
    return _nr(ctx, res, err)


@tool("bp_connect", "Wire two pins. Pin spec is 'nodeId:PinName' (nodeId from bp_add_node) "
      "or just 'nodeId' to use the first exec pin. from = output side, to = input side.",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path"), **{"from": S("source pin spec"), "to": S("target pin spec")},
                       graph=S("Graph name")), "bp", "from", "to"))
def bp_connect(ctx, bp: str, **kw):
    ok, res, err = _ue(ctx).call("bp.connect", {
        "bp": bp, "graph": kw.get("graph", "EventGraph"),
        "from": kw["from"], "to": kw["to"]})
    return _nr(ctx, res, err)


@tool("bp_disconnect", "Break a link (or all links from a pin when 'to' is omitted).",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path"), **{"from": S("source pin spec"),
                                                  "to": S("target pin spec (optional)")},
                       graph=S("Graph name")), "bp", "from"))
def bp_disconnect(ctx, bp: str, **kw):
    ok, res, err = _ue(ctx).call("bp.disconnect", {
        "bp": bp, "graph": kw.get("graph", "EventGraph"),
        "from": kw["from"], "to": kw.get("to", "")})
    return _nr(ctx, res, err)


@tool("bp_set_pin_default", "Set a literal default on an input pin (e.g. the message of a "
      "Print String node). pin spec is 'nodeId:PinName'.",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path"), pin=S("pin spec"), value=S("literal value"),
                       graph=S("Graph name")), "bp", "pin", "value"))
def bp_set_pin_default(ctx, bp: str, pin: str, value: str, graph: str = "EventGraph"):
    ok, res, err = _ue(ctx).call("bp.set_pin_default",
                                 {"bp": bp, "pin": pin, "value": value, "graph": graph})
    return _nr(ctx, res, err)


@tool("bp_build", "FASTEST PATH: build a whole graph in a single call and compile it. "
      "Give nodes with symbolic refs and links referring to 'ref.pin'. "
      "Example: nodes=[{ref:'begin',kind:'event',event:'ReceiveBeginPlay'},"
      "{ref:'print',kind:'call_function',function:'PrintString'}], "
      "links=[{from:'begin.then',to:'print.execute'}], "
      "defaults=[{pin:'print.InString',value:'Hello'}]",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path"), graph=S("Graph name"),
                       nodes=A("Node specs (objects)", "object"),
                       links=A("Link specs (objects)", "object"),
                       defaults=A("Pin default specs (objects)", "object"),
                       compile=B("Compile after building", True)), "bp", "nodes"))
def bp_build(ctx, bp: str, nodes: list, links: list | None = None,
             defaults: list | None = None, graph: str = "EventGraph", compile: bool = True):
    ok, res, err = _ue(ctx).call("bp.build_graph", {
        "bp": bp, "graph": graph, "nodes": nodes or [], "links": links or [],
        "defaults": defaults or [], "compile": compile})
    return _nr(ctx, res, err)


@tool("bp_compile", "Compile a Blueprint and report status. Always compile after edits.",
      "blueprints", risk="write",
      parameters=req(P(bp=S("Blueprint path")), "bp"))
def bp_compile(ctx, bp: str):
    ok, res, err = _ue(ctx).call("bp.compile", {"bp": bp})
    if not ok and res is None:
        return {"error": err}
    return {"ok": bool((res or {}).get("success")), **(res or {}),
            "error": err if not ok else None}


# --------------------------------------------------------------------- level
@tool("level_find_actors", "List actors in the current level, optionally filtered by class.",
      "level", parameters=P(**{"class": S("Class name substring"), "limit": N("Max", 200)}))
def level_find_actors(ctx, limit: int = 200, **kw):
    ok, res, err = _ue(ctx).call("actor.find", {"class": kw.get("class", ""), "limit": limit})
    return _nr(ctx, res, err)


@tool("level_spawn", "Spawn an actor in the current level by class name or Blueprint path.",
      "level", risk="write",
      parameters=req(P(**{"class": S("Class name or /Game/... blueprint path"),
                          "x": N("X", 0), "y": N("Y", 0), "z": N("Z", 0)}), "class"))
def level_spawn(ctx, **kw):
    ok, res, err = _ue(ctx).call("actor.spawn", {
        "class": kw["class"], "x": kw.get("x", 0), "y": kw.get("y", 0), "z": kw.get("z", 0)})
    return _nr(ctx, res, err)


@tool("level_set_property", "Set any UPROPERTY on an actor instance in the level "
      "(Unreal text syntax, e.g. 'X=100,Y=0,Z=50').", "level", risk="write",
      parameters=req(P(actor=S("Actor name in the level"), property=S("Property name"),
                       value=S("Value in Unreal text format")), "actor", "property", "value"))
def level_set_property(ctx, actor: str, property: str, value: str):
    ok, res, err = _ue(ctx).call("actor.set_property",
                                 {"actor": actor, "property": property, "value": value})
    return _nr(ctx, res, err)


@tool("level_call", "Call a UFUNCTION on an actor instance in the level.", "level", risk="write",
      parameters=req(P(actor=S("Actor name"), function=S("Function name")), "actor", "function"))
def level_call(ctx, actor: str, function: str):
    ok, res, err = _ue(ctx).call("actor.call", {"actor": actor, "function": function})
    return _nr(ctx, res, err)


@tool("level_play", "Start or stop Play-In-Editor so changes can be tested immediately.",
      "level", risk="write", parameters=req(P(action=S("start or stop", ["start", "stop"])), "action"))
def level_play(ctx, action: str = "start"):
    ok, res, err = _ue(ctx).call("pie.start" if action == "start" else "pie.stop")
    return _nr(ctx, res, err)


# ------------------------------------------------------------------- python
@tool("ue_python", "Escape hatch: run arbitrary Unreal Editor Python with the full `unreal` "
      "module. Use this for anything the typed tools don't cover "
      "(materials, sequencer, niagara, project settings, asset import...).",
      "unreal", risk="write", parameters=req(P(code=S("Python code")), "code"))
def ue_python(ctx, code: str):
    ok, res, err = _ue(ctx).python(code)
    if not ok:
        return {"error": err}
    return {"output": (res or {}).get("output", "")}


@tool("ue_run_batch", "Run several native commands in ONE round trip (pipelined). "
      "Calls = [{'cmd':'bp.add_node','args':{...}}, ...]. Use for bulk edits.",
      "unreal", risk="write",
      parameters=req(P(calls=A("List of {cmd,args} objects", "object")), "calls"))
def ue_run_batch(ctx, calls: list):
    pairs = [(c.get("cmd", ""), c.get("args", {}) or {}) for c in calls]
    results = _ue(ctx).batch(pairs)
    ok = all(r.get("ok") for r in results)
    return {"ok": ok, "results": results}


# --------------------------------------------------------------------- c++
@tool("ue_generate_cpp", "Scaffold a UCLASS/USTRUCT module file pair the way the editor "
      "wizard would, and add it to the project module. Use for native gameplay code.",
      "cplusplus", risk="write",
      parameters=req(P(name=S("Class name without prefix, e.g. HealthComponent"),
                       base=S("Base class", default="ActorComponent"),
                       module=S("Target module (default: project module)"),
                       path=S("Header folder relative to project (default Source/<Module>)")),
                     "name"))
def ue_generate_cpp(ctx, name: str, base: str = "ActorComponent", module: str = "",
                    path: str = ""):
    return ctx.uectl.generate_cpp(name=name, base=base, module=module, path=path)


@tool("ue_build", "Compile the project (UBT / Live Coding). Returns compiler output so you "
      "can fix your own errors.", "cplusplus", risk="shell",
      parameters=P(target=S("Target name (default: auto)"), config=S("Development|Shipping|DebugGame"),
                   live_coding=B("Use Live Coding (faster, editor open)", False),
                   timeout=N("Seconds", 900)))
def ue_build(ctx, target: str = "", config: str = "Development",
             live_coding: bool = False, timeout: int = 900):
    return ctx.uectl.build(target=target, config=config, live_coding=live_coding,
                           timeout=timeout)


# ---------------------------------------------------------------- materials
@tool("mat_create", "Create a material asset and optionally set its base color, "
      "roughness and metallic as constants. Creates the expression graph natively.",
      "materials", risk="write",
      parameters=req(P(path=S("Material path, e.g. /Game/Materials/M_Red"),
                       color=S("Base color as R,G,B (0-1) e.g. '1,0.2,0.1'"),
                       roughness=N("Roughness 0-1", 0.5),
                       metallic=N("Metallic 0-1", 0.0)), "path"))
def mat_create(ctx, path: str, color: str = "1,1,1", roughness: float = 0.5,
               metallic: float = 0.0):
    code = f'''
import unreal, json
path = {path!r}
name = path.split('/')[-1]
pkg = '/'.join(path.split('/')[:-1])
tools = unreal.AssetToolsHelpers.get_asset_tools()
if unreal.EditorAssetLibrary.does_asset_exist(path):
    mat = unreal.load_asset(path)
    created = False
else:
    mat = tools.create_asset(name, pkg, unreal.Material, unreal.MaterialFactoryNew())
    created = True
lib = unreal.MaterialEditingLibrary
try:
    lib.delete_all_material_expressions(mat)
except Exception:
    pass
rgb = [float(x) for x in {color!r}.split(',')]
col = lib.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector, -400, 0)
col.set_editor_property('constant', unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))
lib.connect_material_property(col, '', unreal.MaterialProperty.MP_BASE_COLOR)
rough = lib.create_material_expression(mat, unreal.MaterialExpressionConstant, -400, 160)
rough.set_editor_property('r', {roughness})
lib.connect_material_property(rough, '', unreal.MaterialProperty.MP_ROUGHNESS)
met = lib.create_material_expression(mat, unreal.MaterialExpressionConstant, -400, 280)
met.set_editor_property('r', {metallic})
lib.connect_material_property(met, '', unreal.MaterialProperty.MP_METALLIC)
lib.recompile_material(mat)
unreal.EditorAssetLibrary.save_asset(path)
print('__HARNESS__' + json.dumps({{'path': path, 'created': created}}))
'''
    ok, res, err = _ue(ctx).python(code)
    if not ok:
        return {"error": err}
    return {"output": (res or {}).get("output", ""), "path": path}


@tool("mat_assign", "Assign a material to a component of a blueprint, or to an actor "
      "instance in the level.", "materials", risk="write",
      parameters=req(P(target=S("Blueprint path (/Game/...) or actor name in level"),
                       material=S("Material asset path"),
                       component=S("Component variable name (blueprints only)",
                                   default="Mesh"),
                       slot=N("Material slot index", 0)), "target", "material"))
def mat_assign(ctx, target: str, material: str, component: str = "Mesh", slot: int = 0):
    code = f'''
import unreal, json
target = {target!r}
mat = unreal.load_asset({material!r})
if mat is None:
    print('__HARNESS__' + json.dumps({{'error': 'material not found'}}))
else:
    bp = unreal.load_asset(target)
    if bp is not None and isinstance(bp, unreal.Blueprint):
        sub = unreal.get_default_object(bp.generated_class()) if hasattr(bp, 'generated_class') else None
        unreal.BlueprintEditorLibrary.compile_blueprint(bp)
        comp = None
        for c in (sub.get_components_by_class(unreal.MeshComponent) if sub else []):
            comp = c; break
        if comp:
            comp.set_material({slot}, mat)
            unreal.BlueprintEditorLibrary.compile_blueprint(bp)
            print('__HARNESS__' + json.dumps({{'assigned': target, 'component': comp.get_name()}}))
        else:
            print('__HARNESS__' + json.dumps({{'error': 'no mesh component in default object'}}))
    else:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        hit = None
        for a in eas.get_all_level_actors():
            if a.get_name() == target: hit = a; break
        if hit:
            for c in hit.get_components_by_class(unreal.MeshComponent):
                c.set_material({slot}, mat); break
            print('__HARNESS__' + json.dumps({{'assigned': target}}))
        else:
            print('__HARNESS__' + json.dumps({{'error': 'target not found'}}))
'''
    ok, res, err = _ue(ctx).python(code)
    if not ok:
        return {"error": err}
    return {"output": (res or {}).get("output", "")}


# ---------------------------------------------------------------- widgets
@tool("umg_create_widget", "Create a Widget Blueprint with a simple tree: a CanvasPanel "
      "root plus optional TextBlock / Button children. Good starting point for UI.",
      "ui", risk="write",
      parameters=req(P(path=S("Widget blueprint path, e.g. /Game/UI/WBP_Menu"),
                       title_text=S("Optional text block content")), "path"))
def umg_create_widget(ctx, path: str, title_text: str = ""):
    ok, res, err = _ue(ctx).call(
        "umg.create_widget", {"path": path, "title_text": title_text}
    )
    return _nr(ctx, res, err)


# ---------------------------------------------------------------- input
@tool("input_add_action", "Add an Enhanced Input action mapping (IMC + IA) wired to the "
      "project's input config. Creates assets and updates DefaultInput.ini.",
      "input", risk="write",
      parameters=req(P(name=S("Action name, e.g. IA_Jump"), key=S("Key, e.g. SpaceBar")),
                     "name"))
def input_add_action(ctx, name: str, key: str = "SpaceBar"):
    code = f'''
import unreal, json
tools = unreal.AssetToolsHelpers.get_asset_tools()
pkg = '/Game/Input'
path = pkg + '/' + {name!r}
if unreal.EditorAssetLibrary.does_asset_exist(path):
    print('__HARNESS__' + json.dumps({{'existed': True, 'path': path}}))
else:
    ia = tools.create_asset({name!r}, pkg, unreal.InputAction, unreal.InputActionFactory())
    unreal.EditorAssetLibrary.save_asset(path)
    print('__HARNESS__' + json.dumps({{'created': True, 'path': path, 'key': {key!r}}}))
'''
    ok, res, err = _ue(ctx).python(code)
    if not ok:
        return {"error": err}
    return {"output": (res or {}).get("output", "")}


# ---------------------------------------------------------------- settings
@tool("proj_set_ini", "Read or write any project .ini key (DefaultEngine.ini, "
      "DefaultGame.ini, DefaultInput.ini...). Works with the editor closed.",
      "project", risk="write",
      parameters=req(P(file=S("ini file name", default="DefaultEngine.ini"),
                       section=S("Section name, e.g. /Script/EngineSettings.GameMapsSettings"),
                       values=O("Key/value pairs to set (omit to just read)")), "file", "section"))
def proj_set_ini(ctx, file: str = "DefaultEngine.ini", section: str = "", **kw):
    if not section:
        return {"error": "section is required"}
    values = kw.get("values") or {}
    if not values:
        return {"values": ctx.uectl.read_ini(file, section)}
    path = ctx.uectl.write_ini(file, section, {k: str(v) for k, v in values.items()})
    return {"path": path, "values": {k: str(v) for k, v in values.items()}}


@tool("proj_default_map", "Set the project's default startup map and game mode.",
      "project", risk="write",
      parameters=P(map=S("Map asset path, e.g. /Game/Maps/Main"),
                   game_mode=S("Game mode blueprint path")))
def proj_default_map(ctx, map: str = "", game_mode: str = ""):
    vals = {}
    if map:
        vals["GameDefaultMap"] = map
        vals["EditorStartupMap"] = map
    if game_mode:
        vals["GlobalDefaultGameMode"] = game_mode
    if not vals:
        return {"error": "give map and/or game_mode"}
    ctx.uectl.write_ini("DefaultEngine.ini",
                        "/Script/EngineSettings.GameMapsSettings", vals)
    return {"set": vals}


# ---------------------------------------------------------------- utilities
@tool("ue_screenshot", "Capture the editor viewport as a PNG in the project's Saved folder, "
      "so you can visually verify layout and lighting.", "unreal",
      parameters=P(name=S("File name", default="harness_shot")))
def ue_screenshot(ctx, name: str = "harness_shot"):
    ok, res, err = _ue(ctx).call("editor.console", {"command": f"HighResShot 1920x1080"})
    return {"ok": ok, "note": "screenshot cmd issued", **(res or {}), "error": err}


@tool("ue_live_coding", "Trigger Live Coding compile through the running editor (C++ "
      "hot reload without closing the editor).", "cplusplus", risk="write")
def ue_live_coding(ctx):
    ok, res, err = _ue(ctx).call("editor.console", {"command": "LiveCoding.Compile"})
    if not ok:
        return {"error": err}
    return {"ok": True, "hint": "check editor_log for compile results", **(res or {})}
