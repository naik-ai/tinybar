"""Shared stand-ins: MLX's memory calls, and a bound with room for every test request (every route needs one)."""

from tinybar_models import memory


class FakeMx:
    """The mlx.core memory calls memory.py uses, recorded."""

    def __init__(self, active=100, cache=50, peak=300):
        self.calls, self.active, self.cache, self.peak = [], active, cache, peak

    def set_memory_limit(self, n):
        self.calls.append(("memory_limit", n))

    def set_cache_limit(self, n):
        self.calls.append(("cache_limit", n))

    def clear_cache(self):
        self.calls.append(("clear_cache",))
        self.cache = 0

    def get_active_memory(self):
        return self.active * memory.MB

    def get_cache_memory(self):
        return self.cache * memory.MB

    def get_peak_memory(self):
        return self.peak * memory.MB


def roomy_bound(name: str) -> memory.Bound:
    """A budget no test request reaches: for tests of what a route answers, not of its memory."""
    return memory.Bound(name, 1_000_000, 0, False, FakeMx(), footprint=lambda: 1)


# --- no network in the tests: any connection that is not to this Mac is refused (CI and local alike) ---

import socket as _socket

import pytest as _pytest

_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}
_real_connect = _socket.socket.connect
_real_connect_ex = _socket.socket.connect_ex


def _check(address):
    if isinstance(address, tuple) and address and address[0] not in _LOCAL:
        raise ConnectionRefusedError(f"the tests may not use the network (tried {address[0]})")


@_pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def connect(self, address):
        _check(address)
        return _real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return _real_connect_ex(self, address)

    monkeypatch.setattr(_socket.socket, "connect", connect)
    monkeypatch.setattr(_socket.socket, "connect_ex", connect_ex)
