"""A scripted OpenAI-compatible model server for offline tests.

No AI is involved: each POST /v1/chat/completions pops the next scripted reply.
A reply is either {"text": "..."} or {"tool_calls": [{"name": ..., "arguments": {...}}]}.
Requests are appended to ``requests.jsonl`` so tests can inspect exactly what Hermes sent.
Supports both plain JSON and SSE streaming responses.

Usage: python scripted_model.py <port> <script.json> <log_dir>
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ScriptedModel:
    def __init__(self, script: list[dict], log_dir: Path):
        self.script = list(script)
        self.log_dir = log_dir
        self.lock = threading.Lock()
        self.calls = 0

    def next_reply(self, path: str, request: dict) -> dict | None:
        """Main-loop requests (they carry ``tools``) consume the script; auxiliary calls
        (titles, compression, ...) get a fixed reply so they never shift the script."""
        with self.lock:
            self.calls += 1
            with open(self.log_dir / "requests.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": path, "body": request}, sort_keys=True) + "\n")
            if not path.rstrip("/").endswith("/chat/completions"):
                return None
            if not request.get("tools"):
                return {"text": "Scripted auxiliary reply"}
            first_user = next((m.get("content") for m in request.get("messages") or []
                               if isinstance(m, dict) and m.get("role") == "user"), "") or ""
            first_user = first_user if isinstance(first_user, str) else json.dumps(first_user)
            # Entries with "for" go only to the agent whose first user message contains it
            # (e.g. a subagent's goal); the rest are served in order to everyone else.
            for i, entry in enumerate(self.script):
                if entry.get("for") and entry["for"] in first_user:
                    return self.script.pop(i)
            for i, entry in enumerate(self.script):
                if not entry.get("for"):
                    return self.script.pop(i)
            return {"text": "(script exhausted)"}


def _message(reply: dict, n: int) -> dict:
    """Script keys: text, tool_calls [{name, arguments: dict | raw str}], reasoning_content, refusal."""
    msg: dict = {"role": "assistant", "content": reply.get("text")}
    if reply.get("tool_calls"):
        msg["tool_calls"] = [
            {"id": f"call_{n}_{i}", "type": "function",
             "function": {"name": tc["name"],
                          "arguments": tc["arguments"] if isinstance(tc.get("arguments"), str) else json.dumps(tc.get("arguments", {}))}}
            for i, tc in enumerate(reply["tool_calls"])
        ]
    for key in ("reasoning_content", "refusal"):
        if key in reply:
            msg[key] = reply[key]
    return msg


def make_handler(model: ScriptedModel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/").endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": "scripted", "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
            reply = model.next_reply(self.path, request)
            if reply is None:
                self._json(200, {})
                return
            n = model.calls
            msg = _message(reply, n)
            finish = reply.get("finish_reason") or ("tool_calls" if msg.get("tool_calls") else "stop")
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            base = {"id": f"chatcmpl-{n}", "created": 1700000000 + n, "model": request.get("model", "scripted")}
            if not request.get("stream"):
                self._json(200, {**base, "object": "chat.completion",
                                 "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                 "usage": usage})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(chunk: dict) -> None:
                self.wfile.write(b"data: " + json.dumps({**base, "object": "chat.completion.chunk", **chunk}).encode() + b"\n\n")
                self.wfile.flush()

            delta: dict = {"role": "assistant"}
            if msg.get("content"):
                delta["content"] = msg["content"]
            for key in ("reasoning_content", "refusal"):
                if msg.get(key):
                    delta[key] = msg[key]
            if msg.get("tool_calls"):
                delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(msg["tool_calls"])]
            send({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def serve(port: int, script: list[dict], log_dir: Path) -> ThreadingHTTPServer:
    log_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ScriptedModel(script, log_dir)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    port, script_path, log_dir = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    serve(port, json.loads(script_path.read_text()), log_dir)
    while True:
        time.sleep(3600)
