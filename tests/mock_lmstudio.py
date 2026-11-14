"""Mock LM Studio server - lets you demo/test the harness with no model loaded.

It plays a tiny scripted agent: turns user intent into tool calls, so you can
watch the whole pipeline (SSE -> tools -> engine) without an LLM.
Not a real model: just a deterministic OpenAI-compatible stub.
"""
from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _chunk(text=None, tool=None, finish=None):
    delta = {}
    if text is not None:
        delta["content"] = text
    if tool is not None:
        delta["tool_calls"] = tool
    return {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "mock-agent",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "mock-agent", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        messages = req.get("messages", [])
        has_tools = bool(req.get("tools"))

        # find the last user message and how many tool results we've had
        last_user = ""
        tool_results = []
        for m in messages:
            if m.get("role") == "user" and not (m.get("content") or "").startswith("TOOL RESULT ("):
                last_user = m.get("content", "")
            if m.get("role") == "tool":
                tool_results.append(m.get("content", ""))
            # text-protocol mode feeds results back as user messages, exactly like
            # a real model would read them from its transcript
            if m.get("role") == "user" and (m.get("content") or "").startswith("TOOL RESULT ("):
                tool_results.append(m.get("content", ""))
        done = len(tool_results)
        text = last_user.lower()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        plan = self._plan(text, done)
        if plan is None:
            self._say("All done. I built and compiled the blueprint.")
            self.wfile.write(_sse(_chunk(finish="stop")))
            self.wfile.write(b"data: [DONE]\n\n")
            return

        if done == 0:
            self._say("On it — creating the blueprint first.")
        else:
            self._say("")

        if has_tools:
            for i, (name, args) in enumerate(plan):
                self.wfile.write(_sse(_chunk(tool=[{
                    "index": i, "id": f"call_{done}_{i}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}])))
            self.wfile.write(_sse(_chunk(finish="tool_calls")))
        else:
            blocks = "\n".join(
                f'```tool_call\n{json.dumps({"tool": n, "args": a})}\n```' for n, a in plan)
            self._say(blocks)
            self.wfile.write(_sse(_chunk(finish="stop")))
        self.wfile.write(b"data: [DONE]\n\n")

    def _say(self, text: str):
        if not text:
            return
        for i in range(0, len(text), 18):
            self.wfile.write(_sse(_chunk(text=text[i:i + 18])))

    def _plan(self, text: str, done: int):
        """Deterministic multi-step plan based on the user's words."""
        bp = "/Game/Blueprints/BP_Demo"
        m = re.search(r"/game/[\w/]+", text)
        if m:
            bp = m.group(0)
        steps = [
            ("bp_create", {"path": bp, "parent": "Actor"}),
            ("bp_add_variable", {"bp": bp, "name": "Speed", "type": "float"}),
            ("bp_build", {"bp": bp, "graph": "EventGraph", "compile": True,
                          "nodes": [
                              {"ref": "begin", "kind": "event",
                               "event": "ReceiveBeginPlay", "x": 0, "y": 0},
                              {"ref": "print", "kind": "call_function",
                               "function": "PrintString", "x": 320, "y": 0}],
                          "links": [{"from": "begin.then", "to": "print.execute"}],
                          "defaults": [{"pin": "print.InString", "value": "Hello from the harness!"}]}),
            ("bp_compile", {"bp": bp}),
            ("editor_save", {}),
        ]
        if done >= len(steps):
            return None
        return [steps[done]]


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1234
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock LM Studio on http://127.0.0.1:{port}/v1")
    srv.serve_forever()
