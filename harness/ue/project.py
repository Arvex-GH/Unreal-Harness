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
        out, cur, seen = [], None, set()
        for line in lines:
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if cur == section:
                    for k, v in values.items():
                        if k not in seen:
                            out.append(f"{k}={v}")
                cur = s[1:-1]
            if cur == section and "=" in s and not s.startswith(";"):
                k = s.split("=", 1)[0].strip()
                if k in values:
                    out.append(f"{k}={values[k]}")
                    seen.add(k)
                    continue
            out.append(line)
        if cur != section:
            out += ["", f"[{section}]"] + [f"{k}={v}" for k, v in values.items()]
        elif seen != set(values):
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
class Installer:
    """Installs the C++ bridge plugin + enables stock plugins."""

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

    def install_plugin(self, build: bool = True, enable_rc: bool = True) -> dict:
        src = self.plugin_source()
        dst = self.plugin_target()
        if not dst:
            return {"error": "no project configured",
                    "hint": "the plugin is copied into <Project>/Plugins/UEHarness"}
        if not src.exists():
            return {"error": f"plugin source missing: {src}"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)

        result: dict = {"installed_to": str(dst)}
        try:
            if enable_rc:
                result["enabled_plugins"] = [
                    n for n in ("RemoteControl", "PythonScriptPlugin", "EditorScriptingUtilities")
                    if self.ue.enable_plugin(n)
                ]
                # RemoteControl is a stock plugin; it lives in the engine, so a
                # .uproject entry is enough to load it with the project.
        except Exception as e:  # noqa: BLE001
            result["plugin_enable_error"] = str(e)

        is_cpp = (self.cfg.project_file.parent / "Source").exists()
        if not is_cpp:
            result["warning"] = (
                "This is a Blueprint-only project, so the native plugin cannot compile. "
                "Two options: (a) convert to C++ (Tools > New C++ Class), or (b) rely on the "
                "Remote Control + Python transport, which is already enabled.")
        elif build:
            result["build"] = self.ue.build(timeout=1200)
        result["next"] = "Restart the editor. The bridge listens on port %d" % self.cfg.bridge_port
        return result
