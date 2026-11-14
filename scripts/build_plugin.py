#!/usr/bin/env python3
"""Build the UEHarness editor plugin against a real engine install.

Usage:
    python scripts/build_plugin.py --engine "C:/Program Files/Epic Games/UE_5.5"
    python scripts/build_plugin.py --engine ~/UnrealEngine --project ~/Games/MyGame/MyGame.uproject

This is the same UBT invocation the editor performs, just headless, so you can
verify the module compiles without opening Unreal.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin" / "UEHarness"


def platform_bin() -> str:
    return {"Windows": "Win64", "Darwin": "Mac", "Linux": "Linux"}[platform.system()]


def find_ubt(engine: Path) -> Path:
    if platform.system() == "Windows":
        cands = [engine / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.exe",
                 engine / "Engine/Binaries/DotNET/UnrealBuildTool.exe"]
    else:
        cands = [engine / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool",
                 engine / "Engine/Binaries/DotNET/UnrealBuildTool.exe"]
        if platform.system() == "Darwin":
            cands.insert(0, engine / "Engine/Binaries/DotNET/UnrealBuildTool/UnrealBuildTool")
    for c in cands:
        if c.exists():
            return c
    # engine source builds keep it under Engine/Source/Programs
    raise SystemExit(f"UnrealBuildTool not found under {engine}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, help="UE install root (contains Engine/)")
    ap.add_argument("--project", help=".uproject to build the plugin inside")
    ap.add_argument("--config", default="Development")
    ap.add_argument("--clean", action="store_true")
    ns = ap.parse_args()

    engine = Path(ns.engine).expanduser().resolve()
    if not (engine / "Engine").exists():
        raise SystemExit(f"{engine} does not look like an engine install (no Engine/ folder)")

    ubt = find_ubt(engine)
    print(f"engine  {engine}\nubt     {ubt}")

    if ns.project:
        proj = Path(ns.project).expanduser().resolve()
        if not proj.exists():
            raise SystemExit(f"project not found: {proj}")
        target = next((p.stem for p in (proj.parent / "Source").glob("*Editor.Target.cs")), None) \
            or next((p.stem for p in (proj.parent / "Source").glob("*.Target.cs")), None)
        if not target:
            raise SystemExit("no *.Target.cs found - is the project C++?")
        cmd = [str(ubt), target, platform_bin(), ns.config,
               f"-Project={proj}", "-WaitMutex", "-FromMsBuild"]
        label = f"project {proj.name} / target {target}"
    else:
        # Compile the module standalone against a throwaway target.
        cmd = [str(ubt), "UE5Editor", platform_bin(), ns.config,
               f"-Plugin={PLUGIN / 'UEHarness.uplugin'}", "-WaitMutex", "-FromMsBuild"]
        label = "plugin only"

    if ns.clean:
        for d in (PLUGIN / "Binaries", PLUGIN / "Intermediate"):
            shutil.rmtree(d, ignore_errors=True)

    print(f"building {label}\n  {' '.join(cmd)}\n")
    proc = subprocess.run(cmd, text=True)
    if proc.returncode == 0:
        print("\nBuild OK.")
        for b in (PLUGIN / "Binaries").rglob("*") if (PLUGIN / "Binaries").exists() else []:
            if b.is_file():
                print(f"  {b.relative_to(PLUGIN)}  ({b.stat().st_size // 1024} KB)")
        return 0
    print(f"\nBuild FAILED (exit {proc.returncode}).")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
