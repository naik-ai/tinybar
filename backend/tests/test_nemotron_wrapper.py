"""The Nemotron wrapper on the wire, with a fake reader: no model, no MLX, no network."""

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import roomy_bound

from tinybar_models import registry, nemotron_server, wire

KEY = "test-key-not-a-secret-but-long-enough-0123"
PNG_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()
RAW = (
    "<x_0.0547><y_0.0445>**INVENTED SUPPLIER ONE (TEST)**\nGSTIN: 27ZZZZZ0002Z1ZZ<x_0.5615><y_0.1258><class_Text>\n\n"
    "<x_0.0439><y_0.3016>\\begin{tabular}{cc}\n1 & 2\\\\\n\\end{tabular}<x_0.9551><y_0.4328><class_Table>"
)


class FakeReader:
    def __init__(self, raw=RAW, finish="stop"):
        self.raw, self.finish, self.calls = raw, finish, []

    def read(self, image, max_tokens):
        self.calls.append(max_tokens)
        return {"raw": self.raw, "prompt_tokens": 6, "completion_tokens": 40, "finish_reason": self.finish}


def start(reader):
    entry = registry.model(registry.load(), "nemotron-parse")
    server = wire.serve("127.0.0.1", 0, KEY, nemotron_server.routes(reader, entry, "nemotron-parse", "w@abc", roomy_bound("nemotron-parse")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(server, body, key=KEY):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def page(*extra, **fields):
    return {"model": "nemotron-parse", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": PNG_URL}}, *extra]}], **fields}


def test_boxes_are_fractions_of_the_page_itself_not_of_a_padded_canvas():
    """Recorded live: the same line drawn at the same pixel on a portrait and a wide page. Were the boxes
    fractions of a padded square canvas, the wide page's y would be off by about a third."""
    from pathlib import Path

    rec = json.loads((Path(__file__).parent / "fixtures" / "nemotron-box-frame.json").read_text())
    x, y = rec["drawnAt"]
    for page in rec["pages"].values():
        (w, h), (x1, y1, x2, y2) = page["size"], page["box"]
        assert abs(x1 * w - x) <= 0.01 * w and abs(y1 * h - y) <= 0.01 * h
        assert 0 < (y2 - y1) * h < 60  # one text line tall, in page pixels


def test_tagged_output_becomes_text_and_boxed_blocks():
    out = nemotron_server.parse(RAW)
    assert out["blocks"] == [
        {"class": "Text", "box": [0.0547, 0.0445, 0.5615, 0.1258], "text": "**INVENTED SUPPLIER ONE (TEST)**\nGSTIN: 27ZZZZZ0002Z1ZZ"},
        {"class": "Table", "box": [0.0439, 0.3016, 0.9551, 0.4328], "text": "\\begin{tabular}{cc}\n1 & 2\\\\\n\\end{tabular}"},
    ]
    assert "<x_" not in out["text"] and "<class_" not in out["text"] and "GSTIN: 27ZZZZZ0002Z1ZZ" in out["text"]


def test_a_page_gives_one_json_object_in_the_content():
    s = start(FakeReader())
    try:
        status, reply = call(s, page({"type": "text", "text": "ignored: the model follows no instructions"}))
        assert status == 200
        content = json.loads(reply["choices"][0]["message"]["content"])
        assert set(content) == {"text", "blocks"} and len(content["blocks"]) == 2
        assert reply["choices"][0]["finish_reason"] == "stop"
        assert reply["usage"] == {"prompt_tokens": 6, "completion_tokens": 40, "total_tokens": 46}
        assert reply["system_fingerprint"].startswith("nvidia/Nemotron-3.5-Parse@")
    finally:
        s.shutdown()


def test_a_cut_off_reading_says_length():
    s = start(FakeReader(finish="length"))
    try:
        assert call(s, page())[1]["choices"][0]["finish_reason"] == "length"
    finally:
        s.shutdown()


@pytest.mark.parametrize(
    "body, message",
    [
        ({"model": "nemotron-parse", "messages": [{"role": "user", "content": "read this"}]}, "exactly one page image"),
        (page({"type": "image_url", "image_url": {"url": PNG_URL}}), "exactly one page image"),
        (page(max_tokens=10**6), "max_tokens"),
        (page(max_tokens="lots"), "max_tokens"),
    ],
)
def test_a_malformed_request_is_refused_and_never_read(body, message):
    reader = FakeReader()
    s = start(reader)
    try:
        status, reply = call(s, body)
        assert status == 400 and message in reply["error"]["message"]
        assert reader.calls == []
    finally:
        s.shutdown()


def test_without_the_key_nothing_is_read():
    reader = FakeReader()
    s = start(reader)
    try:
        assert call(s, page(), key=None)[0] == 401
        assert reader.calls == []
    finally:
        s.shutdown()
