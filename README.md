# Unreal Harness

**An AI developer that actually drives Unreal Engine 5.** Not a chat window that tells you which
menus to click — a local agent with the same options a human developer has: blueprints, graphs,
levels, materials, C++, the project's own files, console, builds, logs.

Runs entirely on your machine against LM Studio. No MCP hop, no cloud, no telemetry.

```
┌──────────────┐   SSE/HTTP   ┌────────────────┐   TCP JSON-lines   ┌──────────────────┐
│   Chat UI    │◄────────────►│  Python brain  │◄──────────────────►│  UE5 Editor      │
│  (browser)   │              │  (FastAPI)     │     0.3ms/call     │  UEHarness plugin│
└──────────────┘              │                │                    └──────────────────┘
                              │                │──── HTTP :30010 ──► Remote Control API
                              │                │──── file I/O ─────► .uproject / .ini / C++
                              └────────────────┘
                                     ▲
                                     │ OpenAI-compatible
                              ┌──────────────┐
                              │  LM Studio   │
                              └──────────────┘
```

---

## Why not MCP

MCP servers for Unreal exist and most of them share two problems: they proxy every call through
JSON-RPC over stdio (an extra process, an extra serialisation, an extra round trip) and they can
only reach what the stock Remote Control API exposes, which is property get/set and function calls
on UObjects — *not* graph editing.

This harness talks to the editor **four ways at once** and picks the fastest that is available:

| Lane | Transport | Latency | What it unlocks |
|---|---|---|---|
| **1. Native bridge** | persistent TCP socket into a C++ editor module | **~0.3 ms**, pipelined | graph authoring, transactions, log capture, level ops |
| **2. Remote Control** | stock HTTP plugin on `:30010` | ~5-15 ms | works with zero installation |
| **3. Python API** | Python Editor Script plugin | ~10-40 ms | anything the typed tools don't cover |
| **4. Disk** | direct file I/O | ~0.1 ms | `.uproject`, `.ini`, C++, Python — works with the editor closed |

Call 1 is the default. `ue_status` tells you which lane is live; the UI shows it in the top bar.

Because the socket is persistent and pipelined, N commands cost one write and one read:
`ue_run_batch` fires 20 blueprint operations in a single round trip.

---

## The blueprint speed trick

Wiring blueprints through one-node-at-a-time tool calls is slow and error-prone (you need a
round trip just to learn a node's GUID before you can connect it).

`bp_build` does the whole graph in **one call, one editor transaction**. Nodes get symbolic refs,
links refer to `ref.pin`, defaults are applied, the blueprint compiles — and it's a single entry in
the Undo stack, exactly like a human's edit.

```python
bp_build(
    bp="/Game/Blueprints/BP_Floater",
    nodes=[
        {"ref": "begin", "kind": "event", "event": "ReceiveBeginPlay", "x": 0,   "y": 0},
        {"ref": "print", "kind": "call_function", "function": "PrintString",
         "x": 320, "y": 0},
    ],
    links=[{"from": "begin.then", "to": "print.execute"}],
    defaults=[{"pin": "print.InString", "value": "Floating!"}],
    compile=True,
)
```

Node kinds: `call_function`, `event`, `custom_event`, `variable_get`, `variable_set`, `branch`,
`sequence`, `cast`.

Surgical edits are also available when you need them: `bp_add_node`, `bp_connect`,
`bp_disconnect`, `bp_set_pin_default`, `bp_read_graph`.

---

## Install

### Windows (PowerShell)

```powershell
git clone https://github.com/Arvex-GH/Unreal-Harness.git
cd Unreal-Harness
.\install.ps1 -Project "C:\Games\MyGame\MyGame.uproject"
```

### Linux / macOS

```bash
git clone https://github.com/Arvex-GH/Unreal-Harness.git
cd Unreal-Harness
./install.sh --project ~/Projects/MyGame/MyGame.uproject
```

The installer creates a virtualenv, installs dependencies, adds a `uh` launcher to your PATH,
creates a desktop shortcut (Windows), and copies the native plugin into
`<Project>/Plugins/UEHarness`, enabling the stock `RemoteControl` + `PythonScriptPlugin` plugins
along the way.

Then:

```bash
uh            # starts the app and opens the UI
uh doctor     # prints exactly what is connected and what isn't
uh update     # self-update
```

### First run checklist

1. **LM Studio** → Developer tab → *Start Server* (port 1234). Load any model with tool-calling
   support for the best experience; models without it automatically fall back to a text tool
   protocol.
2. `uh` → the UI opens at <http://127.0.0.1:8787>.
3. Settings → **Install engine bridge** → restart the editor. The bridge listens on `:34567`.
4. `ue_status` should report `transport: bridge`.

If the plugin can't compile (Blueprint-only project, or no engine path configured), everything
still works over the Remote Control + Python lane — you just lose graph editing.

---

## What the agent can do

49 tools, all with typed JSON schemas.

| Area | Tools |
|---|---|
| **Blueprints** | `bp_create`, `bp_add_variable`, `bp_build`, `bp_add_node`, `bp_connect`, `bp_disconnect`, `bp_set_pin_default`, `bp_read_graph`, `bp_list_graphs`, `bp_compile` |
| **Level** | `level_find_actors`, `level_spawn`, `level_set_property`, `level_call`, `level_play` |
| **Editor** | `editor_console`, `editor_log`, `editor_save`, `editor_undo`, `editor_select`, `ue_screenshot` |
| **Assets** | `ue_search_assets`, `ue_asset_info`, `ue_find_function`, `mat_create`, `mat_assign`, `umg_create_widget`, `input_add_action` |
| **C++** | `ue_generate_cpp`, `ue_build`, `ue_live_coding` |
| **Project** | `proj_set_ini`, `proj_default_map`, `ue_python`, `ue_run_batch`, `ue_launch_editor`, `ue_install_bridge`, `ue_status` |
| **Files** | `fs_read`, `fs_write`, `fs_patch`, `fs_list`, `fs_glob`, `fs_grep`, `fs_mkdir`, `fs_move`, `fs_delete` |
| **System** | `shell_run`, `http_request` |

`ue_python` is the escape hatch: arbitrary Unreal Python over the fast socket, for the long tail
(sequencer, Niagara, PCG, import pipelines) without waiting for a typed tool.

---

## Safety model

"Full control" needs boundaries that are predictable, not absent.

- **Undo** — every native edit runs in a scoped transaction named `Harness: <verb>`. `Ctrl+Z`
  undoes the AI exactly like a human edit.
- **Write sandbox** — writes are confined to the project folder plus any roots you add in
  Settings. Reads are unrestricted (an agent needs engine headers to write correct C++).
- **Approval modes** — `auto` (default, no prompts), `confirm_risky` (gates destructive/shell/
  network tools), `confirm_all`. Toggle from the composer.
- **Autopilot** — keeps iterating until the task is done, up to `max_steps`. Turn it off for
  single-step control.
- **Everything is local** — the only network calls are to your LM Studio and, optionally, GitHub
  for update checks.

Chats and config live in an OS-appropriate data dir and are *never* touched by updates:

```
Windows   %APPDATA%\UnrealHarness
macOS     ~/Library/Application Support/UnrealHarness
Linux     ~/.config/UnrealHarness  (or $XDG_CONFIG_HOME)
```

---

## Updating

Two modes, detected automatically:

- **git checkout** — `uh update` fetches and fast-forwards, refuses if you have local changes
  (or `--force` to autostash).
- **release zip** — downloads the newest GitHub release, backs up the old install to
  `data/backups/`, and swaps files while preserving `config.json`, `chats.db`, logs.

The UI has *Check for updates* and *Install update* in Settings; `uh update --rollback` restores
the previous version.

---

## Hacking on it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m harness serve --no-browser
python tests/test_pipeline.py          # 6 end-to-end tests
```

The test suite spins up `tests/mock_ue.py`, a mock editor that speaks the real wire protocol, and
`tests/mock_lmstudio.py`, a scripted OpenAI-compatible stub — so you can exercise the full
agent loop with no Unreal and no model:

```bash
python -m tests.mock_ue --port 34567 &
python tests/mock_lmstudio.py 1234 &
python -m harness serve
# Settings → LM Studio URL: http://127.0.0.1:1234/v1
```

### Layout

```
harness/
  agent.py        the plan→act→observe loop, context pruning
  llm.py          LM Studio client + text-protocol fallback
  server.py       FastAPI: SSE chat, settings, status, updates
  db.py           SQLite chats
  updater.py      git/release self-update
  ue/
    transport.py  bridge socket, RC shim, capability probing
    project.py    .uproject/.ini/C++/headless control + installer
  tools/          49 tool definitions (fs, ue, blueprints, materials)
  static/         the UI (one file, zero dependencies)
plugin/UEHarness/ the C++ editor module (proxy-free, transactional)
tests/            mock editor + mock LM Studio + e2e tests
```

Adding a tool is ~10 lines: decorate a function with `@tool(...)`, declare its JSON schema, and
it appears in the agent's catalogue, the settings list, and the approval gate automatically.

---

## Status & limits

Tested against a protocol-faithful mock; the C++ module targets UE 5.1–5.6 with version guards
where engine APIs moved. Things that will need tuning on a real project: pin names for exotic
node types, material graph positions, and per-version Remote Control quirks. `ue_python` covers
whatever the typed surface misses in the meantime.

MIT licensed.
