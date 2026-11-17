"""Nemotron-3.5-Parse behind /v1/chat/completions, answering one JSON object.

Ported from Draftbook's services/models/draftbook_models/nemotron_server.py (commit 3d7d468, 17 Nov 2026); started by the gateway as a worker.

mlx-vlm's own server runs Nemotron, but answers raw Markdown wrapped in box and class tokens, and its chat
path dropped spaces the direct generate path keeps ("GSTIN:27…" against "GSTIN: 27…"). This wrapper runs
the model through mlx-vlm's generate with the model's fixed task prompt and the model card's settings
(greedy, repetition penalty 1.1 over the full history), and parses the tokens into blocks.

Request (POST /v1/chat/completions, Authorization: Bearer <key>; through the gateway, the gateway's key):
    {"model": "nemotron-parse", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,<page>"}}]}], "max_tokens": 8192}
Exactly one image. Text parts are ignored (the model follows no instructions), as are response_format and
temperature. max_tokens is honoured up to the registry's limit.

Response: a chat.completion whose choices[0].message.content is
    {"text": "<the page as Markdown, tokens removed>",
     "blocks": [{"class": "Text", "box": [x1, y1, x2, y2], "text": "..."}, ...]}
Boxes are fractions of the page width and height (0..1), top-left origin. finish_reason "length" means
the reading was cut off and must not be treated as complete.
"""

import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from . import memory, registry, wire

BLOCK = re.compile(
    r"<x_(?P<x1>[0-9.]+)><y_(?P<y1>[0-9.]+)>(?P<text>.*?)<x_(?P<x2>[0-9.]+)><y_(?P<y2>[0-9.]+)><class_(?P<cls>[A-Za-z_-]+)>",
    re.S,
)


def parse(raw: str) -> dict:
    """Blocks from Nemotron's tagged output; text outside any block is kept in "text" too."""
    blocks = [
        {"class": m["cls"], "box": [float(m["x1"]), float(m["y1"]), float(m["x2"]), float(m["y2"])], "text": m["text"].strip()}
        for m in BLOCK.finditer(raw)
    ]
    text = re.sub(r"<x_[0-9.]+>|<y_[0-9.]+>|<class_[A-Za-z_-]+>", "", raw).strip()
    return {"text": text, "blocks": blocks}


class MlxNemotronReader:
    def __init__(self, model_dir: Path, prompt: str):
        from mlx_vlm import load

        self.model, self.processor = load(str(model_dir))
        self.prompt = prompt

    def read(self, image: bytes, max_tokens: int) -> dict:
        from mlx_vlm import generate

        with tempfile.NamedTemporaryFile(suffix=".img") as f:
            f.write(image)
            f.flush()
            r = generate(self.model, self.processor, self.prompt, image=[f.name], max_tokens=max_tokens,
                         temperature=0.0, repetition_penalty=1.1, repetition_context_size=0, verbose=False)
        return {"raw": r.text, "prompt_tokens": r.prompt_tokens, "completion_tokens": r.generation_tokens,
                "finish_reason": r.finish_reason or "stop"}


def routes(reader, entry: dict, served_name: str, weights: str, bound: memory.Bound) -> dict:
    lock = bound.work_lock  # MLX evaluation is not safe across threads; requests wait their turn (health takes it too)
    original = entry["weights"]["original"]
    fingerprint = f"{original['repo']}@{original['revision']}; weights {weights}"
    limit = entry["server"].get("maxTokens", 8192)

    def chat(body: dict) -> dict:
        images, _texts = wire.user_parts(body)
        if len(images) != 1:
            raise wire.WireError(400, "Nemotron reads exactly one page image per request")
        max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or limit
        if not isinstance(max_tokens, int) or not 0 < max_tokens <= limit:
            raise wire.WireError(400, f"max_tokens must be an integer from 1 to {limit}")
        started = time.monotonic()
        with lock, memory.request(bound):
            out = reader.read(images[0], max_tokens)
        return wire.completion(served_name, parse(out["raw"]), fingerprint, prompt_tokens=out["prompt_tokens"],
                               completion_tokens=out["completion_tokens"], seconds=time.monotonic() - started,
                               finish_reason="length" if out["finish_reason"] == "length" else "stop")

    def models(_body) -> dict:
        return {"object": "list", "data": [{"id": served_name, "object": "model", "owned_by": "local"}]}

    return {("POST", "/v1/chat/completions"): chat, ("GET", "/v1/models"): models}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nemotron_server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="nemotron-parse")
    parser.add_argument("--host", default=wire.LOOPBACK)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--weights", default="unknown", help="the weights' source, reported in system_fingerprint")
    args = parser.parse_args(argv)
    wire.exit_with_gateway()
    wire.limit_image_pixels()
    wire.check_loopback(args.host)
    key = wire.check_key(os.environ.get(wire.WORKER_KEY_ENV))
    entry = registry.model(registry.load(), args.name)
    bound = memory.Bound.from_entry(entry, memory.mlx_core())
    bound.configure()
    reader = MlxNemotronReader(Path(args.model_dir), entry["fixedPrompt"])
    server = wire.serve(args.host, args.port, key, routes(reader, entry, args.name, args.weights, bound), bound.health)
    print(f"nemotron wrapper ready on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
