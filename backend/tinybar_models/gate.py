"""An ASGI gate in front of mlx-vlm's own server (standard library only).

Ported from Draftbook's services/models/draftbook_models/gate.py (17 Nov 2026), unchanged in behaviour.

mlx-vlm 0.7.12 serves 54 HTTP paths and 2 WebSocket routes, fetches http image URLs, reads local file
paths, loads adapters and other models on request, and allows any origin. The gate passes through only
    POST /v1/chat/completions    GET /v1/models    GET /health
each with the key, and a chat body only when it passes wire.check_chat_body (known fields, text and inline
data:image parts only, no adapter_path, audio or video), names the served model, decodes every image to
PNG, JPEG or WebP bytes, and declares a Content-Length of at most 25 MB with no Transfer-Encoding.
Everything else gets an OpenAI error envelope and never reaches mlx-vlm.

Memory (memory.py): GET /health is answered here, never by mlx-vlm, with the status and memory figures
only. A chat request is refused with 503 while the server is over its memory budget, and MLX's buffer
cache is released after each request when the registry entry says so.
"""

import json

from . import memory, wire

ROUTES = {("POST", "/v1/chat/completions"), ("GET", "/v1/models"), ("GET", "/health")}


async def _send_json(send, status: int, payload: dict, headers: dict | None = None):
    data = json.dumps(payload).encode()
    extra = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()), *extra]})
    await send({"type": "http.response.body", "body": data})


async def _send_error(send, status: int, message: str, kind: str, headers: dict | None = None):
    await _send_json(send, status, wire.error(status, message, kind), headers)


def gate(app, served: str, key: str, bound: memory.Bound):

    async def gated(scope, receive, send):
        if scope["type"] == "lifespan":
            return await app(scope, receive, send)
        if scope["type"] != "http":  # WebSocket routes are closed
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        method, path = scope["method"], scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            if not wire.authorised(headers.get("authorization"), key):
                raise wire.WireError(401, "Invalid API key", "authentication_error")
            if (method, path) not in ROUTES:
                raise wire.WireError(404, f"no route {method} {path}", "not_found_error")
            if (method, path) == ("GET", "/health"):
                return await _send_json(send, 200, bound.health())
            if method != "POST":
                return await app(scope, receive, send)
            if "transfer-encoding" in headers:
                raise wire.WireError(400, "Transfer-Encoding is not accepted; send a Content-Length")
            length = wire.body_length(headers.get("content-length"))
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > length:
                    raise wire.WireError(400, "request body is longer than its Content-Length")
                chunks.append(chunk)
                if not message.get("more_body"):
                    break
            raw = b"".join(chunks)
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise wire.WireError(400, "request body is not JSON")
            wire.check_chat_body(body)
            if body.get("model") != served:
                raise wire.WireError(404, f"this server serves only the model '{served}'", "not_found_error")
            wire.check_images(body)
            bound.admit()
        except wire.WireError as e:
            return await _send_error(send, e.status, e.message, e.kind)
        except memory.OverBudget as e:
            return await _send_error(send, 503, bound.refusal(e), "server_error", memory.RETRY_AFTER)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        with bound.serving():
            try:
                return await app(scope, replay, send)
            finally:
                bound.after_request()

    return gated
