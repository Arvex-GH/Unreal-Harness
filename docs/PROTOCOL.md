# Bridge Protocol v2

How the harness and the editor module talk. Implemented in
`harness/ue/transport.py` (client) and `plugin/UEHarness/.../UEHarnessServer.cpp` (server).

## Framing

Loopback TCP, one JSON object per line, UTF-8, `\n` terminated.

```
{"id": 12, "cmd": "bp.add_node", "args": {"bp": "/Game/BP_Test", "kind": "branch"}, "flags": 0}
{"id": 12, "ok": true,  "result": {"id": "e0a1...", "title": "Branch", "inputs": [...], "outputs": [...]}}
{"id": 13, "ok": false, "error": "pin 'InStrng' not found on PrintString"}
```

- `id` is a uint64 echoed back. Ids are caller-assigned and may repeat across connections.
- `flags` bit 0: skip the implicit transaction (used by read-only verbs internally).
- Requests are pipelined: write N lines, read N replies. The server processes them in order on the
  game thread; replies are written as each finishes.

## Threading

The worker thread parses and then marshals to the game thread via `AsyncTask`, blocking on an
`FEvent`. Each command therefore runs on the game thread — safe for every editor API — and the
whole dispatch is wrapped in:

```cpp
FScopedTransaction Transaction(FText::FromString("Harness: bp.connect"));
```

which is what makes AI edits undoable.

## Verbs

| Verb | Args | Result |
|---|---|---|
| `handshake` / `ping` | – | plugin, protocol, engine_version, project, pie_running, verbs[] |
| `editor.console` | command | output |
| `editor.log` | lines, filter | lines[] |
| `editor.save_all` | – | saved |
| `editor.undo` | – | – |
| `editor.select` | asset | – |
| `editor.info` | – | project, project_dir, engine_version, platform, actor_count |
| `asset.search` | query, class, path, limit | assets[] |
| `asset.rename` / `asset.delete` | from/to, path | renamed / deleted |
| `bp.create` | path, parent | path |
| `bp.variable_add` | bp, name, type | name |
| `bp.list_graphs` | bp | graphs[] |
| `bp.graph_read` | bp, graph | nodes[] (id, class, title, x, y, inputs[], outputs[]) |
| `bp.add_node` | bp, kind, graph, x, y, + kind-specific | node |
| `bp.connect` | bp, from, to, graph | connected |
| `bp.disconnect` | bp, from, to?, graph | – |
| `bp.set_pin_default` | bp, pin, value, graph | – |
| `bp.build_graph` | bp, graph, nodes[], links[], defaults[], compile | nodes[], links[], errors[], compiled |
| `bp.compile` | bp | success, status |
| `actor.find` | class, limit | actors[] |
| `actor.spawn` | class, x, y, z | name, path |
| `actor.set_property` | actor, property, value | set |
| `actor.call` | actor, function | – |
| `pie.start` / `pie.stop` | – | – |
| `python.exec` | code | output |

## Pin specs

Address a pin as `nodeGuid:PinName`. Two shortcuts:

- bare `nodeGuid` → the node's first exec pin in the requested direction
- `then` / `exec` as the pin name → same as bare

Direction is implied by position: `from` is resolved as an output, `to` as an input.

## Symbolic refs in `bp.build_graph`

`bp.build_graph` exists so the caller never needs a round trip to learn GUIDs. Node specs carry a
`ref`, links and defaults refer to `ref.pin`, and the server substitutes real GUIDs before
dispatching the individual verbs:

```json
{
  "bp": "/Game/Blueprints/BP_Door",
  "nodes": [
    {"ref": "begin", "kind": "event", "event": "ReceiveActorBeginOverlap", "x": 0, "y": 0},
    {"ref": "branch", "kind": "branch", "x": 300, "y": 0},
    {"ref": "open", "kind": "call_function", "function": "K2_SetActorRotation", "x": 600, "y": 0}
  ],
  "links": [
    {"from": "begin.then", "to": "branch.execute"},
    {"from": "branch.then", "to": "open.execute"}
  ],
  "defaults": [{"pin": "branch.Condition", "value": "true"}],
  "compile": true
}
```

Failures are per-item and non-fatal: a bad link is reported in `links[]` with `ok:false` and the
rest of the graph still builds, so the agent gets one consolidated observation to fix.

## Capability negotiation

`handshake.verbs` is the source of truth for what this editor build supports. The harness uses it
to decide whether a verb goes over the bridge or falls back to Remote Control / Python. Anything
not listed is rejected client-side with a readable message instead of a mysterious timeout.

## Ports

| Port | Owner | Override |
|---|---|---|
| 34567 | bridge (this protocol) | `-HarnessPort=` on the editor cmdline, `UE_HARNESS_PORT` env var, or Settings |
| 30010 | Remote Control API (stock) | Settings |
| 8787 | harness UI | `uh --port N` |
