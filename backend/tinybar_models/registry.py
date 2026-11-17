"""The model registry (backend/registry.json): loading and validation.

Ported from Draftbook's services/models/draftbook_models/catalogue.py (17 Nov 2026). Changes: no fixed
port per model (the gateway gives each worker a free loopback port), a per-entry idle TTL, concurrency,
queue limit and readiness timeout with registry-wide defaults, optional per-file SHA256 checksums, an
optional `weights.external` folder registered where it lies, and a `chat` work kind.

Standard library only, so the tests run without MLX or a network.
"""

import json
import os
import re
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "registry.json"
REGISTRY_ENV = "TINYBAR_MODELS_REGISTRY"
WORK_KINDS = {"read-page", "check-reading", "extract-fields", "chat"}
SERVER_KINDS = {"mlx-vlm", "jev-wrapper", "nemotron-wrapper"}
WEIGHT_SOURCES = {"original", "mlx"}
INSTRUCTIONS = {"honoured", "partly honoured", "ignored"}
COMMERCIAL_USE = {"allowed", "not_allowed", "unread"}
NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_KEYS = {"ttlSeconds", "concurrency", "queueLimit", "readyTimeoutSeconds"}
MIN_FREE_GB = 25


class RegistryError(ValueError):
    pass


def _whole(value, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def validate_memory(data: dict) -> None:
    """Each model's memory {budgetMb, cacheLimitMb, clearCacheAfterRequest} and the registry's total."""
    total = data.get("memoryBudgetTotalMb")
    if not _whole(total):
        raise RegistryError("memoryBudgetTotalMb must be a whole number of MB above 0")
    for m in data["models"]:
        where = f"model {m.get('name', '?')}"
        mem = m.get("memory")
        if not isinstance(mem, dict) or set(mem) != {"budgetMb", "cacheLimitMb", "clearCacheAfterRequest"}:
            raise RegistryError(f"{where}: memory needs exactly budgetMb, cacheLimitMb and clearCacheAfterRequest")
        if not _whole(mem["budgetMb"]):
            raise RegistryError(f"{where}: memory.budgetMb must be a whole number of MB above 0")
        if mem["budgetMb"] > total:
            raise RegistryError(f"{where}: memory.budgetMb {mem['budgetMb']} is over memoryBudgetTotalMb {total}")
        cache = mem["cacheLimitMb"]
        if not _whole(cache, 0) or cache > mem["budgetMb"]:
            raise RegistryError(f"{where}: memory.cacheLimitMb must be a whole number of MB from 0 to budgetMb")
        if not isinstance(mem["clearCacheAfterRequest"], bool):
            raise RegistryError(f"{where}: memory.clearCacheAfterRequest must be true or false")


def _validate_defaults(data: dict) -> None:
    d = data.get("defaults")
    if not isinstance(d, dict) or set(d) != DEFAULT_KEYS | {"availableByDefault"}:
        raise RegistryError(f"defaults needs exactly {', '.join(sorted(DEFAULT_KEYS | {'availableByDefault'}))}")
    if not isinstance(d["availableByDefault"], bool):
        raise RegistryError("defaults.availableByDefault must be true or false")
    for k in DEFAULT_KEYS:
        if not _whole(d[k], 0 if k == "queueLimit" else 1):
            raise RegistryError(f"defaults.{k} must be a whole number above 0")


def validate(data: dict) -> dict:
    """Raise RegistryError on the first broken rule; return the data unchanged."""
    if data.get("schemaVersion") != 1:
        raise RegistryError("schemaVersion must be 1")
    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise RegistryError("models must be a non-empty list")
    _validate_defaults(data)
    names = set()
    for m in models:
        name = m.get("name", "")
        where = f"model {name or '?'}"
        if not isinstance(name, str) or not NAME.match(name):
            raise RegistryError(f"{where}: name must be lower-case letters, digits, '.' or '-'")
        if name in names:
            raise RegistryError(f"{where}: duplicate name")
        names.add(name)
        work = m.get("work")
        if not work or not set(work) <= WORK_KINDS:
            raise RegistryError(f"{where}: work must be a non-empty subset of {sorted(WORK_KINDS)}")
        weights = m.get("weights", {})
        for source in WEIGHT_SOURCES:
            ref = weights.get(source)
            if not ref or not REPO.match(ref.get("repo", "")) or not REVISION.match(ref.get("revision", "")):
                raise RegistryError(f"{where}: weights.{source} needs a repo and a 40-character revision")
            sums = ref.get("sha256", {})
            if not isinstance(sums, dict) or not all(isinstance(k, str) and isinstance(v, str) and SHA256.match(v) for k, v in sums.items()):
                raise RegistryError(f"{where}: weights.{source}.sha256 must map file paths to 64-character SHA256 digests")
        if not weights.get("reuse") or not set(weights["reuse"]) <= WEIGHT_SOURCES:
            raise RegistryError(f"{where}: weights.reuse must list 'original' and/or 'mlx'")
        sizes = weights.get("approxSizeGb", {})
        if not all(isinstance(sizes.get(s), (int, float)) and sizes[s] > 0 for s in WEIGHT_SOURCES):
            raise RegistryError(f"{where}: weights.approxSizeGb needs a positive size for each source")
        if weights.get("download", "mlx") not in WEIGHT_SOURCES:
            raise RegistryError(f"{where}: weights.download must be 'original' or 'mlx'")
        external = weights.get("external")
        if external is not None and (not isinstance(external, dict) or external.get("source") not in WEIGHT_SOURCES
                                     or not isinstance(external.get("path"), str) or not external["path"]):
            raise RegistryError(f"{where}: weights.external needs a source ('original' or 'mlx') and a path")
        server = m.get("server", {})
        if server.get("kind") not in SERVER_KINDS:
            raise RegistryError(f"{where}: server.kind must be one of {sorted(SERVER_KINDS)}")
        if "port" in server:
            raise RegistryError(f"{where}: server.port is not used; the gateway gives each worker a free loopback port")
        if "maxTokens" in server and not _whole(server["maxTokens"]):
            raise RegistryError(f"{where}: server.maxTokens must be a whole number above 0")
        for k in DEFAULT_KEYS:
            if k in m and not _whole(m[k], 0 if k == "queueLimit" else 1):
                raise RegistryError(f"{where}: {k} must be a whole number above 0")
        if "pinned" in m and not isinstance(m["pinned"], bool):
            raise RegistryError(f"{where}: pinned must be true or false")
        if "availableByDefault" in m and not isinstance(m["availableByDefault"], bool):
            raise RegistryError(f"{where}: availableByDefault must be true or false")
        caps = m.get("capabilities", {})
        for flag in ("imageInput", "jsonSchemaOutput", "boxes"):
            if not isinstance(caps.get(flag), bool):
                raise RegistryError(f"{where}: capabilities.{flag} must be true or false")
        if caps.get("instructions") not in INSTRUCTIONS:
            raise RegistryError(f"{where}: capabilities.instructions must be one of {sorted(INSTRUCTIONS)}")
        licence = m.get("licence")
        if not isinstance(licence, dict):
            raise RegistryError(f"{where}: licence is required, one entry per weights source")
        for source in WEIGHT_SOURCES:
            entry = licence.get(source)
            if not isinstance(entry, dict) or entry.get("commercialUse") not in COMMERCIAL_USE or not entry.get("readFrom"):
                raise RegistryError(f"{where}: licence.{source} needs commercialUse ({' | '.join(sorted(COMMERCIAL_USE))}) and readFrom")
            if entry["commercialUse"] != "unread" and not entry.get("id"):
                raise RegistryError(f"{where}: licence.{source}.id is required once the licence is read")
    validate_memory(data)
    return data


def path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(REGISTRY_ENV) or REGISTRY_PATH).expanduser()


def load(p: Path | None = None) -> dict:
    return validate(json.loads(Path(p or path()).read_text()))


def model(data: dict, name: str) -> dict:
    for m in data["models"]:
        if m["name"] == name:
            return m
    known = ", ".join(m["name"] for m in data["models"])
    raise RegistryError(f"unknown model '{name}'; the registry has: {known}")


def find(data: dict, name) -> dict | None:
    return next((m for m in data["models"] if m["name"] == name), None)


def setting(data: dict, m: dict, key: str) -> int:
    """A per-entry setting (ttlSeconds, concurrency, queueLimit, readyTimeoutSeconds) or its registry default."""
    return m.get(key, data["defaults"][key])


def available_by_default(data: dict, m: dict) -> bool:
    """Whether a model may be served before anyone turned it on or off: its own entry, else the registry default
    (false, so a newly added model serves nothing until it is turned on)."""
    return m.get("availableByDefault", data["defaults"]["availableByDefault"])


def budget_mb(m: dict) -> int:
    return m["memory"]["budgetMb"]


def over_total(data: dict, models: list[dict]) -> str | None:
    """Why this set of workers may not run together, or None. Each model counts once."""
    names = sorted({m["name"] for m in models})
    total = sum(budget_mb(model(data, n)) for n in names)
    if total > data["memoryBudgetTotalMb"]:
        return (f"the memory budgets of {', '.join(names)} sum to {total} MB, over the total of "
                f"{data['memoryBudgetTotalMb']} MB (memoryBudgetTotalMb in the registry)")
    return None
