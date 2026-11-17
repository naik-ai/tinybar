"""The Jev wrapper on the wire, with a fake scorer: no model, no MLX, no network."""

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import roomy_bound

from tinybar_models import registry, jev_server, wire

KEY = "test-key-not-a-secret-but-long-enough-0123"
PAGE = b"\x89PNG\r\n\x1a\n" + b"PAGE" * 4
PAGE_URL = "data:image/png;base64," + base64.b64encode(PAGE).decode()
TOTAL = {"block": 7, "text": "Total: 3,471.56", "box": [0.05, 0.55, 0.21, 0.57]}
HEAD = {"block": 2, "text": "TAX INVOICE", "box": [0.05, 0.17, 0.25, 0.19]}
NO_BOX = {"block": 3, "text": "a line the reader gave no box for"}
NULL_BOX = {"block": 4, "text": "another", "box": None}


class FakeScorer:
    renderer_id = "fake-renderer/0"

    def __init__(self, prob=0.875, fail=False, shapes_complex=False):
        self.prob, self.fail, self.calls, self.shapes_complex = prob, fail, [], shapes_complex

    def score_block(self, page, box, text):
        self.calls.append((page, box, text))
        if self.fail:
            raise RuntimeError("model blew up")
        return {"prob_1": self.prob if not callable(self.prob) else self.prob(text), "n_tokens": 100}


def start(scorer):
    entry = registry.model(registry.load(), "jev")
    server = wire.serve("127.0.0.1", 0, KEY, jev_server.routes(scorer, entry, "jev", "test/weights@abc", roomy_bound("jev")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def scorer():
    return FakeScorer(prob=lambda text: 0.98 if text.startswith("Total") else 0.25)


@pytest.fixture
def server(scorer):
    s = start(scorer)
    yield s
    s.shutdown()


def call(server, path="/v1/chat/completions", body=None, key=KEY):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def request(blocks, *, image=PAGE_URL, text=None, extra_parts=()):
    parts = [{"type": "image_url", "image_url": {"url": image}},
             {"type": "text", "text": text if text is not None else json.dumps({"blocks": blocks})}, *extra_parts]
    return {"model": "jev", "messages": [{"role": "user", "content": parts}]}


def content(reply):
    return json.loads(reply["choices"][0]["message"]["content"])


def test_each_boxed_block_is_scored_in_request_order(server, scorer):
    status, reply = call(server, body=request([TOTAL, HEAD]))
    assert status == 200 and reply["object"] == "chat.completion" and reply["model"] == "jev"
    assert content(reply) == {"scores": [{"block": 7, "score": 0.98}, {"block": 2, "score": 0.25}]}
    assert scorer.calls == [(PAGE, TOTAL["box"], TOTAL["text"]), (PAGE, HEAD["box"], HEAD["text"])]
    revision = registry.model(registry.load(), "jev")["weights"]["original"]["revision"]
    assert reply["system_fingerprint"] == f"openbmb/MiniCPM-OCR-Jev@{revision}; weights test/weights@abc; renderer fake-renderer/0"
    assert reply["usage"]["prompt_tokens"] == 200


def test_a_block_without_a_box_gets_no_entry_and_is_never_scored(server, scorer):
    status, reply = call(server, body=request([NO_BOX, TOTAL, NULL_BOX]))
    assert status == 200
    assert content(reply) == {"scores": [{"block": 7, "score": 0.98}]}
    assert [c[2] for c in scorer.calls] == [TOTAL["text"]]


def test_a_page_with_no_boxes_at_all_gets_an_empty_list_not_a_page_score(server, scorer):
    status, reply = call(server, body=request([NO_BOX, NULL_BOX]))
    assert status == 200 and content(reply) == {"scores": []}
    assert scorer.calls == []


def test_without_the_key_nothing_answers_health_included(server, scorer):
    assert call(server, body=request([TOTAL]), key=None)[0] == 401
    assert call(server, body=request([TOTAL]), key="wrong")[0] == 401
    assert call(server, path="/v1/models", key=None)[0] == 401
    assert call(server, path="/health", key=None)[0] == 401
    assert call(server, path="/health") == (200, {"status": "ok"})
    assert scorer.calls == []


GIF_URL = "data:image/png;base64," + base64.b64encode(b"GIF89a....").decode()


@pytest.mark.parametrize(
    "body, message",
    [
        (request([TOTAL], extra_parts=[{"type": "image_url", "image_url": {"url": PAGE_URL}}]), "exactly one page image"),
        ({"model": "jev", "messages": [{"role": "user", "content": [{"type": "text", "text": "{}"}]}]}, "exactly one page image"),
        (request([TOTAL], text="Total: 3,471.56"), "must be JSON"),
        (request([TOTAL], extra_parts=[{"type": "text", "text": "more"}]), "one text part"),
        (request([]), "non-empty list"),
        (request([TOTAL, dict(HEAD, block=7)]), "unique integer"),
        (request([dict(TOTAL, block="7")]), "unique integer"),
        (request([dict(TOTAL, text=None)]), "'text' must be a string"),
        (request([dict(TOTAL, box=[0.2, 0.5, 0.1, 0.6])]), "'box' must be"),
        (request([dict(TOTAL, box=[0.1, 0.5, 1.4, 0.6])]), "'box' must be"),
        (request([dict(TOTAL, box=[0.1, 0.5, 0.2])]), "'box' must be"),
        (request([dict(TOTAL, box=[10, 20, 30, 40])]), "'box' must be"),
        (request([TOTAL], image="https://example.com/a.png"), "data:image"),
        (request([TOTAL], image=GIF_URL), "PNG, JPEG or WebP"),
    ],
)
def test_a_malformed_request_is_refused_and_nothing_is_scored(body, message):
    scorer = FakeScorer()
    s = start(scorer)
    try:
        status, reply = call(s, body=body)
        assert status == 400
        assert message in reply["error"]["message"]
        assert scorer.calls == []
    finally:
        s.shutdown()


def test_a_model_failure_is_an_error_not_a_low_score():
    s = start(FakeScorer(fail=True))
    try:
        status, reply = call(s, body=request([TOTAL]))
        assert status == 500
        assert "choices" not in reply and reply["error"]["type"] == "server_error"
    finally:
        s.shutdown()


def test_a_probability_outside_zero_to_one_is_an_error():
    s = start(FakeScorer(prob=1.5))
    try:
        assert call(s, body=request([TOTAL]))[0] == 500
    finally:
        s.shutdown()


def test_models_lists_only_the_served_name(server):
    assert call(server, path="/v1/models")[1]["data"] == [{"id": "jev", "object": "model", "owned_by": "local"}]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "localhost", "::"])
def test_a_wrapper_refuses_a_non_loopback_bind(host):
    with pytest.raises(SystemExit, match="refusing to bind"):
        wire.serve(host, 0, KEY, {})


@pytest.mark.parametrize("key", ["", "short-key"])
def test_a_wrapper_refuses_to_start_without_a_long_key(key):
    with pytest.raises(SystemExit, match="at least 32"):
        wire.serve("127.0.0.1", 0, key, {})


def test_the_model_gets_the_block_text_byte_for_byte_markup_and_all(server, scorer):
    marked = {"block": 9, "text": "**TAX INVOICE**\n\\begin{tabular}{cc}\n\\thead{Qty} & 10\\\\\n\\end{tabular}", "box": [0.1, 0.1, 0.5, 0.3]}
    assert call(server, body=request([marked]))[0] == 200
    assert scorer.calls[0][2] == marked["text"]


def raw_call(server, body):
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read()), r.headers


@pytest.mark.parametrize("text", ["कुल: 3,471.56", "કુલ: 3,471.56"])
def test_a_block_the_renderer_cannot_shape_gets_no_entry_and_the_rest_is_scored(text):
    scorer = FakeScorer(prob=lambda t: 0.98 if t.startswith("Total") else 0.25, shapes_complex=False)
    s = start(scorer)
    try:
        status, reply, headers = raw_call(s, request([TOTAL, {"block": 5, "text": text, "box": [0.1, 0.1, 0.2, 0.2]}, HEAD]))
        assert status == 200
        assert content(reply) == {"scores": [{"block": 7, "score": 0.98}, {"block": 2, "score": 0.25}]}
        assert headers["X-Unshaped-Blocks"] == "5"
        assert [c[2] for c in scorer.calls] == [TOTAL["text"], HEAD["text"]]
    finally:
        s.shutdown()


def test_a_page_whose_every_block_is_unshapeable_answers_an_empty_list():
    scorer = FakeScorer(shapes_complex=False)
    s = start(scorer)
    try:
        blocks = [{"block": 1, "text": "कुल", "box": [0.1, 0.1, 0.2, 0.2]}, {"block": 2, "text": "કુલ", "box": [0.3, 0.1, 0.4, 0.2]}]
        status, reply, headers = raw_call(s, request(blocks))
        assert status == 200 and content(reply) == {"scores": []} and headers["X-Unshaped-Blocks"] == "1,2"
        assert scorer.calls == []
    finally:
        s.shutdown()


def test_a_renderer_that_shapes_scores_every_script():
    scorer = FakeScorer(shapes_complex=True)
    s = start(scorer)
    try:
        status, reply, headers = raw_call(s, request([{"block": 5, "text": "कुल", "box": [0.1, 0.1, 0.2, 0.2]}]))
        assert content(reply) == {"scores": [{"block": 5, "score": 0.875}]} and "X-Unshaped-Blocks" not in headers
    finally:
        s.shutdown()


def test_a_wrapper_refuses_transfer_encoding(server, scorer):
    import socket

    with socket.create_connection(("127.0.0.1", server.server_address[1])) as conn:
        conn.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {KEY}\r\n"
                     "Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}".encode())
        status_line = conn.recv(200).split(b"\r\n")[0]
    assert b" 400 " in status_line and scorer.calls == []


def test_an_unboxed_block_in_any_script_is_simply_not_scored(server, scorer):
    status, reply = call(server, body=request([TOTAL, {"block": 5, "text": "कुल"}]))
    assert status == 200 and content(reply) == {"scores": [{"block": 7, "score": 0.98}]}


def test_a_500_never_echoes_exception_text_or_paths():
    class Leaky(FakeScorer):
        def score_block(self, page, box, text):
            raise RuntimeError("/Users/someone/secret/path exploded")

    s = start(Leaky())
    try:
        status, reply = call(s, body=request([TOTAL]))
        assert status == 500 and "/Users" not in json.dumps(reply) and "exploded" not in json.dumps(reply)
    finally:
        s.shutdown()


def test_a_body_with_an_unknown_field_is_refused(server, scorer):
    status, reply = call(server, body={**request([TOTAL]), "adapter_path": "/tmp/a"})
    assert status == 400 and "adapter_path" in reply["error"]["message"] and scorer.calls == []
