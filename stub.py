"""Loopback stand-in for the model endpoint during a replay.

The replayed Hermes points its provider at this server. Main-loop calls never
reach it (Ariadne's middleware answers them from the cassette); if one does,
that is a *leak* and is reported. Auxiliary calls (titles, compression, ...)
are not routed through middleware in Hermes, so they land here and get the
recorded auxiliary text back in order, which keeps replays offline end to end.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def classify_aux(body: dict[str, Any]) -> str:
    """Guess which auxiliary task a request is, from its prompt (auxiliary calls carry no task id)."""
    text = json.dumps(body.get("messages") or body.get("system") or "", ensure_ascii=False).lower()[:6000]
    if "title" in text and ("conversation" in text or "session" in text) and "summar" not in text[:400]:
        return "title_generation"
    if any(w in text for w in ("summar", "compact", "compress", "handoff")):
        return "compression"
    if "title" in text:
        return "title_generation"
    return "other"


class Stub:
    def __init__(self, aux_texts: list[Any], on_event: Callable[[str, dict[str, Any]], None], model: str = "ariadne-replay"):
        # aux_texts: [(task, text)] in recorded order (plain strings are accepted as task "other")
        self.aux = [a if isinstance(a, tuple) else ("other", a) for a in aux_texts]
        self.on_event = on_event
        self.model = model
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.armed = True  # False during warm-up: aux calls get a placeholder and keep the recorded queue

    def _next_aux(self, task: str = "other") -> str:
        with self.lock:
            if not self.armed:
                return "Warm-up"
            for i, (t, text) in enumerate(self.aux):
                if t == task:
                    return self.aux.pop(i)[1]
            if task == "other" and self.aux:
                return self.aux.pop(0)[1]
            return "Replayed session"

    def start(self) -> int:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": stub.model, "object": "model"}]})
                else:
                    self._send(404, {"error": "ariadne stub: not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                path = self.path.rstrip("/")
                if not (path.endswith("/chat/completions") or path.endswith("/messages") or path.endswith("/responses")):
                    self._send(200, {})
                    return
                if body.get("tools") and stub.armed:
                    stub.on_event("leak", {"path": path, "note": "a main-loop model call reached the network stand-in"})
                    self._send(503, {"error": {"message": "ariadne replay: model calls are served from the cassette"}})
                    return
                armed = stub.armed
                task = classify_aux(body)
                text = stub._next_aux(task)
                if armed:
                    stub.on_event("aux_served", {"path": path, "task": task})
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                if path.endswith("/messages"):
                    msg = {"id": "ariadne-aux", "type": "message", "role": "assistant", "model": stub.model,
                           "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                           "usage": {"input_tokens": 0, "output_tokens": 0}}
                    self._send(200, msg)
                    return
                chunk_base = {"id": "ariadne-aux", "created": 0, "model": stub.model}
                message = {"role": "assistant", "content": text}
                if not body.get("stream"):
                    self._send(200, {**chunk_base, "object": "chat.completion",
                                     "choices": [{"index": 0, "message": message, "finish_reason": "stop"}], "usage": usage})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in ({"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}):
                    self.wfile.write(b"data: " + json.dumps({**chunk_base, "object": "chat.completion.chunk", **chunk}).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
