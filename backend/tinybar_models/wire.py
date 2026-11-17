"""The OpenAI chat-completions wire every worker and the gateway keep: key rules, the request gate, response
and error envelopes, and the loopback-only HTTP server the wrappers run on. Standard library only.

Ported from Draftbook's services/models/draftbook_models/wire.py (17 Nov 2026); the key's environment
variable is the worker key the gateway hands its own workers.
"""

import base64
import binascii
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple

LOOPBACK = "127.0.0.1"
WORKER_KEY_ENV = "TINYBAR_WORKER_KEY"
REPORT_FD_ENV = "TINYBAR_REPORT_FD"  # the pipe a worker writes its bound port to
PARENT_PIPE_ENV = "TINYBAR_PARENT_PIPE"  # "1": stdin is the gateway's pipe; EOF means the gateway is gone
MAX_BODY_BYTES = 25 * 1024 * 1024
MIN_KEY_LENGTH = 32
DATA_URL = re.compile(r"^data:image/(png|jpeg|webp);base64,")
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")
# The request fields a local model server accepts. Anything else (adapter_path, images, audio, video, …)
# is refused before a handler sees it.
BODY_KEYS = {
    "model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k", "min_p",
    "seed", "stop", "stream", "stream_options", "response_format", "repetition_penalty", "presence_penalty",
    "frequency_penalty",
}
MESSAGE_KEYS = {"role", "content", "name"}
ROLES = {"system", "user", "assistant"}
INTERNAL_ERROR = "internal error; the server log has the detail"


class WireError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error", headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.kind, self.headers = status, message, kind, headers or {}


def report_port(port: int, env=None) -> None:
    """Tell the gateway which port this worker bound (it binds port 0, so no key is ever sent to a guessed port)."""
    env = os.environ if env is None else env
    fd = env.get(REPORT_FD_ENV)
    n = small_int(fd)
    if n is not None:
        os.write(n, f"{port}\n".encode())
        os.close(n)


MAX_IMAGE_PIXELS = 50_000_000  # an A4 page at 600 dpi is about 35 million pixels


def small_int(text, max_digits: int = 10) -> int | None:
    """A non-negative integer from ASCII digits, at most max_digits long; None otherwise (never an int() of a
    huge or non-ASCII digit string)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > max_digits or not all("0" <= c <= "9" for c in text):
        return None
    return int(text)


def limit_image_pixels() -> None:
    """A backstop inside the workers: Pillow warns above this and refuses (DecompressionBombError) above twice it.
    The exact cap is check_images(), which every request passes in the gateway and the workers alike."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def exit_with_gateway(env=None, stdin=None, exit=os._exit) -> threading.Thread | None:
    """When started by the gateway, exit as soon as its end of our stdin closes: a gateway killed by any signal,
    SIGKILL included, takes its workers with it."""
    env = os.environ if env is None else env
    if env.get(PARENT_PIPE_ENV) != "1":
        return None
    # The raw descriptor, not sys.stdin.buffer: a daemon thread blocked inside the buffered reader holds its lock,
    # and Python aborts at exit when it cannot take that lock.
    read = stdin.read if stdin is not None else (lambda n: os.read(0, n))

    def watch():
        try:
            while read(1):
                pass
        except (OSError, ValueError):
            pass
        exit(0)

    t = threading.Thread(target=watch, daemon=True, name="exit-with-gateway")
    t.start()
    return t


def error(status: int, message: str, kind: str, code=None) -> dict:
    """The OpenAI error envelope. `code` is the HTTP status, unless a named code is given (model_not_available)."""
    return {"error": {"message": message, "type": kind, "code": status if code is None else code}}


def check_key(key: str | None, source: str = WORKER_KEY_ENV) -> str:
    if not key or len(key) < MIN_KEY_LENGTH:
        raise SystemExit(f"{source} must hold a key of at least {MIN_KEY_LENGTH} characters")
    return key


def check_loopback(host: str) -> str:
    if host != LOOPBACK:
        raise SystemExit(f"refusing to bind {host!r}: local model servers listen on {LOOPBACK} only")
    return host


def authorised(header: str | None, key: str) -> bool:
    return bool(key) and secrets.compare_digest((header or "").encode(), f"Bearer {key}".encode())


def body_length(header: str | None, limit: int = MAX_BODY_BYTES) -> int:
    """Content-Length must be present, numeric, above 0 and at most the limit (25 MB for chat)."""
    length = small_int(header)
    if length is None:
        raise WireError(400, "Content-Length is required and must be a number")
    if length == 0:
        raise WireError(400, "request body is empty")
    if length > limit:
        size = f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit} bytes"
        raise WireError(413, f"request body is over {size}")
    return length


def check_chat_body(body) -> dict:
    """The gate for /v1/chat/completions: known fields only; messages of text and inline images only."""
    if not isinstance(body, dict):
        raise WireError(400, "request body must be a JSON object")
    extra = sorted(set(body) - BODY_KEYS)
    if extra:
        raise WireError(400, f"unsupported request field(s): {', '.join(extra)}")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise WireError(400, "messages must be a non-empty list")
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES or set(m) - MESSAGE_KEYS:
            raise WireError(400, "each message needs a role (system, user, assistant) and content only")
        content = m.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise WireError(400, "message content must be a string or a list of parts")
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text" and set(part) <= {"type", "text"} and isinstance(part.get("text"), str):
                continue
            if kind == "image_url" and set(part) <= {"type", "image_url"}:
                ref = part.get("image_url")
                url = ref.get("url") if isinstance(ref, dict) and set(ref) <= {"url", "detail"} else None
                if isinstance(url, str) and DATA_URL.match(url):
                    continue
                raise WireError(400, "image_url must be a data:image/(png|jpeg|webp);base64, URL")
            raise WireError(400, f"unsupported content part: {kind!r} (text and image_url only)")
    return body


def decode_image(url: str) -> bytes:
    if not DATA_URL.match(url):
        raise WireError(400, "image_url must be a data:image/(png|jpeg|webp);base64, URL")
    try:
        data = base64.b64decode(url.split(";base64,", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        raise WireError(400, "image_url is not valid base64")
    if not data.startswith(IMAGE_MAGIC):
        raise WireError(400, "image bytes must be PNG, JPEG or WebP")
    return data


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG, JPEG or WebP header, without decoding the image; None when it cannot be read."""
    import struct

    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start-of-frame
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    except (struct.error, IndexError):
        return None
    return None


def check_images(body: dict) -> None:
    """Decode every image part and read its size from the header, so bad base64, non-image bytes, an unreadable
    header or an image over MAX_IMAGE_PIXELS is a 400 here, for every model, never a 500 or a huge decode in a worker."""
    for m in body["messages"]:
        if isinstance(m["content"], list):
            for p in m["content"]:
                if p["type"] == "image_url":
                    size = image_size(decode_image(p["image_url"]["url"]))
                    if size is None:
                        raise WireError(400, "the image's size cannot be read from its header")
                    if size[0] * size[1] > MAX_IMAGE_PIXELS:
                        raise WireError(400, f"the image is over {MAX_IMAGE_PIXELS:,} pixels")


class Reply(NamedTuple):
    """A route's answer with extra response headers (outside the JSON the contract reads)."""

    body: dict
    headers: dict


def user_parts(body: dict) -> tuple[list[bytes], list[str]]:
    """Images and texts of the last user message, in order, after the gate."""
    check_chat_body(body)
    users = [m for m in body["messages"] if m["role"] == "user"]
    if not users:
        raise WireError(400, "a user message is required")
    content = users[-1]["content"]
    if isinstance(content, str):
        return [], [content]
    images = [decode_image(p["image_url"]["url"]) for p in content if p["type"] == "image_url"]
    texts = [p["text"] for p in content if p["type"] == "text"]
    return images, texts


def completion(model: str, content: dict, fingerprint: str, prompt_tokens: int = 0, completion_tokens: int = 0,
               seconds: float | None = None, finish_reason: str = "stop") -> dict:
    """One chat.completion whose message content is one JSON object. The weights' (and renderer's) identity
    goes in the standard system_fingerprint field, so the content holds only the answer."""
    out = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": fingerprint,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }
    if seconds is not None:
        out["timings"] = {"total_seconds": round(seconds, 3)}
    return out


def _ok() -> dict:
    return {"status": "ok"}


def make_handler(key: str, routes: dict, health=_ok):
    """routes: {("GET"|"POST", path): fn(body|None) -> dict}. Every route needs the key, /health included.
    health() answers GET /health (memory.Bound.health: status and memory figures, numbers only)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "tinybar-models-worker"
        sys_version = ""

        def log_message(self, format, *args):  # no request lines (paths only, but keep worker logs quiet)
            pass

        def _send(self, status: int, payload, headers: dict | None = None):
            if isinstance(payload, Reply):
                payload, headers = payload.body, payload.headers
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str):
            path = self.path.split("?", 1)[0]
            try:
                if not authorised(self.headers.get("Authorization"), key):
                    raise WireError(401, "Invalid API key", "authentication_error")
                if (method, path) == ("GET", "/health"):
                    return self._send(200, health())
                fn = routes.get((method, path))
                if fn is None:
                    raise WireError(404, f"no route {method} {path}", "not_found_error")
                body = None
                if method == "POST":
                    if self.headers.get("Transfer-Encoding") is not None:
                        raise WireError(400, "Transfer-Encoding is not accepted; send a Content-Length")
                    length = body_length(self.headers.get("Content-Length"))
                    try:
                        body = json.loads(self.rfile.read(length))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise WireError(400, "request body is not JSON")
                self._send(200, fn(body))
            except WireError as e:
                self._send(e.status, error(e.status, e.message, e.kind), e.headers)
            except Exception:  # a model failure is an error, never a made-up answer; details stay in the log
                traceback.print_exc(file=sys.stderr)
                self._send(500, error(500, INTERNAL_ERROR, "server_error"))

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


class LoopbackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup: HTTPServer.server_bind calls socket.getfqdn(host),
    which can stall for a long time where name resolution is slow or offline (seen on CI runners: a worker
    never got to report its port). A loopback server has no use for its host's fully qualified name."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def serve(host: str, port: int, key: str, routes: dict, health=_ok) -> ThreadingHTTPServer:
    check_loopback(host)
    check_key(key)
    server = LoopbackHTTPServer((host, port), make_handler(key, routes, health))
    report_port(server.server_address[1])
    return server
