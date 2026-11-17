"""Where things live, outside the repository.

- The weights store: ~/Models (TINYBAR_MODELS_HOME). hf/ is HF_HOME (its hub/ is the Hugging Face hub cache
  layout); gguf/ and other/ are for other tools. ~/.cache/huggingface is a link to ~/Models/hf.
- The backend's own state: ~/Library/Application Support/TinybarLocal (TINYBAR_LOCAL_HOME), mode 0700:
  the key, the request ledger, logs, worker run folders, pins and the weights verification records.
  Installed Tinybar's own data is never touched.
"""

import os
from pathlib import Path

STORE_ENV = "TINYBAR_MODELS_HOME"
HOME_ENV = "TINYBAR_LOCAL_HOME"


def _env(env):
    return os.environ if env is None else env


def store(env: dict | None = None) -> Path:
    return Path(_env(env).get(STORE_ENV) or Path.home() / "Models").expanduser()


def hf_home(env: dict | None = None) -> Path:
    return store(env) / "hf"


def hf_hub(env: dict | None = None) -> Path:
    return hf_home(env) / "hub"


def home(env: dict | None = None) -> Path:
    return Path(_env(env).get(HOME_ENV) or Path.home() / "Library" / "Application Support" / "TinybarLocal").expanduser()


def private_home(env: dict | None = None) -> Path:
    """The state folder, created or tightened to mode 0700."""
    h = home(env)
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(h, 0o700)
    return h


def record_path(name: str, env: dict | None = None) -> Path:
    """weights/<name>.json: written by a pull once the weights are verified; its presence marks 'downloaded'.
    Not Path.with_suffix: names hold dots (qwen3.5-4b)."""
    return home(env) / "weights" / f"{name}.json"


def logs(env: dict | None = None) -> Path:
    return home(env) / "logs"


def run(env: dict | None = None) -> Path:
    return home(env) / "run"


def ledger(env: dict | None = None) -> Path:
    return home(env) / "ledger.sqlite3"


def key_file(env: dict | None = None) -> Path:
    return home(env) / "api-key"


def pins(env: dict | None = None) -> Path:
    return home(env) / "pins.json"


def availability(env: dict | None = None) -> Path:
    return home(env) / "availability.json"


def gateway_record(env: dict | None = None) -> Path:
    return home(env) / "gateway.json"


def tilde(p) -> str | None:
    """A path for an API response: the home folder written as ~, so no user name leaves the process."""
    if p is None:
        return None
    text, home_dir = str(p), str(Path.home())
    return "~" + text[len(home_dir):] if text == home_dir or text.startswith(home_dir + "/") else text


def tilde_text(text: str | None) -> str | None:
    """Every occurrence of the home folder in a message written as ~."""
    return None if text is None else text.replace(str(Path.home()) + "/", "~/")
