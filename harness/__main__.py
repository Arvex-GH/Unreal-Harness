"""CLI entry point: `unreal-harness` / `uh`.

  uh                 start the app (opens the browser)
  uh --no-browser    start headless
  uh install-bridge  copy the native plugin into the configured project
  uh doctor          check python, LM Studio, project, engine, plugin
  uh update          self-update
  uh version
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
import webbrowser

from . import __version__
from .config import Config, DATA_DIR, ensure_dirs


def _open_browser(url: str, delay: float = 1.2) -> None:
    def go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def cmd_serve(args: argparse.Namespace) -> int:
    ensure_dirs()
    cfg = Config.load()
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host

    url = f"http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '') else cfg.host}:{cfg.port}"
    if not _port_free(cfg.host, cfg.port):
        print(f"Port {cfg.port} is busy — is the harness already running?")
        print(f"Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    banner = f"""
  ╭──────────────────────────────────────────────╮
  │  Unreal Harness v{__version__:<28}│
  │  full-control AI developer for UE5           │
  ╰──────────────────────────────────────────────╯
  UI        {url}
  Data      {DATA_DIR}
  Project   {cfg.project_path or '(not set yet — open Settings)'}
  Model     {cfg.llm_base_url} {('· ' + cfg.model) if cfg.model else ''}
"""
    print(banner)
    if not args.no_browser:
        _open_browser(url)

    import uvicorn

    uvicorn.run("harness.server:app", host=cfg.host, port=cfg.port,
                log_level="warning", access_log=False)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    from .llm import LLMClient
    from .ue.project import Installer, UEController
    from .ue.transport import UnrealClient

    print(f"Unreal Harness {__version__}")
    print(f"python        {sys.version.split()[0]}  ({sys.executable})")
    print(f"data dir      {DATA_DIR}")
    cfg = Config.load()
    print(f"project       {cfg.project_path or '(none)'}")
    print(f"engine        {cfg.engine_path or '(none)'}")

    ue = UnrealClient(cfg)
    st = ue.status()
    print(f"transport     {st['transport']}  editor_connected={st['editor_connected']}")
    if st["transport"] == "bridge":
        print(f"bridge        {st['bridge']['verbs']} verbs, "
              f"{st['bridge']['latency_ms']}ms round trip")

    status = UEController(cfg).summary()
    if status.get("project"):
        print(f"c++ project   {status['is_cpp']}")
        print(f"plugins       {', '.join(status['plugins_enabled']) or '(none)'}")

    inst = Installer(cfg)
    tgt = inst.plugin_target()
    print(f"harness plugin {'installed' if tgt and (tgt / 'UEHarness.uplugin').exists() else 'not installed'}"
          + (f" at {tgt}" if tgt else ""))

    llm = LLMClient(cfg)
    health = asyncio.run(llm.health())
    if health["ok"]:
        print(f"lm studio     ok, models: {', '.join(health['models']) or '(none loaded)'}")
    else:
        print(f"lm studio     FAIL: {health['error']}")
    return 0


def cmd_install_bridge(args: argparse.Namespace) -> int:
    from .ue.project import Installer

    cfg = Config.load()
    if args.project:
        cfg.project_path = args.project
        cfg._autodetect()
        cfg.save()
    inst = Installer(cfg)
    result = inst.install_plugin(build=not args.no_build)
    print(json.dumps(result, indent=2, default=str))
    return 0 if not result.get("error") else 1


def cmd_update(args: argparse.Namespace) -> int:
    from . import updater

    cfg = Config.load()
    if args.check:
        print(json.dumps(asyncio.run(updater.check(cfg, force=True)), indent=2, default=str))
        return 0
    if getattr(args, "rollback", False):
        print(json.dumps(updater.rollback(), indent=2, default=str))
        return 0
    res = updater.apply(cfg, force=args.force)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unreal-harness", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"unreal-harness {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="start the app (default)")
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--host")
    p_serve.set_defaults(func=cmd_serve)

    p_doc = sub.add_parser("doctor", help="diagnose setup")
    p_doc.set_defaults(func=cmd_doctor)

    p_ib = sub.add_parser("install-bridge", help="install the native C++ plugin")
    p_ib.add_argument("--project", help="path to .uproject")
    p_ib.add_argument("--no-build", action="store_true")
    p_ib.set_defaults(func=cmd_install_bridge)

    p_up = sub.add_parser("update", help="self-update")
    p_up.add_argument("--check", action="store_true")
    p_up.add_argument("--force", action="store_true")
    p_up.add_argument("--rollback", action="store_true")
    p_up.set_defaults(func=cmd_update)

    args = parser.parse_args(argv)
    if not args.cmd:
        args = parser.parse_args(["serve"] + (argv or sys.argv[1:]))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
