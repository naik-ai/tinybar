"""The gate in front of mlx-vlm's own server, and the launcher's preflight: no model, MLX or network."""

import asyncio
import importlib.util
import json

import pytest
from conftest import roomy_bound

from tinybar_models import gate, mlx_server, wire

KEY = "k" * 40
PNG_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGP4DwABAQEAsTj2FAAAAABJRU5ErkJggg=="  # a real 1x1 PNG: the gate reads its size


def good_body(**extra):
    return {"model": "qwen3.5-4b", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": PNG_URL}}, {"type": "text", "text": "read"}]}], **extra}


def run(method="POST", path="/v1/chat/completions", body=None, key=KEY, length=None, raw=None, scope_type="http", extra=()):
    """Drive the gate with one request; returns (status, payload, what the inner app saw)."""
    seen = {}

    async def inner(scope, receive, send):
        message = await receive() if scope["method"] == "POST" else {}
        seen["path"], seen["body"] = scope["path"], message.get("body")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    headers = [(b"content-type", b"application/json")]
    if key:
        headers.append((b"authorization", f"Bearer {key}".encode()))
    if length != "omit" and method == "POST":
        headers.append((b"content-length", str(len(data) if length is None else length).encode()))
    headers += list(extra)
    scope = {"type": scope_type, "method": method, "path": path, "headers": headers}
    sent, received = [], [{"type": "http.request", "body": data, "more_body": False}]

    async def receive():
        return received.pop(0) if received else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(gate.gate(inner, "qwen3.5-4b", KEY, roomy_bound("qwen3.5-4b"))(scope, receive, send))
    if scope_type != "http":
        return sent, None, seen
    status = sent[0]["status"]
    payload = json.loads(sent[1]["body"])
    return status, payload, seen


def test_only_three_routes_pass():
    assert gate.ROUTES == {("POST", "/v1/chat/completions"), ("GET", "/v1/models"), ("GET", "/health")}
    for method, path in [("POST", "/unload"), ("POST", "/v1/embeddings"), ("GET", "/v1/settings"), ("GET", "/docs"),
                         ("POST", "/v1/responses"), ("POST", "/v1/messages"), ("GET", "/metrics")]:
        status, payload, seen = run(method, path, body={})
        assert status == 404 and payload["error"]["type"] == "not_found_error" and seen == {}
    assert run("GET", "/health")[0] == 200 and run("GET", "/v1/models")[0] == 200


def test_every_route_needs_the_key_health_included():
    for method, path in sorted(gate.ROUTES):
        status, payload, seen = run(method, path, body=good_body(), key=None)
        assert status == 401 and payload["error"]["type"] == "authentication_error" and seen == {}
    assert run(body=good_body(), key="wrong" * 10)[0] == 401


def test_a_good_request_reaches_mlx_vlm_unchanged():
    body = good_body(max_tokens=10, response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}})
    status, _, seen = run(body=body)
    assert status == 200 and json.loads(seen["body"]) == body


@pytest.mark.parametrize(
    "body, message",
    [
        (good_body(adapter_path="/tmp/adapter"), "adapter_path"),
        (good_body(images=["/etc/hosts"]), "images"),
        (good_body(audio="clip.wav"), "audio"),
        (good_body(video="clip.mp4"), "video"),
        ({**good_body(), "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://169.254.169.254/x.png"}}]}]}, "data:image"),
        ({**good_body(), "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "/Users/someone/secret.png"}}]}]}, "data:image"),
        ({**good_body(), "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/svg+xml;base64,AAAA"}}]}]}, "data:image"),
        ({**good_body(), "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "x"}}]}]}, "unsupported content part"),
        ({**good_body(), "messages": [{"role": "user", "content": [{"type": "video_url", "video_url": {"url": "x"}}]}]}, "unsupported content part"),
        ({**good_body(), "messages": [{"role": "user", "content": [{"type": "input_image", "image_url": "x"}]}]}, "unsupported content part"),
        ({**good_body(), "messages": [{"role": "tool", "content": "x"}]}, "role"),
        ({**good_body(), "messages": []}, "non-empty"),
    ],
)
def test_a_refused_body_never_reaches_mlx_vlm(body, message):
    status, payload, seen = run(body=body)
    assert status == 400 and message in payload["error"]["message"] and seen == {}


def test_another_model_is_refused_before_mlx_vlm_can_unload_anything():
    status, payload, seen = run(body=good_body(model="mlx-community/Other"))
    assert status == 404 and "qwen3.5-4b" in payload["error"]["message"] and seen == {}


@pytest.mark.parametrize(
    "length, raw, status, message",
    [
        (str(wire.MAX_BODY_BYTES + 1), b"{}", 413, "over 25 MB"),
        ("omit", b"{}", 400, "Content-Length"),
        ("ten", b"{}", 400, "Content-Length"),
        ("0", b"", 400, "empty"),
        ("2", b'{"model": 1}', 400, "longer than its Content-Length"),
        (None, b"not json", 400, "not JSON"),
    ],
)
def test_the_body_length_rules(length, raw, status, message):
    got, payload, seen = run(length=length, raw=raw)
    assert got == status and message in payload["error"]["message"] and seen == {}


@pytest.mark.parametrize("value", [b"chunked", b"gzip, chunked", b"identity"])
def test_any_transfer_encoding_is_refused(value):
    status, payload, seen = run(body=good_body(), extra=[(b"transfer-encoding", value)])
    assert status == 400 and "Transfer-Encoding" in payload["error"]["message"] and seen == {}


@pytest.mark.parametrize(
    "url, message",
    [
        ("data:image/png;base64,***not base64***", "not valid base64"),
        ("data:image/png;base64," + "R0lGODlhAQABAAAAACw=", "PNG, JPEG or WebP"),  # GIF bytes labelled PNG
        ("data:image/jpeg;base64," + "PCFET0NUWVBFIGh0bWw+", "PNG, JPEG or WebP"),  # HTML bytes
    ],
)
def test_bad_image_bytes_are_a_400_in_the_gate_not_a_500_in_the_model(url, message):
    body = {**good_body(), "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    status, payload, seen = run(body=body)
    assert status == 400 and message in payload["error"]["message"] and seen == {}


def test_websockets_are_closed():
    sent, _, _ = run(scope_type="websocket", method="GET", path="/v1/realtime")
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize(
    "argv, env, message",
    [
        (["--port", "1"], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "--host 127.0.0.1 is required"),
        (["--host", "0.0.0.0"], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "refusing to bind"),
        (["--host", "127.0.0.1"], {"TINYBAR_SERVED_MODEL": "m"}, "at least 32"),
        (["--host", "127.0.0.1"], {"MLX_VLM_SERVER_API_KEY": "", "TINYBAR_SERVED_MODEL": "m"}, "at least 32"),
        (["--host", "127.0.0.1"], {"MLX_VLM_SERVER_API_KEY": "short", "TINYBAR_SERVED_MODEL": "m"}, "at least 32"),
        (["--host", "127.0.0.1", "--api-key", KEY], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "never on the command line"),
        (["--host", "127.0.0.1", "--api-k=" + KEY], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "never on the command line"),
        (["--host", "127.0.0.1", "--api", KEY], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "never on the command line"),
        (["--host", "127.0.0.1", "--hos", "0.0.0.0"], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "--host once, in full"),
        (["--host", "127.0.0.1", "--host=0.0.0.0"], {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}, "--host once, in full"),
        (["--host", "127.0.0.1"], {"MLX_VLM_SERVER_API_KEY": KEY}, "TINYBAR_SERVED_MODEL"),
    ],
)
def test_the_launcher_refuses_before_loading_anything(argv, env, message):
    with pytest.raises(SystemExit, match=message):
        mlx_server.preflight(argv, env)


def test_the_launcher_accepts_loopback_and_a_long_key():
    assert mlx_server.preflight(["--host", "127.0.0.1", "--port", "18905"],
                                {"MLX_VLM_SERVER_API_KEY": KEY, "TINYBAR_SERVED_MODEL": "m"}) == ("m", KEY)


@pytest.mark.skipif(importlib.util.find_spec("mlx_vlm") is None, reason="needs the full environment (mlx-vlm)")
def test_hardened_mlx_vlm_app_keeps_three_routes_and_no_cors():
    from fastapi.middleware.cors import CORSMiddleware

    import mlx_vlm.server as package

    def flat(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)
            yield from flat(inner.routes) if inner is not None else [r]

    app = package.app
    before = list(flat(app.router.routes))
    mlx_server.harden(app)
    after = list(flat(app.router.routes))
    pairs = {(m, r.path) for r in after for m in (getattr(r, "methods", None) or ()) if m != "HEAD"}
    assert len(before) > 50 and any(type(r).__name__ == "APIWebSocketRoute" for r in before)
    assert len(after) == 3 and pairs == gate.ROUTES
    assert not any(m.cls is CORSMiddleware for m in app.user_middleware)
