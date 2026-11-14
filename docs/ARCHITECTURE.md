# Architecture notes

## The central problem

An LLM can write code, but Unreal's editor state is not code. The interesting operations —
"create a blueprint, add a variable, put a BeginPlay, print the variable, compile" — exist only
as in-memory editor objects manipulated through the editor UI. To let a model do them we need a
programmatic surface that is (a) fast enough to be used iteratively, (b) atomic enough to be
verified, and (c) complete enough that the model doesn't stall on gaps.

## Layer by layer

### 1. LLM (LM Studio)

OpenAI-compatible `/v1/chat/completions` with `tools`. Streaming SSE. Two details matter:

- **Model capability detection.** Many local models advertise tool support and then fail. On a
  400 mentioning tools, or a malformed tool call, the client flips `native_tool_calls` off and
  re-runs with a text protocol (`\`\`\`tool_call {json}\`\`\`` blocks) that the harness parses
  itself. The agent doesn't get stuck on a model that can't do function calling.
- **Context budget.** A UE session produces huge tool outputs. `prune()` stubs old tool results to
  300 chars and drops the oldest non-system messages until the transcript fits
  `context_budget_tokens`. The system prompt (tool catalogue + environment) is always first and
  never pruned.

### 2. Agent loop

```
for step in range(max_steps):
    messages = system + prune(history)
    stream = llm.stream_chat(messages, tools=registry.schemas())
    if no tool calls: persist + return final answer
    for call in tool_calls:            # parallel calls from one assistant turn
        gate = approval_policy(call)
        result = registry.execute(call)  # never raises; errors are observations
        persist(tool result)
```

The agent sees errors as tool results, not exceptions, which is what makes self-correction work:
compile fails → it reads `editor_log` → patches → recompiles.

### 3. Tools

A tool is a function plus a JSON schema plus a risk tag. `registry.execute` wraps every call so a
handler exception becomes `{"ok": false, "error": "..."}`. Two consequences:

- the model can never crash a turn with a bad argument
- a TypeError from a wrong argument name is returned *with the schema attached*, so the model can
  fix its own call

Risk tags (`safe`/`write`/`destructive`/`shell`/`network`) drive both the approval gate and
availability (`allow_shell`, `allow_network`).

### 4. Transports

`UnrealClient.probe()` picks the best lane within 2 seconds of the last probe and caches the
result. `call()` walks a preference list and falls through on failure. Failure messages name the
port and the fix, because a silent "tool failed" is the worst possible feedback for an agent.

The bridge is a raw socket, not HTTP: no framing overhead, no server, `TCP_NODELAY`, and a
`makefile("rwb")` handles line buffering. `batch()` writes N requests in one `send` and reads N
replies — the reason bulk operations are cheap.

### 5. Editor module (C++)

Why a plugin at all, given the Python API exists?

| Need | Python | C++ module |
|---|---|---|
| Graph node creation | via `BlueprintEditorLibrary` (partial, version-dependent) | direct `NewObject<UK2Node_*>` + `AllocateDefaultPins` |
| Transaction control | per-call | one outer transaction per command |
| Latency of the transport | HTTP + Python VM each call | direct socket, no interpreter |
| Log capture | pull-only | `FOutputDevice` hook, cheap ring buffer |
| Compile results | status enum | `CompilerResultsLog` |

It's ~1100 lines, dependency-light (`BlueprintGraph`, `Kismet`, `UnrealEd`, `Sockets`), and
guarded where engine APIs moved between 5.1 and 5.6.

Everything runs on the game thread via `AsyncTask` + `FEvent`, so no editor API is touched from a
worker thread.

### 6. Disk lane

`.uproject` JSON, `Config/*.ini` INI editing, C++ scaffolding (matching the editor wizard's
output), and headless `UnrealEditor-Cmd -ExecutePythonScript` runs. This is what makes the harness
useful when the editor isn't running — and it's how the installer enables plugins before the next
launch.

## Design decisions worth flagging

**Symbolic refs over GUIDs.** Round-tripping to learn a GUID costs a full model turn
(seconds of local inference). Letting the caller invent names and resolving them server-side turns
a 10-node graph from ~20 round trips into 1.

**One transaction per command, not per tool call.** `bp_build` issues many internal verb
dispatches inside one outer `FScopedTransaction`, so it's one undo entry, not fifteen.

**Errors as data, everywhere.** No tool raises. No transport raises. The agent's only feedback
channel is the transcript, so every failure has to be legible in it.

**No engine state in the harness.** The harness keeps no model of the project; it queries the
editor each time. A stale cache is worse than a 0.3 ms call.
