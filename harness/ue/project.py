"""Project-level control that does not require the editor to be running.

This is the "bypass lane": the harness edits the project's own files directly
(the same bytes a human would edit by hand), which is often faster and always
works headless.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import Config

VERSION_KEYS = ["MajorVersion", "MinorVersion", "PatchVersion", "Changelist", "BranchName"]


class UEController:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------- helpers
    @property
    def proj(self) -> Path | None:
        return self.cfg.project_file

    def _require_proj(self) -> Path:
        p = self.proj
        if not p or not p.exists():
            raise FileNotFoundError(
                "No .uproject configured. Open Settings and point the harness at your project.")
        return p

    def _platform_bin(self) -> str:
        return {"Windows": "Win64", "Darwin": "Mac", "Linux": "Linux"}[platform.system()]

    def ubt_path(self) -> Path | None:
        if not self.cfg.engine_path:
            return None
        eng = Path(self.cfg.engine_path)
        if platform.system() == "Windows":
            return eng / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.exe"
        if platform.system() == "Darwin":
            return eng / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool"
        return eng / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool"

    def editor_cmd(self) -> Path | None:
        if not self.cfg.engine_path:
            return None
        eng = Path(self.cfg.engine_path)
        if platform.system() == "Windows":
            return eng / "Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
        if platform.system() == "Darwin":
            return eng / "Engine/Binaries/Mac/UnrealEditor-Cmd"
        return eng / "Engine/Binaries/Linux/UnrealEditor-Cmd"

    def module_name(self) -> str:
        proj = self._require_proj()
        return proj.stem

    def source_dir(self) -> Path:
        return self._require_proj().parent / "Source"

    # --------------------------------------------------------- uproject I/O
    def read_uproject(self) -> dict:
        p = self._require_proj()
        raw = p.read_text(encoding="utf-8-sig")
        return json.loads(raw) if raw.strip() else {}

    def write_uproject(self, data: dict) -> None:
        p = self._require_proj()
        backup = p.with_suffix(".uproject.bak")
        if p.exists():
            shutil.copy2(p, backup)
        p.write_text(json.dumps(data, indent=4), encoding="utf-8")

    def enable_plugin(self, name: str) -> bool:
        """Enable a plugin by name in the .uproject (idempotent)."""
        data = self.read_uproject()
        plugins = data.setdefault("Plugins", [])
        for entry in plugins:
            if entry.get("Name") == name:
                if entry.get("Enabled") is True:
                    return False
                entry["Enabled"] = True
                self.write_uproject(data)
                return True
        plugins.append({"Name": name, "Enabled": True})
        self.write_uproject(data)
        return True

    def plugins_enabled(self) -> list[str]:
        return [p.get("Name", "") for p in self.read_uproject().get("Plugins", [])
                if p.get("Enabled")]

    def module_descriptor(self) -> Path:
        name = self.module_name()
        return self.source_dir() / f"{name}.uproject"  # placeholder, replaced below

    # ------------------------------------------------- target/module naming
    def primary_target(self) -> str:
        """Find the editor/game target name from Source/*.Target.cs."""
        src = self.source_dir()
        if not src.exists():
            raise FileNotFoundError(f"Source folder not found: {src}")
        cands = [p.stem for p in src.glob("*.Target.cs")]
        for pref in ("Editor", "Game"):
            hits = [c for c in cands if c.endswith(pref)]
            if hits:
                return sorted(hits)[0]
        if cands:
            return cands[0]
        raise FileNotFoundError("no *.Target.cs found - is this a C++ project?")

    def build(self, target: str = "", config: str = "Development",
              live_coding: bool = False, timeout: int = 900) -> dict:
        proj = self._require_proj()
        target = target or self.primary_target()
        ubt = self.ubt_path()
        if live_coding:
            # Console command through the running editor is the fastest path.
            return {"error": "live coding must be triggered through the editor",
                    "hint": "use editor_console('LiveCoding.Compile') via the bridge"}
        if not ubt or not Path(ubt).exists():
            return {"error": f"UnrealBuildTool not found at {ubt}",
                    "hint": "set engine_path in Settings"}

        if platform.system() == "Windows":
            cmd = [str(ubt), target, self._platform_bin(), config,
                   f"-Project={proj}", "-WaitMutex", "-FromMsBuild"]
        else:
            cmd = [str(ubt), target, self._platform_bin(), config,
                   f"-Project={proj}", "-WaitMutex"]

        t0 = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired as e:
            return {"error": f"build timed out after {timeout}s",
                    "stdout": (e.stdout or "")[-8000:]}
        out = (proc.stdout or "") + (proc.stderr or "")
        errors = [l for l in out.splitlines() if ": error" in l or ": fatal error" in l]
        warnings = [l for l in out.splitlines() if ": warning" in l]
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "errors": errors[-60:],
            "error_count": len(errors),
            "warnings": len(warnings),
            "log_tail": out[-8000:],
            "duration_ms": int((time.time() - t0) * 1000),
        }

    # ------------------------------------------------------------ C++ wizard
    CPP_TEMPLATES = {
        "ActorComponent": {
            "include": "#include \"CoreMinimal.h\"\n#include \"Components/ActorComponent.h\"\n#include \"{name}.generated.h\"",
            "macros": "UCLASS( ClassGroup=(Custom), meta=(BlueprintSpawnableComponent) )",
            "base": "UActorComponent",
            "extra": "GENERATED_BODY()\n\npublic:\t\n\t{cls}();\n\nprotected:\n\tvirtual void BeginPlay() override;\n\npublic:\t\n\tvirtual void TickComponent(float DeltaTime, ELevelTick TickType, FActorComponentTickFunction* ThisTickFunction) override;",
            "ctor_body": "\tPrimaryComponentTick.bCanEverTick = true;",
            "template_class": "U{name}",
        },
        "Actor": {
            "include": "#include \"CoreMinimal.h\"\n#include \"GameFramework/Actor.h\"\n#include \"{name}.generated.h\"",
            "macros": "UCLASS()",
            "base": "AActor",
            "extra": "GENERATED_BODY()\n\npublic:\t\n\t{cls}();\n\nprotected:\n\tvirtual void BeginPlay() override;\n\npublic:\t\n\tvirtual void Tick(float DeltaTime) override;",
            "ctor_body": "\tPrimaryActorTick.bCanEverTick = true;",
            "template_class": "A{name}",
        },
        "Character": {
            "include": "#include \"CoreMinimal.h\"\n#include \"GameFramework/Character.h\"\n#include \"{name}.generated.h\"",
            "macros": "UCLASS()",
            "base": "ACharacter",
            "extra": "GENERATED_BODY()\n\npublic:\t\n\t{cls}();\n\nprotected:\n\tvirtual void BeginPlay() override;\n\npublic:\t\n\tvirtual void Tick(float DeltaTime) override;\n\tvirtual void SetupPlayerInputComponent(class UInputComponent* PlayerInputComponent) override;",
            "ctor_body": "\tPrimaryActorTick.bCanEverTick = true;",
            "template_class": "A{name}",
        },
        "UserWidget": {
            "include": "#include \"CoreMinimal.h\"\n#include \"Blueprint/UserWidget.h\"\n#include \"{name}.generated.h\"",
            "macros": "UCLASS()",
            "base": "UUserWidget",
            "extra": "GENERATED_BODY()\n\nprotected:\n\tvirtual void NativeConstruct() override;",
            "ctor_body": "",
            "template_class": "U{name}",
        },
    }

    def generate_cpp(self, name: str, base: str = "ActorComponent", module: str = "",
                     path: str = "") -> dict:
        proj = self._require_proj()
        module = module or self.module_name()
        spec = self.CPP_TEMPLATES.get(base)
        if not spec:
            return {"error": f"unsupported base '{base}'",
                    "available": list(self.CPP_TEMPLATES)}

        name = re.sub(r"[^A-Za-z0-9_]", "", name)
        cls = spec["template_class"].format(name=name)
        api = module.upper() + "_API"
        folder = Path(path) if path else self.source_dir() / module
        folder = folder if folder.is_absolute() else proj.parent / folder
        folder.mkdir(parents=True, exist_ok=True)

        h_path = folder / f"{name}.h"
        cpp_path = folder / f"{name}.cpp"

        header = f"""// Copyright (c) {time.strftime('%Y')}. Generated by Unreal Harness.
#pragma once

{spec['include'].format(name=name)}

{spec['macros'].format(name=name)}
class {api} {cls} : public {spec['base']}
{{
{spec['extra'].format(cls=cls, name=name)}
}};
"""
        body_impl = {
            "ActorComponent": (
                f"\tSetTickGroup(ETickingGroup::TG_PrePhysics);\n\tPrimaryComponentTick.bCanEverTick = true;",
                "\tSuper::BeginPlay();\n",
                "\tSuper::TickComponent(DeltaTime, TickType, ThisTickFunction);\n",
            ),
            "Actor": (
                "\tPrimaryActorTick.bCanEverTick = true;",
                "\tSuper::BeginPlay();\n",
                "\tSuper::Tick(DeltaTime);\n",
            ),
            "Character": (
                "\tPrimaryActorTick.bCanEverTick = true;",
                "\tSuper::BeginPlay();\n",
                "\tSuper::Tick(DeltaTime);\n",
            ),
            "UserWidget": ("", "\tSuper::NativeConstruct();\n", ""),
        }[base]

        cpp = f"""// Copyright (c) {time.strftime('%Y')}. Generated by Unreal Harness.
#include "{name}.h"

{cls}::{cls}()
{{
{body_impl[0]}
}}

void {cls}::BeginPlay()
{{
\tSuper::BeginPlay();
"""
        if base in ("Actor", "Character"):
            cpp += "}\n\nvoid %s::Tick(float DeltaTime)\n{\n\tSuper::Tick(DeltaTime);\n" % cls
        elif base == "ActorComponent":
            cpp += ("}\n\nvoid %s::TickComponent(float DeltaTime, ELevelTick TickType, "
                    "FActorComponentTickFunction* ThisTickFunction)\n{\n"
                    "\tSuper::TickComponent(DeltaTime, TickType, ThisTickFunction);\n" % cls)
        elif base == "UserWidget":
            cpp = cpp.replace(f"void {cls}::BeginPlay()", f"void {cls}::NativeConstruct()")
        cpp += "}\n"

        h_path.write_text(header, encoding="utf-8")
        cpp_path.write_text(cpp, encoding="utf-8")

        # Make sure the Build.cs knows about nothing new (module-level is fine),
        # and refresh project files so the IDE/UBT pick the files up.
        return {"header": str(h_path), "source": str(cpp_path), "class": cls,
                "module": module, "base": base,
                "next": f"call bp_compile or ue_build to compile '{cls}'"}

    # -------------------------------------------------------- ini accessors
    def read_ini(self, section_file: str, section: str) -> dict[str, str]:
        proj = self._require_proj().parent
        path = proj / "Config" / section_file
        if not path.exists():
            return {}
        out, cur = {}, None
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1]
            elif cur == section and "=" in s and not s.startswith(";"):
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def write_ini(self, section_file: str, section: str, values: dict[str, str]) -> str:
        proj = self._require_proj().parent
        path = proj / "Config" / section_file
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
        out, cur, seen, found_section = [], None, set(), False
        for line in lines:
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if cur == section:
                    for k, v in values.items():
                        if k not in seen:
                            out.append(f"{k}={v}")
                            seen.add(k)
                cur = s[1:-1]
                if cur == section:
                    found_section = True
            if cur == section and "=" in s and not s.startswith(";"):
                k = s.split("=", 1)[0].strip()
                if k in values:
                    out.append(f"{k}={values[k]}")
                    seen.add(k)
                    continue
            out.append(line)
        if not found_section:
            if out and out[-1].strip() != "":
                out.append("")
            out += [f"[{section}]"] + [f"{k}={v}" for k, v in values.items()]
        elif cur == section:
            for k, v in values.items():
                if k not in seen:
                    out.append(f"{k}={v}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return str(path)

    # ------------------------------------------------------ headless python
    def run_editor_python(self, code: str, timeout: int = 300) -> dict:
        """Execute project Python in a headless editor (works with editor closed)."""
        proj = self._require_proj()
        cmd_exe = self.editor_cmd()
        if not cmd_exe or not Path(cmd_exe).exists():
            return {"error": f"UnrealEditor-Cmd not found at {cmd_exe}"}
        script = proj.parent / "Saved" / "Harness" / f"headless_{int(time.time()*1000)}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(code, encoding="utf-8")
        cmd = [str(cmd_exe), str(proj), f"-ExecutePythonScript={script}",
               "-unattended", "-nosplash", "-nop4", "-stdout", "-NullRHI"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired:
            return {"error": "headless run timed out"}
        return {"ok": proc.returncode == 0,
                "stdout": (proc.stdout or "")[-12000:],
                "stderr": (proc.stderr or "")[-4000:]}

    # -------------------------------------------------------------- project
    def summary(self) -> dict:
        p = self.proj
        if not p:
            return {"error": "no project configured"}
        return {
            "project": p.name,
            "path": str(p),
            "engine": self.cfg.engine_path,
            "is_cpp": (p.parent / "Source").exists(),
            "plugins_enabled": self.plugins_enabled(),
            "maps": [str(x) for x in (p.parent / "Content").glob("**/*.umap")][:20]
            if (p.parent / "Content").exists() else [],
        }


# ---------------------------------------------------------------- installer
_INIT_BEGIN = "# BEGIN UNREAL HARNESS"
_INIT_END = "# END UNREAL HARNESS"
_INIT_SNIPPET = f"""{_INIT_BEGIN}
try:
    import unreal as _uh_unreal
    _rc_cls = getattr(_uh_unreal, "RemoteControlSettings", None)
    if _rc_cls is not None:
        _rc_cdo = _uh_unreal.get_default_object(_rc_cls)
        for _prop in ("enable_remote_python_execution", "allow_console_command_remote_execution"):
            try:
                _rc_cdo.set_editor_property(_prop, True)
            except Exception:
                pass
except Exception:
    pass
{_INIT_END}
"""


class Installer:
    """Installs the C++ bridge plugin + configures Remote Control & Python."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ue = UEController(cfg)

    def plugin_source(self) -> Path:
        from .. import ROOT
        return ROOT / "plugin" / "UEHarness"

    def plugin_target(self) -> Path | None:
        proj = self.cfg.project_file
        if not proj:
            return None
        return proj.parent / "Plugins" / "UEHarness"

    def _uat_path(self) -> Path | None:
        if not self.cfg.engine_path:
            return None
        eng = Path(self.cfg.engine_path)
        rel = "Engine/Build/BatchFiles/RunUAT.bat" if platform.system() == "Windows" else "Engine/Build/BatchFiles/RunUAT.sh"
        p = eng / rel
        return p if p.exists() else None

    def configure_remote_control(self) -> dict:
        """Enable Remote Control + Python execution in .uproject, Config/*.ini, and init_unreal.py."""
        proj = self.cfg.project_file
        if not proj or not proj.exists():
            return {"configured": False, "reason": "no project configured"}

        enabled = [
            n for n in ("RemoteControl", "PythonScriptPlugin", "EditorScriptingUtilities")
            if self.ue.enable_plugin(n)
        ]

        rc_ini = self.ue.write_ini(
            "DefaultRemoteControl.ini",
            "/Script/RemoteControlCommon.RemoteControlSettings",
            {
                "bAutoStartWebServer": "True",
                "bAutoStartWebSocketServer": "True",
                "RemoteControlHttpServerPort": str(self.cfg.rc_port),
                "bEnableRemotePythonExecution": "True",
                "bAllowConsoleCommandRemoteExecution": "True",
            },
        )
        self.ue.write_ini(
            "DefaultEngine.ini",
            "/Script/PythonScriptPlugin.PythonScriptPluginSettings",
            {
                "bRemoteExecution": "True",
                "bDeveloperMode": "True",
            },
        )

        # Startup Python hook: guarantees RemoteControlSettings allows Python in memory at boot
        init_py = proj.parent / "Content" / "Python" / "init_unreal.py"
        init_py.parent.mkdir(parents=True, exist_ok=True)
        existing = init_py.read_text(encoding="utf-8", errors="ignore") if init_py.exists() else ""
        if _INIT_BEGIN in existing and _INIT_END in existing:
            pre = existing.split(_INIT_BEGIN, 1)[0]
            post = existing.split(_INIT_END, 1)[1].lstrip("\r\n")
            updated = pre.rstrip() + ("\n\n" if pre.strip() else "") + _INIT_SNIPPET + (("\n" + post) if post else "")
        elif existing.strip():
            updated = existing.rstrip() + "\n\n" + _INIT_SNIPPET
        else:
            updated = _INIT_SNIPPET
        if updated != existing:
            init_py.write_text(updated, encoding="utf-8")

        return {
            "configured": True,
            "enabled_plugins": enabled,
            "remote_control_ini": rc_ini,
            "init_python": str(init_py),
        }

    def sync_existing_install(self) -> dict:
        """Keep existing project plugin & Remote Control settings up to date after harness updates."""
        proj = self.cfg.project_file
        if not proj or not proj.exists():
            return {"synced": False}

        out: dict = {"synced": True}
        if self.cfg.auto_install_bridge:
            try:
                out["remote_control"] = self.configure_remote_control()
            except Exception as e:  # noqa: BLE001
                out["rc_error"] = str(e)

        src = self.plugin_source()
        dst = self.plugin_target()
        if src.exists() and dst and (dst / "UEHarness.uplugin").exists():
            try:
                src_desc = json.loads((src / "UEHarness.uplugin").read_text(encoding="utf-8-sig"))
                dst_desc = json.loads((dst / "UEHarness.uplugin").read_text(encoding="utf-8-sig"))
                needs_sync = (
                    "EngineVersion" in dst_desc
                    or int(dst_desc.get("Version", 0)) < int(src_desc.get("Version", 0))
                    or dst_desc.get("VersionName") != src_desc.get("VersionName")
                )
                if needs_sync:
                    shutil.copy2(src / "UEHarness.uplugin", dst / "UEHarness.uplugin")
                    dst_src = dst / "Source"
                    if dst_src.exists():
                        shutil.rmtree(dst_src)
                    shutil.copytree(src / "Source", dst_src)
                    out["plugin_updated"] = str(dst)
            except Exception as e:  # noqa: BLE001
                out["plugin_sync_error"] = str(e)
        return out

    def _build_standalone_plugin(self, src: Path, dst: Path, timeout: int = 1200) -> dict:
        """Build UEHarness standalone via RunUAT BuildPlugin (supports Blueprint-only projects)."""
        uat = self._uat_path()
        if not uat:
            return {"ok": False, "error": "RunUAT not found (set engine_path in Settings)"}
        import tempfile

        plat = self.ue._platform_bin()
        with tempfile.TemporaryDirectory(prefix="ueharness_uat_") as tmp:
            pkg_dir = Path(tmp) / "UEHarness"
            cmd = [
                str(uat),
                "BuildPlugin",
                f"-Plugin={src / 'UEHarness.uplugin'}",
                f"-Package={pkg_dir}",
                f"-TargetPlatforms={plat}",
                "-Rocket",
            ]
            t0 = time.time()
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"BuildPlugin timed out after {timeout}s"}
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0 or not (pkg_dir / "Binaries").exists():
                errors = [l for l in out.splitlines() if ": error" in l or "ERROR:" in l]
                return {
                    "ok": False,
                    "exit_code": proc.returncode,
                    "errors": errors[-40:],
                    "log_tail": out[-6000:],
                    "duration_ms": int((time.time() - t0) * 1000),
                }
            # Remove EngineVersion stamp from packaged .uplugin so patch/minor updates never prompt
            pkg_uplugin = pkg_dir / "UEHarness.uplugin"
            if pkg_uplugin.exists():
                try:
                    desc = json.loads(pkg_uplugin.read_text(encoding="utf-8-sig"))
                    desc.pop("EngineVersion", None)
                    desc["Installed"] = False
                    pkg_uplugin.write_text(json.dumps(desc, indent=2), encoding="utf-8")
                except Exception:
                    shutil.copy2(src / "UEHarness.uplugin", pkg_uplugin)
            if dst.exists():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(pkg_dir, dst)
            return {"ok": True, "duration_ms": int((time.time() - t0) * 1000)}

    def install_plugin(self, build: bool = True, enable_rc: bool = True) -> dict:
        src = self.plugin_source()
        dst = self.plugin_target()
        if not dst or not self.cfg.project_file:
            return {"error": "no project configured",
                    "hint": "the plugin is copied into <Project>/Plugins/UEHarness"}
        if not src.exists():
            return {"error": f"plugin source missing: {src}"}

        result: dict = {}
        try:
            if enable_rc:
                rc_info = self.configure_remote_control()
                result["enabled_plugins"] = rc_info.get("enabled_plugins", [])
                result["remote_control_ini"] = rc_info.get("remote_control_ini", "")
        except Exception as e:  # noqa: BLE001
            result["plugin_enable_error"] = str(e)

        is_cpp = (self.cfg.project_file.parent / "Source").exists()
        if is_cpp:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            self.ue.enable_plugin("UEHarness")
            result["installed_to"] = str(dst)
            if build:
                result["build"] = self.ue.build(timeout=1200)
        else:
            if build and self._uat_path():
                uat_res = self._build_standalone_plugin(src, dst)
                result["build"] = uat_res
                if uat_res.get("ok"):
                    self.ue.enable_plugin("UEHarness")
                    result["installed_to"] = str(dst)
                else:
                    # Do not leave unbuilt C++ module in a Blueprint-only project without binaries
                    if dst.exists() and not (dst / "Binaries").exists():
                        shutil.rmtree(dst, ignore_errors=True)
                    result["warning"] = (
                        "Blueprint-only project without a C++ build toolchain: "
                        "configured Remote Control + Remote Python execution instead "
                        "(all tools work via Remote Control after restarting Unreal Editor)."
                    )
            else:
                # Copy source only if user already has Binaries, otherwise rely on RC + Python cleanly
                if dst.exists() and (dst / "Binaries").exists():
                    shutil.copy2(src / "UEHarness.uplugin", dst / "UEHarness.uplugin")
                    if (dst / "Source").exists():
                        shutil.rmtree(dst / "Source")
                    shutil.copytree(src / "Source", dst / "Source")
                    result["installed_to"] = str(dst)
                else:
                    if dst.exists() and not (dst / "Binaries").exists():
                        shutil.rmtree(dst, ignore_errors=True)
                    result["warning"] = (
                        "Blueprint-only project: enabled Remote Control + Remote Python execution "
                        "in Config/DefaultRemoteControl.ini (set engine_path and click Install engine "
                        "bridge with Visual Studio installed if you also want the compiled C++ socket bridge)."
                    )
        result["next"] = (
            f"Restart Unreal Editor once. Native bridge uses :{self.cfg.bridge_port}; "
            f"Remote Control + Python uses :{self.cfg.rc_port}."
        )
        return result
