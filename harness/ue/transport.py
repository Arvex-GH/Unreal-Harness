"""Transport layer: how the harness talks to the Unreal Editor.

Priority (all local, all direct - no MCP in the loop):

  1. BRIDGE  - persistent TCP JSON-lines socket into our C++ editor plugin.
               ~0.1-2 ms per call, pipelinable, batches an entire blueprint
               construction into one editor tick.
  2. RC      - stock "Remote Control API" plugin over HTTP :30010 plus the
               Python Editor Script plugin for anything not exposed as a UFUNCTION.
  3. DISK    - no editor at all: read/write .uproject, .ini, C++, Python files.

`auto` picks the fastest available and transparently degrades. Every transport
returns (ok: bool, payload, error: str|None).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import httpx

from ..config import Config


class TransportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 1. Native bridge
# ---------------------------------------------------------------------------
class BridgeTransport:
    """Persistent, pipelined socket to the UEHarness C++ editor plugin."""

    name = "bridge"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._next_id = 1
        self.handshake: dict[str, Any] = {}
        self.latency_ms: float | None = None

    # -- connection ---------------------------------------------------
    def connect(self, timeout: float = 1.5) -> bool:
        with self._lock:
            if self._sock:
                return True
            try:
                s = socket.create_connection(
                    (self.cfg.bridge_host, self.cfg.bridge_port), timeout=timeout
                )
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(60.0)
                self._sock = s
                self._file = s.makefile("rwb")
                self.handshake = self._raw("handshake", {}) or {}
                return True
            except OSError:
                self._close_locked()
                return False

    def _close_locked(self) -> None:
        try:
            if self._file:
                self._file.close()
        except Exception:
            pass
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._file = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # -- io -----------------------------------------------------------
    def _raw(self, cmd: str, args: dict[str, Any], flags: int = 0) -> dict[str, Any]:
        if not self._sock or not self._file:
            raise TransportError("bridge not connected")
        call_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"id": call_id, "cmd": cmd, "args": args, "flags": flags},
                             separators=(",", ":")) + "\n"
        t0 = time.perf_counter()
        self._file.write(payload.encode("utf-8"))
        self._file.flush()
        line = self._file.readline()
        if not line:
            self._close_locked()
            raise TransportError("bridge closed the connection")
        reply = json.loads(line.decode("utf-8"))
        self.latency_ms = (time.perf_counter() - t0) * 1000.0
        if not reply.get("ok"):
            raise TransportError(reply.get("error") or "unknown bridge error")
        return reply.get("result") or {}

    def call(self, cmd: str, args: dict[str, Any] | None = None, flags: int = 0):
        with self._lock:
            try:
                return True, self._raw(cmd, args or {}, flags), None
            except (TransportError, OSError, ValueError) as e:
                return False, None, str(e)

    def batch(self, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """Pipeline N commands in one write. Returns list of {'ok','result'/'error'}."""
        with self._lock:
            if not self._sock or not self._file:
                return [{"ok": False, "error": "bridge not connected"} for _ in calls]
            ids, buf = [], []
            for cmd, args in calls:
                call_id = self._next_id
                self._next_id += 1
                ids.append(call_id)
                buf.append(json.dumps({"id": call_id, "cmd": cmd, "args": args},
                                      separators=(",", ":")))
            try:
                self._file.write(("\n".join(buf) + "\n").encode("utf-8"))
                self._file.flush()
                out = []
                for _ in ids:
                    line = self._file.readline()
                    if not line:
                        raise TransportError("bridge closed during batch")
                    out.append(json.loads(line.decode("utf-8")))
                return out
            except (TransportError, OSError, ValueError) as e:
                self._close_locked()
                return [{"ok": False, "error": str(e)} for _ in calls]

    def ping(self) -> bool:
        if not self.connect():
            return False
        ok, _, _ = self.call("ping")
        return ok


class RemotePythonDisabledError(TransportError):
    """Raised when Remote Control is reachable but bEnableRemotePythonExecution is false in UE."""


# ---------------------------------------------------------------------------
# 2. Remote Control API + Python editor scripting
# ---------------------------------------------------------------------------
class RemoteControlTransport:
    """Stock-plugin transport. Works with zero C++ compilation in the project."""

    name = "rc"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = f"http://{cfg.rc_host}:{cfg.rc_port}"
        self._client = httpx.Client(timeout=15.0)
        self._exec_id = 0
        self.available: bool | None = None

    def ping(self) -> bool:
        try:
            r = self._client.get(f"{self.base}/remote/info", timeout=1.5)
            self.available = r.status_code == 200
            return self.available
        except Exception:
            self.available = False
            return False

    def _put(self, path: str, body: dict[str, Any]) -> Any:
        r = self._client.put(f"{self.base}{path}", json=body)
        if r.status_code >= 400:
            txt = r.text[:400]
            if "Default__PythonScriptLibrary cannot be accessed remotely" in txt:
                auto_note = self._auto_enable_remote_python()
                raise RemotePythonDisabledError(
                    "Unreal Remote Control blocked Default__PythonScriptLibrary because "
                    "'bEnableRemotePythonExecution' was disabled in the running editor. "
                    f"{auto_note}"
                )
            raise TransportError(f"RC {r.status_code}: {txt}")
        try:
            return r.json()
        except Exception:
            return {}

    def _auto_enable_remote_python(self) -> str:
        try:
            from .project import Installer

            info = Installer(self.cfg).configure_remote_control()
            if info.get("configured"):
                return (
                    f"Unreal Harness automatically enabled bEnableRemotePythonExecution=True in "
                    f"{info.get('remote_control_ini')}. Restart Unreal Editor once to activate it "
                    "(or in Unreal Editor open Edit > Project Settings > Plugins > Remote Control, "
                    "temporarily tick 'Restrict Server Access', tick 'Enable Remote Python Execution', "
                    "then untick 'Restrict Server Access')."
                )
        except Exception:
            pass
        return (
            "Enable 'bEnableRemotePythonExecution=True' under "
            "[/Script/RemoteControlCommon.RemoteControlSettings] in Config/DefaultRemoteControl.ini "
            "and restart Unreal Editor."
        )

    def call_object(
        self,
        object_path: str,
        function_name: str,
        parameters: dict[str, Any] | None = None,
        transaction: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "objectPath": object_path,
            "functionName": function_name,
            "generateTransaction": transaction,
        }
        if parameters:
            body["parameters"] = parameters
        data = self._put("/remote/object/call", body)
        return data if isinstance(data, dict) else {}

    def set_property(self, object_path: str, property_name: str, value: Any) -> None:
        self._put(
            "/remote/object/property",
            {
                "objectPath": object_path,
                "propertyName": property_name,
                "propertyValue": {property_name: value},
                "access": "WRITE_ACCESS",
            },
        )

    def create_widget_blueprint(self, path: str, title_text: str = "") -> dict[str, Any]:
        """Direct Remote Control WidgetBlueprint creation without Default__PythonScriptLibrary."""
        pkg_name = path.split(".")[0].rstrip("/")
        if not pkg_name.startswith("/"):
            pkg_name = "/" + pkg_name
        parts = pkg_name.rsplit("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise TransportError(f"invalid widget path: {path}")
        pkg_dir, asset_name = parts[0], parts[1]
        obj_path = f"{pkg_name}.{asset_name}"

        exists_res = self.call_object(
            "/Script/EditorScriptingUtilities.Default__EditorAssetLibrary",
            "DoesAssetExist",
            {"AssetPath": pkg_name},
        )
        if exists_res.get("ReturnValue") is True:
            return {"ok": True, "existed": True, "created": False, "path": pkg_name}

        umg_settings = "/Script/UMGEditor.Default__UMGEditorProjectSettings"
        set_root_ok = False
        try:
            self.set_property(umg_settings, "DefaultRootWidget", "/Script/UMG.CanvasPanel")
            set_root_ok = True
        except Exception:
            pass

        try:
            tools_path = "/Engine/Transient.AssetToolsImpl_0"
            try:
                at_res = self.call_object(
                    "/Script/AssetTools.Default__AssetToolsHelpers", "GetAssetTools"
                )
                rv = at_res.get("ReturnValue")
                if isinstance(rv, str) and rv.startswith("/"):
                    tools_path = rv
            except Exception:
                pass

            self.call_object(
                tools_path,
                "CreateAsset",
                {
                    "AssetName": asset_name,
                    "PackagePath": pkg_dir,
                    "AssetClass": "/Script/UMGEditor.WidgetBlueprint",
                    "Factory": "/Script/UMGEditor.Default__WidgetBlueprintFactory",
                },
                transaction=True,
            )
        finally:
            if set_root_ok:
                try:
                    self.set_property(umg_settings, "DefaultRootWidget", "None")
                except Exception:
                    pass

        try:
            self.call_object(
                "/Script/BlueprintEditorLibrary.Default__BlueprintEditorLibrary",
                "CompileBlueprint",
                {"Blueprint": obj_path},
            )
        except Exception:
            pass

        self.call_object(
            "/Script/EditorScriptingUtilities.Default__EditorAssetLibrary",
            "SaveAsset",
            {"AssetToSave": pkg_name, "bOnlyIfIsDirty": False},
        )

        verify = self.call_object(
            "/Script/EditorScriptingUtilities.Default__EditorAssetLibrary",
            "DoesAssetExist",
            {"AssetPath": pkg_name},
        )
        if verify.get("ReturnValue") is not True:
            raise TransportError(f"failed to create WidgetBlueprint at {pkg_name} via Remote Control")

        out: dict[str, Any] = {
            "ok": True,
            "created": True,
            "existed": False,
            "path": pkg_name,
            "root_widget": "CanvasPanel",
            "transport": "rc_direct",
        }
        if title_text:
            note = self._auto_enable_remote_python()
            out["note"] = (
                f"Created {pkg_name} with CanvasPanel root via Remote Control. "
                f"To populate child TextBlock ('{title_text}') without the C++ bridge, "
                f"Remote Python has been enabled in DefaultRemoteControl.ini — {note}"
            )
        return out

    def python(self, code: str) -> str:
        """Run Python inside the editor via the Python Editor Script plugin.

        We go through the Remote Control object-call endpoint so no extra
        listener has to be started by the user.
        """
        self._exec_id += 1
        body = {
            "objectPath": "/Script/PythonScriptPlugin.Default__PythonScriptLibrary",
            "functionName": "ExecutePythonCommandEx",
            "parameters": {"PythonCommand": code, "ExecutionMode": "ExecuteFile",
                           "FileExecutionScope": "Private"},
            "generateTransaction": False,
        }
        data = self._put("/remote/object/call", body)
        result = (data or {}).get("ReturnValue") or {}
        out = (result.get("CommandResult") or "") + "\n" + (result.get("LogOutput") or "")
        if result.get("CommandResult") is None and not out.strip():
            raise TransportError(f"python execution failed: {data}")
        return out

    def call(self, cmd: str, args: dict[str, Any] | None = None, flags: int = 0):
        """Adapts the native verb surface onto python/RC calls (slower, but portable)."""
        args = args or {}
        try:
            code = _rc_shim(cmd, args)
            out = self.python(f"import unreal, json\n{_PREAMBLE}\n{code}")
            for line in reversed(out.splitlines()):
                if "__HARNESS__" in line:
                    payload_str = line.split("__HARNESS__", 1)[1].strip()
                    try:
                        parsed = json.loads(payload_str)
                        if isinstance(parsed, dict):
                            ok_flag = bool(parsed.pop("_ok", True))
                            return ok_flag, parsed, (parsed.get("error") if not ok_flag else None)
                    except Exception:
                        pass
            return True, {"output": out}, None
        except RemotePythonDisabledError as e:
            if cmd == "umg.create_widget":
                try:
                    res = self.create_widget_blueprint(
                        args.get("path", ""), args.get("title_text", "")
                    )
                    return True, res, None
                except TransportError:
                    pass
            return False, None, str(e)
        except TransportError as e:
            return False, None, str(e)


_PREAMBLE = """
def _hr(ok, **kw):
    d = {'_ok': bool(ok)}; d.update(kw)
    print('__HARNESS__' + json.dumps(d))
"""


_RC_SHIMS: dict[str, str] = {
    "editor.info": "print('__HARNESS__' + json.dumps({'project': unreal.SystemLibrary.get_project_name(), 'engine': unreal.SystemLibrary.get_engine_version()}))",
    "editor.save_all": "_hr(unreal.EditorAssetLibrary.save_directory('/Game', only_if_is_dirty=False, recursive=True))",
    "asset.search": """
q = str(_args.get('query', '')); cls = str(_args.get('class', '')); lim = int(_args.get('limit', 50))
ar = unreal.AssetRegistryHelpers.get_asset_registry()
f = unreal.ARFilter(package_paths=[str(_args.get('path') or '/Game')], recursive_paths=True)
res = []
for a in ar.get_assets(f):
    n = str(a.asset_name)
    c_name = str(a.asset_class_path.asset_name)
    if q and q.lower() not in n.lower(): continue
    if cls and cls.lower() not in c_name.lower(): continue
    res.append({'name': n, 'path': str(a.object_path), 'class': c_name})
    if len(res) >= lim: break
_hr(True, assets=res, count=len(res))
""",
    "bp.list_graphs": """
bp = unreal.load_asset(str(_args.get('bp', '')))
_hr(bp is not None, graphs=[g.get_name() for g in bp.get_editor_property('function_graphs')] + [g.get_name() for g in bp.get_editor_property('ubergraph_pages')])
""",
    "bp.compile": """
bp = unreal.load_asset(str(_args.get('bp', '')))
unreal.BlueprintEditorLibrary.compile_blueprint(bp)
_hr(True, success=True, status=str(bp.get_editor_property('status')))
""",
    "bp.create": """
p = str(_args.get('path', '')).split('.')[0].rstrip('/')
pkg, name = p.rsplit('/', 1)
if unreal.EditorAssetLibrary.does_asset_exist(p):
    _hr(True, path=p, existed=True, created=False)
else:
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    fac = unreal.BlueprintFactory()
    parent_cls = getattr(unreal, str(_args.get('parent') or 'Actor'), unreal.Actor)
    fac.set_editor_property('parent_class', parent_cls)
    bp = tools.create_asset(name, pkg, unreal.Blueprint, fac)
    unreal.EditorAssetLibrary.save_asset(p)
    _hr(bp is not None, path=p, created=bp is not None, existed=False)
""",
    "umg.create_widget": """
p = str(_args.get('path', '')).split('.')[0].rstrip('/')
pkg, name = p.rsplit('/', 1)
title_text = str(_args.get('title_text', ''))
created = False
if unreal.EditorAssetLibrary.does_asset_exist(p):
    wbp = unreal.load_asset(p)
else:
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    umg_cfg = unreal.get_default_object(unreal.UMGEditorProjectSettings)
    old_root = umg_cfg.get_editor_property('default_root_widget')
    umg_cfg.set_editor_property('default_root_widget', unreal.CanvasPanel)
    try:
        fac = unreal.WidgetBlueprintFactory()
        fac.set_editor_property('parent_class', unreal.UserWidget)
        wbp = tools.create_asset(name, pkg, unreal.WidgetBlueprint, fac)
        created = wbp is not None
    finally:
        umg_cfg.set_editor_property('default_root_widget', old_root)

if wbp is not None and title_text:
    wt = unreal.find_object(wbp, 'WidgetTree')
    if wt is not None:
        canvas = unreal.find_object(wt, 'CanvasPanel_0') or unreal.find_object(wt, 'RootCanvas')
        if canvas is not None:
            txt = unreal.find_object(wt, 'TitleText')
            if txt is None:
                txt = unreal.new_object(unreal.TextBlock, outer=wt, name='TitleText')
                slot = canvas.add_child_to_canvas(txt)
                if slot is not None:
                    try:
                        slot.set_auto_size(True)
                        slot.set_alignment(unreal.Vector2D(0.5, 0.5))
                    except Exception:
                        pass
            if txt is not None:
                txt.set_text(title_text)

if wbp is not None:
    try:
        unreal.BlueprintEditorLibrary.compile_blueprint(wbp)
    except Exception:
        pass
    unreal.EditorAssetLibrary.save_asset(p)
_hr(wbp is not None, path=p, created=created, existed=not created, title_text=title_text)
""",
    "bp.variable_add": """
bp = unreal.load_asset(str(_args.get('bp', '')))
t = unreal.BlueprintEditorLibrary.get_basic_type_by_name(str(_args.get('type') or 'bool'))
ok = unreal.BlueprintEditorLibrary.add_member_variable(bp, str(_args.get('name', '')), t)
_hr(ok)
""",
    "editor.console": """
w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
unreal.SystemLibrary.execute_console_command(w, _args.get('command', ''))
_hr(True, output='executed')
""",
    "level.new": """
p = (_args.get('path') or '/Game/Maps/NewLevel').split('.')[0].rstrip('/')
add_floor = bool(_args.get('add_floor', True))
save = bool(_args.get('save', True))
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ok = les.new_level(p)
if ok and add_floor:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    floor = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0, 0, -50), unreal.Rotator(0, 0, 0))
    if floor is not None:
        cube_mesh = unreal.load_asset('/Engine/BasicShapes/Cube')
        if cube_mesh is not None:
            floor.static_mesh_component.set_static_mesh(cube_mesh)
        floor.set_actor_scale3d(unreal.Vector(50, 50, 1))
        floor.set_actor_label('Floor')
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
cam_loc = unreal.Vector(420, -180, 170)
cam_rot = unreal.MathLibrary.find_look_at_rotation(cam_loc, unreal.Vector(0, 0, 50))
ues.set_level_viewport_camera_info(cam_loc, cam_rot)
if ok and save:
    try:
        les.save_current_level()
    except Exception:
        unreal.EditorAssetLibrary.save_asset(p)
_hr(ok, path=p, floor_added=add_floor)
""",
    "level.camera": """
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
cur_ok, cur_loc, cur_rot = ues.get_level_viewport_camera_info()
if not cur_ok:
    cur_loc, cur_rot = unreal.Vector(0, 0, 150), unreal.Rotator(0, 0, 0)
if any(k in _args for k in ('x', 'y', 'z', 'pitch', 'yaw', 'roll', 'look_x', 'look_y', 'look_z')) or _args.get('set'):
    loc = unreal.Vector(
        float(_args.get('x', cur_loc.x)),
        float(_args.get('y', cur_loc.y)),
        float(_args.get('z', cur_loc.z)),
    )
    if any(k in _args for k in ('look_x', 'look_y', 'look_z')):
        target = unreal.Vector(
            float(_args.get('look_x', 0.0)),
            float(_args.get('look_y', 0.0)),
            float(_args.get('look_z', 0.0)),
        )
        rot = unreal.MathLibrary.find_look_at_rotation(loc, target)
    else:
        rot = unreal.Rotator(
            float(_args.get('roll', cur_rot.roll)),
            float(_args.get('pitch', cur_rot.pitch)),
            float(_args.get('yaw', cur_rot.yaw)),
        )
    ues.set_level_viewport_camera_info(loc, rot)
    unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_invalidate_viewports()
    cur_loc, cur_rot = loc, rot
_hr(True, location=f'X={cur_loc.x:.1f} Y={cur_loc.y:.1f} Z={cur_loc.z:.1f}',
    rotation=f'P={cur_rot.pitch:.1f} Y={cur_rot.yaw:.1f} R={cur_rot.roll:.1f}')
""",
    "viewport.screenshot": """
import os
name = (_args.get('name') or 'harness_shot').strip() or 'harness_shot'
if name.lower().endswith('.png'):
    name = name[:-4]
safe = ''.join(c if (c.isalnum() or c in ('-', '_')) else '_' for c in name) or 'harness_shot'
out_dir = os.path.abspath(os.path.join(unreal.Paths.project_saved_dir(), 'Screenshots', 'Harness'))
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, safe + '.png')
try:
    if os.path.exists(out_path):
        os.remove(out_path)
except Exception:
    pass
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
try:
    les.editor_set_viewport_realtime(True)
    les.editor_invalidate_viewports()
except Exception:
    pass
task_started = False
try:
    unreal.AutomationLibrary.take_high_res_screenshot(1280, 720, out_path, force_game_view=False)
    task_started = True
except Exception:
    pass
if not task_started:
    w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    unreal.SystemLibrary.execute_console_command(w, f'HighResShot 1280x720 filename="{out_path}"')
try:
    les.editor_invalidate_viewports()
except Exception:
    pass
_hr(True, path=out_path, queued=True)
""",
    "actor.find": """
cls = str(_args.get('class', '')); lim = int(_args.get('limit', 50))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
res = []
for a in eas.get_all_level_actors():
    if cls and cls.lower() not in a.get_class().get_name().lower() and cls.lower() not in a.get_actor_label().lower(): continue
    res.append({'name': a.get_name(), 'label': a.get_actor_label(), 'class': a.get_class().get_name(), 'path': a.get_path_name()})
    if len(res) >= lim: break
_hr(True, actors=res)
""",
    "actor.spawn": """
cls_name = str(_args.get('class', 'Actor')).strip()
shapes = {'cube': 'Cube', 'sphere': 'Sphere', 'cylinder': 'Cylinder', 'cone': 'Cone', 'plane': 'Plane'}
shape_asset = shapes.get(cls_name.lower())
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
loc = unreal.Vector(float(_args.get('x', 0)), float(_args.get('y', 0)), float(_args.get('z', 0)))
rot = unreal.Rotator(float(_args.get('roll', 0)), float(_args.get('pitch', 0)), float(_args.get('yaw', 0)))
actor = None
if shape_asset:
    actor = eas.spawn_actor_from_class(unreal.StaticMeshActor, loc, rot)
    if actor is not None:
        try:
            actor.root_component.set_editor_property('mobility', unreal.ComponentMobility.MOVABLE)
        except Exception:
            pass
        m = unreal.load_asset(f'/Engine/BasicShapes/{shape_asset}')
        if m is not None:
            actor.static_mesh_component.set_static_mesh(m)
elif cls_name.startswith('/'):
    bp = unreal.load_asset(cls_name)
    if bp is not None:
        gen = bp.generated_class() if hasattr(bp, 'generated_class') else bp
        actor = eas.spawn_actor_from_class(gen, loc, rot)
else:
    ucls = getattr(unreal, cls_name, None) or getattr(unreal, cls_name.removeprefix('A'), None)
    if ucls is None:
        ucls = unreal.load_class(None, f'/Script/Engine.{cls_name.removeprefix("A")}')
    if ucls is not None:
        actor = eas.spawn_actor_from_class(ucls, loc, rot)

if actor is None:
    _hr(False, error=f'class not found or spawn failed: {cls_name}')
else:
    try:
        actor.root_component.set_editor_property('mobility', unreal.ComponentMobility.MOVABLE)
    except Exception:
        pass
    if any(k in _args for k in ('scale', 'sx', 'sy', 'sz')):
        u = float(_args.get('scale', 1.0))
        actor.set_actor_scale3d(unreal.Vector(
            float(_args.get('sx', u)), float(_args.get('sy', u)), float(_args.get('sz', u))
        ))
    if _args.get('label'):
        actor.set_actor_label(str(_args['label']))
    _hr(True, name=actor.get_name(), label=actor.get_actor_label(), path=actor.get_path_name())
""",
    "actor.set_property": """
import re as _re
aname = str(_args.get('actor', ''))
prop = str(_args.get('property', ''))
val_str = str(_args.get('value', ''))
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
hit = None
for a in eas.get_all_level_actors():
    if a.get_name() == aname or a.get_actor_label().lower() == aname.lower():
        hit = a
        break
if hit is None:
    _hr(False, error=f'actor not found: {aname}')
else:
    def _nums(s):
        return [float(x) for x in _re.findall(r'[-+]?\\d*\\.?\\d+', s)]
    pl = prop.lower()
    if pl in ('rotation', 'actorrotation'):
        ns = _nums(val_str)
        p_v = float(_re.search(r'P(?:itch)?\\s*=\\s*([-+]?\\d*\\.?\\d+)', val_str, _re.I).group(1)) if _re.search(r'P(?:itch)?\\s*=', val_str, _re.I) else (ns[0] if len(ns) > 0 else 0.0)
        y_v = float(_re.search(r'Y(?:aw)?\\s*=\\s*([-+]?\\d*\\.?\\d+)', val_str, _re.I).group(1)) if _re.search(r'Y(?:aw)?\\s*=', val_str, _re.I) else (ns[1] if len(ns) > 1 else 0.0)
        r_v = float(_re.search(r'R(?:oll)?\\s*=\\s*([-+]?\\d*\\.?\\d+)', val_str, _re.I).group(1)) if _re.search(r'R(?:oll)?\\s*=', val_str, _re.I) else (ns[2] if len(ns) > 2 else 0.0)
        try: hit.root_component.set_editor_property('mobility', unreal.ComponentMobility.MOVABLE)
        except Exception: pass
        hit.set_actor_rotation(unreal.Rotator(r_v, p_v, y_v), False)
        _hr(True, set=True, target=hit.get_name())
    elif pl in ('location', 'actorlocation'):
        ns = _nums(val_str) + [0.0, 0.0, 0.0]
        hit.set_actor_location(unreal.Vector(ns[0], ns[1], ns[2]), False, False)
        _hr(True, set=True, target=hit.get_name())
    elif pl in ('scale', 'actorscale3d'):
        ns = _nums(val_str)
        if len(ns) == 1: ns = ns * 3
        ns = (ns + [1.0, 1.0, 1.0])[:3]
        hit.set_actor_scale3d(unreal.Vector(ns[0], ns[1], ns[2]))
        _hr(True, set=True, target=hit.get_name())
    else:
        snake = _re.sub(r'(?<!^)(?=[A-Z])', '_', prop).lower()
        snake_no_b = snake[2:] if snake.startswith('b_') else snake
        cands = [prop, snake, snake_no_b]
        targets = [hit] + list(hit.get_components_by_class(unreal.ActorComponent))
        done = False
        last_err = ''
        for t_obj in targets:
            for pname in cands:
                try:
                    cur = t_obj.get_editor_property(pname)
                except Exception:
                    continue
                try:
                    if isinstance(cur, bool):
                        coerced = val_str.strip().lower() in ('1', 'true', 'yes', 'on')
                    elif isinstance(cur, int):
                        coerced = int(float(val_str))
                    elif isinstance(cur, float):
                        coerced = float(val_str)
                    elif isinstance(cur, unreal.Color):
                        ns = _nums(val_str) + [255, 255, 255, 255]
                        mx = max(ns[:3]) if ns else 1.0
                        if mx <= 1.0:
                            ns = [int(round(x * 255)) for x in ns[:4]]
                        else:
                            ns = [int(round(x)) for x in ns[:4]]
                        coerced = unreal.Color(b=ns[2], g=ns[1], r=ns[0], a=ns[3] if len(ns) > 3 else 255)
                    elif isinstance(cur, unreal.LinearColor):
                        ns = _nums(val_str) + [1.0, 1.0, 1.0, 1.0]
                        if max(ns[:3]) > 1.0:
                            ns = [x / 255.0 for x in ns[:3]] + [1.0]
                        coerced = unreal.LinearColor(ns[0], ns[1], ns[2], ns[3])
                    elif isinstance(cur, unreal.Vector):
                        ns = _nums(val_str) + [0.0, 0.0, 0.0]
                        coerced = unreal.Vector(ns[0], ns[1], ns[2])
                    else:
                        coerced = val_str
                    t_obj.set_editor_property(pname, coerced)
                    if hasattr(t_obj, 'recapture_sky'):
                        try: t_obj.recapture_sky()
                        except Exception: pass
                    done = True
                    _hr(True, set=True, target=t_obj.get_name(), property=pname)
                    break
                except Exception as ex:
                    last_err = str(ex)
            if done:
                break
        if not done:
            _hr(False, error=last_err or f'property {prop} not found on {aname} or its components')
""",
    "pie.start": "unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_play_simulate()\n_hr(True)",
    "pie.stop": "unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_request_end_play()\n_hr(True)",
}


def _rc_shim(cmd: str, args: dict[str, Any]) -> str:
    tmpl = _RC_SHIMS.get(cmd)
    if not tmpl:
        raise TransportError(
            f"'{cmd}' is only available on the native bridge transport. "
            "Install the UEHarness plugin (harness installer does this) or enable "
            "the Python Editor Script plugin for fallback coverage."
        )
    return f"_args = json.loads({json.dumps(args)!r})\n" + tmpl


# ---------------------------------------------------------------------------
# Unified client
# ---------------------------------------------------------------------------
class UnrealClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bridge = BridgeTransport(cfg)
        self.rc = RemoteControlTransport(cfg)
        self._active: str = "disk"
        self._last_probe = 0.0
        self.capabilities: set[str] = set()

    # -- discovery ----------------------------------------------------
    def probe(self, force: bool = False) -> str:
        now = time.time()
        if not force and now - self._last_probe < 2.0:
            return self._active
        self._last_probe = now
        if self.bridge.connect(timeout=1.0):
            self._active = "bridge"
            self.capabilities = set(self.bridge.handshake.get("verbs") or [])
            return self._active
        if self.rc.ping():
            self._active = "rc"
            self.capabilities = set(_RC_SHIMS)
            return self._active
        self._active = "disk"
        self.capabilities = set()
        return self._active

    @property
    def active(self) -> str:
        return self.probe()

    def status(self) -> dict[str, Any]:
        self.probe(force=True)
        return {
            "transport": self._active,
            "editor_connected": self._active in ("bridge", "rc"),
            "bridge": {
                "port": self.cfg.bridge_port,
                "latency_ms": round(self.bridge.latency_ms, 2) if self.bridge.latency_ms else None,
                "handshake": self.bridge.handshake,
                "verbs": len(self.capabilities),
            },
            "remote_control": {"port": self.cfg.rc_port, "reachable": self._active == "rc"},
            "project": str(self.cfg.project_file or ""),
            "capabilities": sorted(self.capabilities),
        }

    # -- calls --------------------------------------------------------
    def call(self, cmd: str, args: dict[str, Any] | None = None):
        """Route a native verb to the best transport. Returns (ok, result, error)."""
        args = args or {}
        mode = self.cfg.prefer_transport
        order = {"auto": ["bridge", "rc"], "bridge": ["bridge"],
                 "rc": ["rc"], "disk": []}.get(mode, ["bridge", "rc"])

        last_err = "no editor transport reachable"
        for t in order:
            if t == "bridge":
                if not self.bridge.connect():
                    last_err = f"bridge unreachable on {self.cfg.bridge_host}:{self.cfg.bridge_port}"
                    continue
                ok, res, err = self.bridge.call(cmd, args)
                if ok or "unknown command" not in (err or ""):
                    return ok, res, err
                last_err = err or last_err
            elif t == "rc":
                if not self.rc.ping():
                    last_err = f"remote control unreachable on :{self.cfg.rc_port}"
                    continue
                return self.rc.call(cmd, args)
        return False, None, last_err

    def batch(self, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """One round trip for many operations - the blueprint speed trick."""
        if self.bridge.connect():
            return self.bridge.batch(calls)
        # Fallback: sequential shimmed calls
        out = []
        for cmd, args in calls:
            ok, res, err = self.call(cmd, args)
            out.append({"ok": ok, "result": res, "error": err})
        return out

    def python(self, code: str):
        """Arbitrary editor Python (rc) or plugin-side eval (bridge)."""
        if self.bridge.connect():
            return self.bridge.call("python.exec", {"code": code})
        if self.rc.ping():
            try:
                return True, {"output": self.rc.python(code)}, None
            except TransportError as e:
                return False, None, str(e)
        return False, None, "no editor connection"

    def close(self) -> None:
        self.bridge.close()
