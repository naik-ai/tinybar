"""A stand-in model worker for the gateway tests: a real process on a loopback port, no model.

Like the real workers it binds port 0, reports the port on the gateway's pipe (wire.report_port) and exits
when the gateway's end of its stdin closes (wire.exit_with_gateway). Behaviour from the environment:
    FAKE_READY_DELAY   seconds before it listens
    FAKE_NEVER_READY   1: /v1/models answers 503 forever
    FAKE_EXIT          exit with this code at start
    FAKE_DELAY         seconds each chat request takes
    FAKE_HEALTH        the status /health reports (ok, over_budget, footprint_unknown)
    FAKE_HEALTH_CODE   answer /health with this HTTP status instead (a failing probe)
    FAKE_STATUS        answer chat with this status (503 carries Retry-After: 30)
    FAKE_DIE_MID_STREAM 1: a streamed answer sends one event, then the process dies
    FAKE_NO_DONE       1: a streamed answer ends cleanly but without its terminal `data: [DONE]`
    FAKE_SHORT_BODY    1: a plain answer declares a longer Content-Length than it sends, then the process dies
    FAKE_SPAM          print this many bytes of log lines at start
    FAKE_TERM_DELAY    seconds it keeps running after SIGTERM
    FAKE_NO_STDIN_EXIT 1: ignore the gateway pipe (to show what an orphan would do)
A chat answer names the worker and reports usage {prompt_tokens: 7, completion_tokens: 3}; with "stream": true
it answers server-sent events, the last data chunk carrying the usage.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tinybar_models import wire


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    if not os.environ.get("FAKE_NO_STDIN_EXIT"):
        wire.exit_with_gateway()
    if os.environ.get("FAKE_EXIT"):
        sys.exit(int(os.environ["FAKE_EXIT"]))
    if os.environ.get("FAKE_TERM_DELAY"):
        delay_s = float(os.environ["FAKE_TERM_DELAY"])
        signal.signal(signal.SIGTERM, lambda *a: threading.Timer(delay_s, lambda: os._exit(0)).start())
    spam = int(os.environ.get("FAKE_SPAM", "0"))
    while spam > 0:
        sys.stdout.write("x" * 99 + "\n")
        spam -= 100
    sys.stdout.flush()
    time.sleep(float(os.environ.get("FAKE_READY_DELAY", "0")))
    key = os.environ["TINYBAR_WORKER_KEY"]
    delay = float(os.environ.get("FAKE_DELAY", "0"))

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send_json(self, status, obj, headers=None):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")  # the gateway must strip this
            self.send_header("X-Unshaped-Blocks", "5")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Authorization") != f"Bearer {key}":
                return self.send_json(401, {"error": {"message": "no"}})
            if self.path == "/health" and os.environ.get("FAKE_HEALTH_CODE"):
                return self.send_json(int(os.environ["FAKE_HEALTH_CODE"]), {"error": {"message": "sick"}})
            if self.path == "/health":
                return self.send_json(200, {"status": os.environ.get("FAKE_HEALTH", "ok"), "memory": {"inFlight": 0}})
            if self.path == "/v1/models":
                if os.environ.get("FAKE_NEVER_READY"):
                    return self.send_json(503, {"error": {"message": "loading"}})
                return self.send_json(200, {"object": "list", "data": [{"id": args.name}]})
            self.send_json(404, {"error": {"message": "no route"}})

        def do_POST(self):
            if self.headers.get("Authorization") != f"Bearer {key}":
                return self.send_json(401, {"error": {"message": "no"}})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with open(os.environ.get("FAKE_EVENTS", os.devnull), "a") as f:
                f.write(f"start {os.getpid()}\n")
            time.sleep(delay)
            if os.environ.get("FAKE_STATUS"):
                status = int(os.environ["FAKE_STATUS"])
                return self.send_json(status, {"error": {"message": "busy", "type": "server_error", "code": status}},
                                      {"Retry-After": "30"} if status == 503 else None)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                events = [{"choices": [{"delta": {"content": f"fake answer from {args.name}"}}]},
                          {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}]
                for i, e in enumerate(events):
                    chunk = f"data: {json.dumps(e)}\n\n".encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    if os.environ.get("FAKE_DIE_MID_STREAM"):
                        time.sleep(0.1)
                        os._exit(9)
                    time.sleep(0.05)
                if os.environ.get("FAKE_NO_DONE"):
                    self.wfile.write(b"0\r\n\r\n")
                    return
                end = b"data: [DONE]\n\n"
                self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(end), end))
                return
            if os.environ.get("FAKE_SHORT_BODY"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "500")
                self.end_headers()
                self.wfile.write(b'{"object": "chat.completion", "choi')
                self.wfile.flush()
                time.sleep(0.1)
                os._exit(9)
            self.send_json(200, {"object": "chat.completion", "model": args.name,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": f"fake answer from {args.name}"},
                                              "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    wire.report_port(server.server_address[1])
    server.serve_forever()


if __name__ == "__main__":
    main()
