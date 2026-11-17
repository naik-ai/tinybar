"""MiniCPM-OCR-Jev behind /v1/chat/completions: one page, all its blocks, one score per boxed block.

Ported from Draftbook's services/models/draftbook_models/jev_server.py (commit 3d7d468, 17 Nov 2026); started by the gateway as a worker.

Jev is a classifier, not a chat model: a region crop, a reading of it and an image rendered from that
reading go in; P(reading consistent with the crop) comes out. It needs a block crop: on a whole invented
page it scored a right reading 0.86 and one with the total changed 0.82, while on the total's block crop it
scored 0.98 and 0.00002 (mlx-proof.md). So this wrapper takes the page and the reader's blocks, crops each
box, renders each block's text (render.py), and scores each block.

Request (POST /v1/chat/completions, Authorization: Bearer <key>; through the gateway, the gateway's key):
    {"model": "jev", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,<page>"}},
        {"type": "text", "text": "{\\"blocks\\": [{\\"block\\": 1, \\"text\\": \\"Total: 3,471.56\\",
                                                  \\"box\\": [0.05, 0.55, 0.21, 0.57]}, ...]}"}]}]}
One page image and one text part holding a JSON object {"blocks": [...]}. Each block has an integer
"block" (unique), a "text" and a "box": [x1, y1, x2, y2] as fractions of the page width and height, top-left
origin (Nemotron's block boxes as they come), or null / absent when the reader gave none.
response_format, temperature and max_tokens are ignored.

Response: a chat.completion whose choices[0].message.content is exactly
    {"scores": [{"block": 1, "score": 0.98}, ...]}
in request order, one entry per block with a box. A block without a box, or in a script the renderer
cannot shape (render.py), gets no entry, never a guessed score; the rest of the page is still scored, and
a page where no block can be scored answers {"scores": []}. The contract's object is strict, so the blocks
left out for their script are named only in the X-Unshaped-Blocks response header (e.g. "5,9") and the
server log. system_fingerprint names the weights and the renderer (version and font files). Any failure is
an HTTP error with an OpenAI error envelope, never a score.

The text given to Jev is the block's text byte for byte; only the render image draws its markup. Scores
depend on the renderer (the published ones used a Linux Chromium renderer), so set thresholds on this
wrapper's scores.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import memory, registry, render, wire

CROP_PAD = 0.004  # a sliver of the page around each box, as a fraction of the page
UNSCORED_HEADER = "X-Unshaped-Blocks"  # boxed blocks left unscored because their script cannot be drawn


def parse_blocks(texts: list[str]) -> list[dict]:
    if len(texts) != 1:
        raise wire.WireError(400, 'Jev needs one text part holding {"blocks": [...]}')
    try:
        payload = json.loads(texts[0])
    except json.JSONDecodeError:
        raise wire.WireError(400, 'the text part must be JSON: {"blocks": [...]}')
    blocks = payload.get("blocks") if isinstance(payload, dict) else None
    if not isinstance(blocks, list) or not blocks:
        raise wire.WireError(400, '"blocks" must be a non-empty list')
    seen = set()
    for b in blocks:
        n = b.get("block") if isinstance(b, dict) else None
        if not isinstance(n, int) or isinstance(n, bool) or n in seen:
            raise wire.WireError(400, "each block needs a unique integer 'block'")
        seen.add(n)
        if not isinstance(b.get("text"), str):
            raise wire.WireError(400, f"block {n}: 'text' must be a string")
        box = b.get("box")
        if box is None:
            continue
        if (not isinstance(box, list) or len(box) != 4
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)
                or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
            raise wire.WireError(400, f"block {n}: 'box' must be [x1, y1, x2, y2] page fractions with x1 < x2 and y1 < y2")
    return blocks


class MlxJevScorer:
    """The real scorer: crops the box, renders the text (render.py), scores with mlx-vlm's Jev port."""

    def __init__(self, model_dir: Path):
        from mlx_vlm import load

        self.renderer = render.Renderer()  # fails loudly when a font is missing
        self.renderer_id = self.renderer.identity
        self.shapes_complex = self.renderer.raqm
        self.model, self.processor = load(str(model_dir))
        self._page_key, self._page = None, None

    def _open(self, page: bytes):
        import io

        from PIL import Image

        if self._page_key != hash(page):
            image = Image.open(io.BytesIO(page))  # reads the header only
            if image.width * image.height > wire.MAX_IMAGE_PIXELS:
                raise wire.WireError(400, f"the page image is over {wire.MAX_IMAGE_PIXELS:,} pixels")
            self._page, self._page_key = image.convert("RGB"), hash(page)
        return self._page

    def score_block(self, page: bytes, box: list[float], text: str) -> dict:
        from mlx_vlm.models.minicpm_ocr_jev import score

        image = self._open(page)
        w, h = image.size
        x1, y1, x2, y2 = box
        crop = image.crop((max(0, int((x1 - CROP_PAD) * w)), max(0, int((y1 - CROP_PAD) * h)),
                           min(w, int((x2 + CROP_PAD) * w) + 1), min(h, int((y2 + CROP_PAD) * h) + 1)))
        out = score(self.model, self.processor, crop, self.renderer.draw(text), text)
        return {"prob_1": out["prob_1"], "n_tokens": out["n_tokens"]}


def routes(scorer, entry: dict, served_name: str, weights: str, bound: memory.Bound) -> dict:
    lock = bound.work_lock  # MLX evaluation is not safe across threads; requests wait their turn (health takes it too)
    original = entry["weights"]["original"]
    fingerprint = (f"{original['repo']}@{original['revision']}; weights {weights}; "
                   f"renderer {getattr(scorer, 'renderer_id', 'unknown')}")

    def chat(body: dict) -> dict:
        images, texts = wire.user_parts(body)
        if len(images) != 1:
            raise wire.WireError(400, "Jev needs exactly one page image")
        blocks = parse_blocks(texts)
        shapes = getattr(scorer, "shapes_complex", False)
        unshaped = [b["block"] for b in blocks
                    if b.get("box") is not None and not shapes and render.unshapeable(b["text"])]
        started, scores, tokens = time.monotonic(), [], 0
        with lock, memory.request(bound):
            for b in blocks:
                if b.get("box") is None or b["block"] in unshaped:
                    continue  # no box, or a script the renderer cannot draw: no score entry, never a guessed one
                out = scorer.score_block(images[0], b["box"], b["text"])
                prob = float(out["prob_1"])
                if not 0.0 <= prob <= 1.0:
                    raise RuntimeError(f"block {b['block']}: scorer returned a probability outside [0, 1]")
                scores.append({"block": b["block"], "score": round(prob, 6)})
                tokens += int(out.get("n_tokens", 0))
        reply = wire.completion(served_name, {"scores": scores}, fingerprint, prompt_tokens=tokens,
                                seconds=time.monotonic() - started)
        if unshaped:
            print(f"jev: blocks {unshaped} not scored: script the renderer cannot shape", file=sys.stderr, flush=True)
        headers = {UNSCORED_HEADER: ",".join(map(str, unshaped))} if unshaped else {}
        return wire.Reply(reply, headers)

    def models(_body) -> dict:
        return {"object": "list", "data": [{"id": served_name, "object": "model", "owned_by": "local"}]}

    return {("POST", "/v1/chat/completions"): chat, ("GET", "/v1/models"): models}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev_server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="jev")
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
    scorer = MlxJevScorer(Path(args.model_dir))
    server = wire.serve(args.host, args.port, key, routes(scorer, entry, args.name, args.weights, bound), bound.health)
    print(f"jev wrapper ready on http://{args.host}:{args.port} ({scorer.renderer_id})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
