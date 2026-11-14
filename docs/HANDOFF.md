# Handoff — Unreal Harness v1.0.0

Everything needed to pick this up in a fresh session. Written 2026-11-13.

---

## 1. The original prompt (verbatim)

> Build a complete into the system installable harness. It shall be able to connect to lm studio
> and the goal is to give the ai EVERY tool a normal human developer has option in unreal engine.
> So the harness has to be deeply connected to the ue5 not just via mcp but also directly able to
> change files etc. For the agent as tool calls like for the human as clicks. For instance creating
> wiring up and more of blueprints in seconds. Not over a slow mcp that doesn't work. Find out how
> to do so. As I said: give the ai full control. The harness for now should only show a simple
> chat windows with chats on the left side and a chat in the middle. It shall also be fully
> installable so also create an installer. Don't forget the ability to update if the repo has a new
> update. That's it for now. Later we will add more. Go and go crazy!

Follow-up in turn 2: *"Please merge or did you already?"* — answered: nothing to merge, single
source tree, archive byte-identical to source. Turn 3: GitHub connector is unsupported in that
session, so this handoff was requested before pushing to a new repo.

---

## 2. What was built

A local-first AI agent harness that drives Unreal Engine 5 with the same options a human developer
has. **7,490 lines** across 43 files. Runs against LM Studio (OpenAI-compatible
`/v1/chat/completions`), no cloud, no telemetry.

### The central design decision: no MCP in the loop

MCP servers for Unreal all share two flaws — every call is an extra process hop through
JSON-RPC/stdio, and they only reach what the stock Remote Control API exposes (property get/set),
**not** graph editing. So four lanes exist and the harness auto-picks the fastest available:

| Lane | Mechanism | Latency | Unlocks |
|---|---|---|---|
| **1. Native bridge** | persistent TCP socket into a C++ editor module (written from scratch) | **0.3 ms** (measured) | graph authoring, transactions, log capture, level ops |
| 2. Remote Control | stock `:30010` HTTP plugin | ~5–15 ms | works with zero installation |
| 3. Python API | Python Editor Script plugin | ~10–40 ms | anything not covered by typed tools |
| 4. Disk | direct file I/O | ~0.1 ms | `.uproject`, `.ini`, C++ — works with editor closed |

The socket is pipelined: `ue_run_batch` fires N operations in one write/read pair (verified: 20
commands in one round trip).

### The blueprint speed trick — `bp_build`

Wiring node-by-node is slow because you need a round trip just to learn a node's GUID before you
can connect it. `bp_build` takes symbolic `ref`s, resolves them server-side, and does the whole
graph — nodes, links, pin defaults, compile — in **one call, one scoped editor transaction**
(one `Ctrl+Z`, like a human edit).

```python
bp_build(
    bp="/Game/Blueprints/BP_Floater",
    nodes=[
        {"ref": "begin", "kind": "event", "event": "ReceiveBeginPlay", "x": 0,   "y": 0},
        {"ref": "print", "kind": "call_function", "function": "PrintString", "x": 320, "y": 0},
    ],
    links=[{"from": "begin.then", "to": "print.execute"}],
    defaults=[{"pin": "print.InString", "value": "Floating!"}],
    compile=True,
)
```

Node kinds: `call_function`, `event`, `custom_event`, `variable_get`, `variable_set`, `branch`,
`sequence`, `cast`.

---

## 3. Inventory

### 49 tools, by category

| Category | Count | Tools |
|---|---|---|
| `unreal` | 14 | `ue_status`, `ue_launch_editor`, `ue_install_bridge`, `editor_console`, `editor_log`, `editor_save`, `editor_undo`, `editor_select`, `ue_find_function`, `ue_search_assets`, `ue_asset_info`, `ue_python`, `ue_run_batch`, `ue_screenshot` |
| `blueprints` | 10 | `bp_create`, `bp_add_variable`, `bp_list_graphs`, `bp_read_graph`, `bp_add_node`, `bp_connect`, `bp_disconnect`, `bp_set_pin_default`, `bp_build`, `bp_compile` |
| `files` | 9 | `fs_read`, `fs_write`, `fs_patch`, `fs_list`, `fs_glob`, `fs_grep`, `fs_mkdir`, `fs_move`, `fs_delete` |
| `level` | 5 | `level_find_actors`, `level_spawn`, `level_set_property`, `level_call`, `level_play` |
| `cplusplus` | 3 | `ue_generate_cpp`, `ue_build`, `ue_live_coding` |
| `materials` | 2 | `mat_create`, `mat_assign` |
| `project` | 2 | `proj_set_ini`, `proj_default_map` |
| `system` | 2 | `shell_run`, `http_request` |
| `ui` | 1 | `umg_create_widget` |
| `input` | 1 | `input_add_action` |

### Files

```
harness/            Python (1,647 core + 797 ue/ + 1,039 tools/)
  agent.py          plan→act→observe loop, context pruning, autopilot
  llm.py            LM Studio client + text-protocol fallback
  server.py         FastAPI: SSE chat streaming, settings, status, updates
  db.py             SQLite chats
  updater.py        git / release-zip self-update + rollback
  tools/            49 tool definitions (fs, ue, blueprints, materials)
  ue/transport.py   bridge socket, RC shim, capability probing
  ue/project.py     .uproject/.ini/C++/headless control + installer
  static/index.html the UI — one file, 1,007 lines, zero dependencies
plugin/UEHarness/   C++ editor module (1,709 lines)
tests/              841 lines: mock UE bridge, mock LM Studio, e2e tests
docs/PROTOCOL.md    wire protocol v2 spec
docs/ARCHITECTURE.md layer-by-layer rationale
install.sh/.ps1     installers (venv, PATH, desktop shortcut, plugin copy)
uninstall.sh/.ps1   clean removal (--purge for data)
scripts/            build_plugin.py (compile against real engine), package.py
```

---

## 4. What is verified vs. unverified

**Verified by running it (9/9 tests passing):**

- `tests/test_pipeline.py` (6) — bridge handshake, full blueprint flow, batch pipelining,
  actor tools, filesystem sandbox, context pruning
- `tests/test_agent.py` (3) — agent loop in **native tool-calling** mode, agent loop in
  **text-protocol fallback** mode, mid-run cancellation

Specifically proven end-to-end against a mock editor speaking the real wire protocol:

- Agent autonomously executed `bp_create → bp_add_variable → bp_build → bp_compile → editor_save`
  and delivered a final answer, in both protocol modes
- `bp_build` created 2 nodes, 1 link, 1 pin default, and compiled — in one round trip
- Invalid connection (exec→data pin) rejected with a readable error the agent can act on
- `ue_run_batch`: 20 commands in one socket round trip
- Sandbox refused a write outside the project; approval gate blocked a destructive tool
- `install.sh` → fresh venv → `uh doctor` → tests pass **from the installed copy**
- Archive unpacked and diffed against source: byte-identical

**NOT verified — the honest caveats:**

1. **The C++ module has never been compiled against a real engine.** It targets UE 5.1–5.6 with
   version guards (`ENGINE_MINOR_VERSION` blocks, avoided `TJsonWriter` field-serialisation
   overloads in favour of a hand-rolled writer, explicit socket lifetime handling). Brace/paren
   balance and API shapes were checked by hand. `scripts/build_plugin.py` exists to verify in one
   command against a real install:
   `python scripts/build_plugin.py --engine "C:/Program Files/Epic Games/UE_5.5" --project <path>`
2. **Blueprint pin names** for exotic node types may need tuning on a real project (the mock
   implements the common subset: PrintString, Branch, events, casts, variable get/set).
3. **No real LM Studio model was used** — a scripted OpenAI-compatible stub stood in. Streaming,
   tool_calls accumulation, and the 400-fallback path are exercised, but real-model behaviour
   (especially small models with weak tool calling) is unproven.
4. **Blueprint-only projects** can't compile the plugin; they fall back to the Remote Control lane,
   which works but has no graph editing.

---

## 5. Environment & how to run

```bash
# develop
cd unreal-harness
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m harness serve            # UI at http://127.0.0.1:8787
python tests/test_pipeline.py      # 6 tests
python tests/test_agent.py         # 3 tests

# demo with NO Unreal and NO model installed
make demo                          # starts mock editor + mock LM Studio + harness
# then Settings → LM Studio URL: http://127.0.0.1:1234/v1
```

Ports: **8787** UI · **34567** native bridge · **30010** Remote Control · **1234** LM Studio.

Real install:

```powershell
.\install.ps1 -Project "C:\Games\MyGame\MyGame.uproject"   # Windows
```
```bash
./install.sh --project ~/Projects/MyGame/MyGame.uproject    # Linux/macOS
```

Then: LM Studio → Developer → Start Server → run `uh` → Settings → **Install engine bridge** →
restart the editor.

---

## 6. Immediate next steps for the new session

1. **Push to GitHub** (repo doesn't exist yet; `harness/config.py` has
   `repo_url = "https://github.com/unreal-harness/unreal-harness"` — update it to the real URL):
   ```bash
   git init -b main && git remote add origin <URL>
   git add -A && git commit -m "Unreal Harness 1.0.0" && git push -u origin main
   ```
   This switches `uh update` from release-zip mode to fast-forward git mode.
2. **Compile-check the plugin** — the single biggest untested piece (see §4.1).
3. Then, in rough order of value: Sequencer/Niagara/PCG tool packs, editor event watch mode
   (react to compile errors and asset saves in real time), real-model testing with a small
   tool-calling model.

---

## 7. Design decisions worth preserving

- **Symbolic refs over GUIDs** — round-tripping to learn a GUID costs a full model turn (seconds of
  local inference). Letting the caller invent names and resolving server-side turns a 10-node graph
  from ~20 round trips into 1.
- **Errors as data, everywhere** — no tool raises, no transport raises. Every failure is a legible
  observation in the transcript, which is what makes self-correction work (compile fails → it reads
  `editor_log` → patches → recompiles).
- **One transaction per command, not per tool call** — `bp_build` issues many internal verb
  dispatches inside one outer `FScopedTransaction`.
- **No engine state cached in the harness** — it queries the editor each time. A stale cache is
  worse than a 0.3 ms call.
- **Write sandbox, open reads** — writes confined to the project + configured roots; reads
  unrestricted (an agent needs engine headers to write correct C++).
- **Model capability detection** — on a 400 mentioning tools, or unparseable tool calls, flip to a
  text protocol (`\`\`\`tool_call {json}\`\`\`` blocks) parsed locally. The agent doesn't get stuck
  on a model that can't do function calling.
