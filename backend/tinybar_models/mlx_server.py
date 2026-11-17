"""mlx-vlm's own OpenAI-compatible server (mlx-vlm 0.7.12, pinned in uv.lock), behind guards.

Ported from Draftbook's services/models/draftbook_models/mlx_server.py (17 Nov 2026). The gateway starts it as one
worker per model on a free loopback port with its own worker key; clients never reach it directly.

Before anything loads: --host must be given and be 127.0.0.1 (mlx-vlm's default is 0.0.0.0), the key must
be in MLX_VLM_SERVER_API_KEY with at least 32 characters (mlx-vlm skips auth when it is unset), and no key
may be passed on the command line.

Then: only POST /v1/chat/completions, GET /v1/models and GET /health stay routed, all behind the key and
the request gate (gate.py); the any-origin CORS layer is removed; a request naming another model gets 404
(and get_cached_model refuses it too, so nothing is unloaded or loaded); errors use the OpenAI envelope and
a 500 never carries exception text. The registry entry's memory budget is set in MLX before the model
loads, and the gate holds it per request (memory.py). The served name comes from TINYBAR_SERVED_MODEL; the other arguments
are mlx_vlm.server's.
"""

import argparse
import os
import sys

from . import memory, registry, wire
from .gate import ROUTES as GATE_ROUTES
from .gate import gate

KEY_ENV = "MLX_VLM_SERVER_API_KEY"
SERVED_ENV = "TINYBAR_SERVED_MODEL"


def preflight(argv: list[str], env) -> tuple[str, str]:
    """(served name, key), or SystemExit. Runs before mlx-vlm is imported."""
    # mlx-vlm's own parser accepts abbreviations (--api-k, --hos), so refuse every short form of the two
    # flags that matter, and any second --host.
    names = [a.split("=", 1)[0] for a in argv if a.startswith("--")]
    if any(len(n) > 2 and "--api-key".startswith(n) for n in names):
        raise SystemExit("the key goes in the environment, never on the command line")
    if any(len(n) > 2 and n != "--host" and "--host".startswith(n) for n in names) or names.count("--host") > 1:
        raise SystemExit("give --host once, in full")
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--host")
    known, _ = parser.parse_known_args(argv)
    if known.host is None:
        raise SystemExit(f"--host {wire.LOOPBACK} is required (mlx-vlm would bind 0.0.0.0)")
    wire.check_loopback(known.host)
    key = wire.check_key(env.get(KEY_ENV), KEY_ENV)
    served = env.get(SERVED_ENV)
    if not served:
        raise SystemExit(f"{SERVED_ENV} is required")
    return served, key


def guard(served: str, original):
    from fastapi import HTTPException

    def get_cached_model(model_path, *args, **kwargs):
        if model_path != served:
            raise HTTPException(status_code=404, detail=f"this server serves only the model '{served}'")
        return original(model_path, *args, **kwargs)

    return get_cached_model


def harden(app) -> None:
    """Keep three routes, drop the CORS layer, answer errors in the OpenAI envelope."""
    import logging

    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def keep(route) -> bool:
        return any((m, getattr(route, "path", None)) in GATE_ROUTES for m in (getattr(route, "methods", None) or ()))

    kept = []
    for route in app.router.routes:
        inner = getattr(route, "original_router", None)  # mlx-vlm's inference routes, included with its key check
        if inner is not None:
            inner.routes[:] = [r for r in inner.routes if keep(r)]
            if inner.routes:
                kept.append(route)
        elif keep(route):
            kept.append(route)
    app.router.routes[:] = kept
    app.user_middleware = [m for m in app.user_middleware if m.cls is not CORSMiddleware]
    app.middleware_stack = None
    log = logging.getLogger("tinybar_models.mlx_server")

    async def http_error(_request, exc):
        status = exc.status_code
        message = str(exc.detail) if status < 500 else wire.INTERNAL_ERROR
        kind = {401: "authentication_error", 404: "not_found_error"}.get(status, "invalid_request_error" if status < 500 else "server_error")
        return JSONResponse(wire.error(status, message, kind), status_code=status)

    async def invalid(_request, exc):
        return JSONResponse(wire.error(400, "request does not match the chat-completions shape", "invalid_request_error"), status_code=400)

    async def crashed(_request, exc):
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(wire.error(500, wire.INTERNAL_ERROR, "server_error"), status_code=500)

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid)
    app.add_exception_handler(Exception, crashed)


def listening_socket(host: str):
    """Bind port 0 on loopback and listen, so the port is ours before anyone is told it."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(64)
    return sock


class UvicornOnOwnSocket:
    """Stands in for the `uvicorn` module inside mlx-vlm's cli: its run() serves on our pre-bound socket (the
    --port given is ignored), reports that port to the gateway, and turns uvicorn's access log off (one line per
    request and health probe otherwise)."""

    def __init__(self, sock):
        import uvicorn

        self.sock, self._uvicorn = sock, uvicorn

    def run(self, app, host=None, port=None, **kwargs):
        kwargs.pop("reload", None)
        kwargs.pop("workers", None)
        config = self._uvicorn.Config(app, access_log=False, **kwargs)
        wire.report_port(self.sock.getsockname()[1])
        self._uvicorn.Server(config).run(sockets=[self.sock])

    def __getattr__(self, name):
        return getattr(self._uvicorn, name)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    served, key = preflight(argv, os.environ)
    wire.exit_with_gateway()
    wire.limit_image_pixels()
    entry = registry.model(registry.load(), served)
    bound = memory.Bound.from_entry(entry, memory.mlx_core())
    bound.configure()
    import mlx_vlm.server as package
    from mlx_vlm.server import cli

    package.get_cached_model = guard(served, package.get_cached_model)
    harden(package.app)
    package.app = gate(package.app, served, key, bound)  # uvicorn loads "mlx_vlm.server:app"
    cli.uvicorn = UvicornOnOwnSocket(listening_socket(wire.LOOPBACK))
    sys.argv = [sys.argv[0], *argv]
    cli.main()


if __name__ == "__main__":
    main()
