import copy
import json

import pytest

from tinybar_models import registry


@pytest.fixture
def data():
    return registry.load()


def test_the_committed_registry_is_valid(data):
    names = [m["name"] for m in data["models"]]
    assert names == ["nemotron-parse", "minicpm-ocr", "jev", "qwen3.5-27b", "qwen3.5-4b"]


def test_every_work_kind_has_a_model(data):
    covered = {w for m in data["models"] for w in m["work"]}
    assert covered == registry.WORK_KINDS


@pytest.mark.parametrize(
    "breaks, message",
    [
        (lambda d: d["models"].append(copy.deepcopy(d["models"][0])), "duplicate name"),
        (lambda d: d["models"][0]["weights"]["mlx"].update(revision="main"), "40-character revision"),
        (lambda d: d["models"][1]["server"].update(port=18902), "server.port is not used"),
        (lambda d: d["models"][0].update(ttlSeconds=0), "ttlSeconds must be"),
        (lambda d: d["models"][0].update(concurrency="1"), "concurrency must be"),
        (lambda d: d["models"][0].update(pinned="yes"), "pinned must be"),
        (lambda d: d["defaults"].pop("ttlSeconds"), "defaults needs exactly"),
        (lambda d: d["models"][0]["weights"]["mlx"].update(sha256={"model.safetensors": "abc"}), "sha256"),
        (lambda d: d["models"][0]["weights"].update(external={"source": "gguf", "path": "/x"}), "weights.external"),
        (lambda d: d["models"][0].update(work=["decide"]), "work must be"),
        (lambda d: d["models"][0]["server"].update(kind="docker"), "server.kind"),
        (lambda d: d["models"][0]["capabilities"].update(jsonSchemaOutput="maybe"), "jsonSchemaOutput"),
        (lambda d: d["models"][0]["weights"].update(reuse=["gguf"]), "weights.reuse"),
        (lambda d: d["models"][0].pop("licence"), "licence"),
        (lambda d: d["models"][0].update(name="Bad Name"), "name must be"),
    ],
)
def test_a_broken_registry_is_refused(data, breaks, message):
    broken = json.loads(json.dumps(data))
    breaks(broken)
    with pytest.raises(registry.RegistryError, match=message):
        registry.validate(broken)


def test_settings_come_from_the_entry_or_the_registry_defaults(data):
    assert registry.setting(data, registry.model(data, "qwen3.5-27b"), "ttlSeconds") == 300
    assert registry.setting(data, registry.model(data, "jev"), "ttlSeconds") == data["defaults"]["ttlSeconds"] == 600
    assert registry.setting(data, registry.model(data, "jev"), "concurrency") == 1


def test_nemotron_lives_in_the_store_from_its_original_weights_only(data):
    w = registry.model(data, "nemotron-parse")["weights"]
    assert "external" not in w and w["reuse"] == ["original"] and w["download"] == "original"


def test_the_registry_path_can_be_overridden():
    assert registry.path({"TINYBAR_MODELS_REGISTRY": "/x/r.json"}).as_posix() == "/x/r.json"
    assert registry.path({}) == registry.REGISTRY_PATH


def test_every_weights_source_states_its_licence_status(data):
    for m in data["models"]:
        for source in ("original", "mlx"):
            entry = m["licence"][source]
            assert entry["commercialUse"] in registry.COMMERCIAL_USE and entry["readFrom"]
    nemotron = registry.model(data, "nemotron-parse")["licence"]["original"]
    assert nemotron["commercialUse"] == "allowed" and "OpenMDW-1.1" in nemotron["id"]
    assert "without restriction" in nemotron["readFrom"]
    # conversions that declare no licence stay unread, so real use of them is refused
    assert registry.model(data, "minicpm-ocr")["licence"]["mlx"]["commercialUse"] == "unread"
    assert registry.model(data, "jev")["licence"]["mlx"]["commercialUse"] == "unread"


@pytest.mark.parametrize(
    "breaks, message",
    [
        (lambda m: m.pop("licence"), "licence is required"),
        (lambda m: m["licence"]["mlx"].update(commercialUse="yes"), "commercialUse"),
        (lambda m: m["licence"]["original"].pop("readFrom"), "readFrom"),
        (lambda m: m["licence"]["original"].update(id=None), "id is required"),
        (lambda m: m["weights"].update(download="gguf"), "weights.download"),
    ],
)
def test_a_broken_licence_entry_is_refused(data, breaks, message):
    broken = json.loads(json.dumps(data))
    breaks(broken["models"][0])
    with pytest.raises(registry.RegistryError, match=message):
        registry.validate(broken)


def test_an_unknown_model_names_the_known_ones(data):
    with pytest.raises(registry.RegistryError, match="nemotron-parse"):
        registry.model(data, "gpt-local")


def test_the_suite_cannot_reach_the_network():
    import socket

    with pytest.raises(ConnectionRefusedError, match="may not use the network"):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
