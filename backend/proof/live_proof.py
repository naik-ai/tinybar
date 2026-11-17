"""Live proof through the one address: ported from Draftbook's scripts/model-proof/run_proof.py and
soak_memory.py (17 Nov 2026), now against the gateway rather than one server per port.

Usage (from the repository root, gateway running; the key is read as the CLI reads it, never printed):
    uv run --directory backend python proof/live_proof.py calls PAGE OUT [--models NAME ...]
    uv run --directory backend python proof/live_proof.py soak PAGE OUT [--model jev] [--requests 20]
    uv run --directory backend python proof/live_proof.py unload NAME OUT [--wait 900]
    uv run --directory backend python proof/live_proof.py evict PAGE OUT FIRST SECOND
PAGE is an invented page (proof/draw_invented_page.py) or another page that may be used; OUT a folder outside
git. On screen and in OUT/*.json: status codes, seconds, token counts, footprints and counts only, never page
text or answers (nemotron's blocks for Jev are kept in OUT/nemotron-blocks.json and never printed).

calls   one call per model in order (nemotron-parse first: Jev scores its blocks): load and request time from
        the ledger, the worker's footprint against its budget after the call.
soak    N requests on one model; the worker's footprint after each; the slope over the second half.
unload  wait until NAME's worker unloads by itself (idle TTL); its footprint before, its process after.
evict   (a gateway started with a low memoryBudgetTotalMb) load FIRST, then call SECOND: FIRST is evicted.
"""

import argparse
import base64
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tinybar_models import cli, keys

PROMPTS = {
    "minicpm-ocr": ("Read this page and return its text.", 1024),
    "qwen3.5-4b": ("Read this page and return its text.", 512),
    "qwen3.5-27b": ("What is the first line of text on this page? Answer in under ten words.", 32),
}


def base() -> str:
    host, port = cli.address()
    return f"http://{host}:{port}"


KEY = None


def key() -> str:
    global KEY
    if KEY is None:
        KEY = keys.api_key(create=False)[0]
    return KEY


def http(method: str, path: str, body: dict | None = None, timeout: float = 3600):
    req = urllib.request.Request(base() + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json",
                                          "X-Client-Name": "live-proof"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), time.monotonic() - started, r.headers.get("X-Request-Id")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.monotonic() - started, e.headers.get("X-Request-Id")


def data_url(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def body_for(name: str, page: Path, blocks: list | None) -> dict:
    image = {"type": "image_url", "image_url": {"url": data_url(page)}}
    if name == "nemotron-parse":
        return {"model": name, "messages": [{"role": "user", "content": [image]}], "max_tokens": 4096}
    if name == "jev":
        return {"model": name, "messages": [{"role": "user", "content": [image, {"type": "text", "text": json.dumps({"blocks": blocks})}]}]}
    prompt, limit = PROMPTS[name]
    return {"model": name, "messages": [{"role": "user", "content": [image, {"type": "text", "text": prompt}]}],
            "max_tokens": limit, "temperature": 0}


def models() -> dict:
    return {m["name"]: m for m in http("GET", "/api/models")[1]["models"]}


def ledger_row(rid: str) -> dict:
    for _ in range(50):
        for r in http("GET", "/api/requests?limit=200")[1]["requests"]:
            if r["id"] == rid:
                return r
        time.sleep(0.1)
    return {}


def worker_pids(name: str) -> list[int]:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines() if "tinybar_models." in l and (f"--name {name} " in l + " " or f"--model {name} " in l + " ")]


def vm_free_mb() -> int:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(out.split("page size of ")[1].split()[0])
    pages = {l.split(":")[0]: int(l.split(":")[1].strip().rstrip(".")) for l in out.splitlines()[1:] if ":" in l and l.split(":")[1].strip().rstrip(".").isdigit()}
    return round((pages.get("Pages free", 0) + pages.get("Pages speculative", 0)) * page / 2**20)


def shape(name: str, reply: dict) -> dict:
    """Counts only: how many blocks, scores, characters; never the text."""
    try:
        content = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {}
    out = {"finishReason": reply["choices"][0].get("finish_reason"), "contentChars": len(content)}
    if name in ("nemotron-parse", "jev"):
        obj = json.loads(content)
        if "blocks" in obj:
            out["blocks"] = len(obj["blocks"])
        if "scores" in obj:
            s = [x["score"] for x in obj["scores"]]
            out.update(scores=len(s), scoreMedian=round(statistics.median(s), 3) if s else None,
                       scoresUnder0_5=sum(1 for x in s if x < 0.5))
    return out


def one_call(name: str, page: Path, blocks: list | None) -> tuple[dict, dict]:
    before = models()[name]["state"]
    status, reply, seconds, rid = http("POST", "/v1/chat/completions", body_for(name, page, blocks))
    row = ledger_row(rid) if rid else {}
    after = models()[name]
    load_s = (row.get("load_ms") or 0) / 1000
    result = {"model": name, "status": status, "stateBefore": before, "loadSeconds": round(load_s, 2),
              "queueSeconds": round((row.get("queue_ms") or 0) / 1000, 2),
              "requestSeconds": round(seconds - load_s - (row.get("queue_ms") or 0) / 1000, 2),
              "totalSeconds": round(seconds, 2), "footprintMb": after["footprintMb"], "budgetMb": after["budgetMb"],
              "promptTokens": row.get("prompt_tokens"), "completionTokens": row.get("completion_tokens"),
              "requestId": rid, **shape(name, reply)}
    if status != 200:
        result["error"] = reply.get("error", {}).get("type")
    return result, reply


def calls(args) -> list[dict]:
    out, results, blocks = Path(args.out), [], None
    out.mkdir(parents=True, exist_ok=True)
    for name in args.models:
        if name == "jev":
            blocks = blocks or json.loads((out / "nemotron-blocks.json").read_text())
        result, reply = one_call(name, Path(args.page), blocks)
        if name == "nemotron-parse" and result["status"] == 200:
            read = json.loads(reply["choices"][0]["message"]["content"])
            blocks = [{"block": i + 1, "text": b["text"], "box": b["box"]} for i, b in enumerate(read["blocks"])]
            (out / "nemotron-blocks.json").write_text(json.dumps(blocks))
        results.append(result)
        print(json.dumps(result), flush=True)
    (out / "calls.json").write_text(json.dumps(results, indent=2))
    return results


def slope(points):
    if len(points) < 2:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 2) if den else None


def soak(args) -> dict:
    out = Path(args.out)
    blocks = json.loads((out / "nemotron-blocks.json").read_text()) if args.model == "jev" else None
    samples = []
    for i in range(1, args.requests + 1):
        status, reply, seconds, rid = http("POST", "/v1/chat/completions", body_for(args.model, Path(args.page), blocks))
        m = models()[args.model]
        samples.append({"request": i, "status": status, "seconds": round(seconds, 2), "footprintMb": m["footprintMb"]})
        print(json.dumps(samples[-1]), flush=True)
    fp = [(s["request"], s["footprintMb"]) for s in samples if s["footprintMb"] is not None]
    half = len(fp) // 2
    summary = {"model": args.model, "requests": len(samples), "ok": sum(1 for s in samples if s["status"] == 200),
               "firstMb": fp[0][1], "lastMb": fp[-1][1], "minMb": min(y for _, y in fp), "maxMb": max(y for _, y in fp),
               "slopeSecondHalfMbPerRequest": slope(fp[half:]), "secondsMedian": statistics.median(s["seconds"] for s in samples),
               "budgetMb": models()[args.model]["budgetMb"]}
    print(json.dumps(summary))
    (out / f"soak-{args.model}.json").write_text(json.dumps({"summary": summary, "samples": samples}, indent=2))
    return summary


def unload(args) -> dict:
    m = models()[args.name]
    pids = worker_pids(args.name)
    before = {"state": m["state"], "footprintMb": m["footprintMb"], "ttlSeconds": m["ttlSeconds"],
              "lastUsedSecondsAgo": m["lastUsedSecondsAgo"], "pids": pids, "systemFreeMb": vm_free_mb(),
              "gatewayFootprintMb": http("GET", "/api/status")[1]["memory"]["footprintMb"]}
    print(json.dumps({"before": before}), flush=True)
    started = time.monotonic()
    while models()[args.name]["state"] in ("loaded", "unloading") and time.monotonic() - started < args.wait:
        time.sleep(2)
    time.sleep(2)
    after = {"state": models()[args.name]["state"], "waitedSeconds": round(time.monotonic() - started),
             "pidsAlive": [p for p in pids if subprocess.run(["kill", "-0", str(p)], capture_output=True).returncode == 0],
             "systemFreeMb": vm_free_mb(), "gatewayFootprintMb": http("GET", "/api/status")[1]["memory"]["footprintMb"]}
    print(json.dumps({"after": after}))
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / f"unload-{args.name}.json").write_text(json.dumps({"before": before, "after": after}, indent=2))
    return after


def evict(args) -> dict:
    out = Path(args.out)
    blocks = json.loads((out / "nemotron-blocks.json").read_text()) if (out / "nemotron-blocks.json").exists() else None
    first, _ = one_call(args.first, Path(args.page), blocks)
    first_pids = worker_pids(args.first)
    states_before = {n: m["state"] for n, m in models().items()}
    status_before = http("GET", "/api/status")[1]["memory"]
    second, _ = one_call(args.second, Path(args.page), blocks)
    time.sleep(1)
    result = {"first": first, "second": second, "statesBefore": states_before,
              "statesAfter": {n: m["state"] for n, m in models().items()}, "memoryBefore": status_before,
              "memoryAfter": http("GET", "/api/status")[1]["memory"],
              "firstPidsAliveAfter": [p for p in first_pids if subprocess.run(["kill", "-0", str(p)], capture_output=True).returncode == 0]}
    print(json.dumps(result, indent=1))
    (out / "evict.json").write_text(json.dumps(result, indent=2))
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="step", required=True)
    c = sub.add_parser("calls")
    c.add_argument("page")
    c.add_argument("out")
    c.add_argument("--models", nargs="+", default=["nemotron-parse", "minicpm-ocr", "jev", "qwen3.5-4b", "qwen3.5-27b"])
    s = sub.add_parser("soak")
    s.add_argument("page")
    s.add_argument("out")
    s.add_argument("--model", default="jev")
    s.add_argument("--requests", type=int, default=20)
    u = sub.add_parser("unload")
    u.add_argument("name")
    u.add_argument("out")
    u.add_argument("--wait", type=float, default=900)
    e = sub.add_parser("evict")
    e.add_argument("page")
    e.add_argument("out")
    e.add_argument("first")
    e.add_argument("second")
    args = p.parse_args()
    {"calls": calls, "soak": soak, "unload": unload, "evict": evict}[args.step](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
