"""The gateway's key. Ported from Draftbook's serve.api_key (17 Nov 2026).

The key is TINYBAR_MODELS_API_KEY from the process environment. When that is unset, the key in
<state folder>/api-key is used, and generated there at first start (written whole, then linked into place,
so two first starts end with one key). A key under 32 characters, from either source, is refused; so is a
key file readable by group or other. The state folder is kept at mode 0700. The key is never printed,
logged or passed on a command line. Workers get a different, per-run key the gateway generates.
"""

import os
import secrets
import tempfile

from . import paths, wire

KEY_ENV = "TINYBAR_MODELS_API_KEY"


def api_key(env: dict | None = None, create: bool = True) -> tuple[str, str]:
    """(key, where it came from)."""
    env = os.environ if env is None else env
    if env.get(KEY_ENV):
        return wire.check_key(env[KEY_ENV], KEY_ENV), KEY_ENV
    path = paths.private_home(env) / "api-key"
    if path.exists():
        if os.stat(path).st_mode & 0o077:
            raise SystemExit(f"{path} is readable by group or other; run chmod 600 on it")
        return wire.check_key(path.read_text().strip(), str(path)), str(path)
    if not create:
        raise SystemExit(f"no key: set {KEY_ENV} or start the gateway once to create {path}")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".api-key.")  # mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # fails when the name exists: of two first starts, one key wins whole
        where = f"{path} (generated)"
    except FileExistsError:
        where = str(path)
    finally:
        os.unlink(tmp)
    return wire.check_key(path.read_text().strip(), str(path)), where


def worker_key() -> str:
    return secrets.token_urlsafe(32)
