"""Memory bounds for the local model servers (003 S-06a). Standard library only; MLX is passed in.

Ported from Draftbook's services/models/draftbook_models/memory.py (17 Nov 2026); commit 3d7d468; only the restart wording
changed, and a 503 carries Retry-After.

Each registry entry carries `memory: {budgetMb, cacheLimitMb, clearCacheAfterRequest}` and the registry a
`memoryBudgetTotalMb` for every local server together (registry.py validates both).

What is measured: the process's physical footprint (`phys_footprint` from libproc, the number Activity
Monitor's Memory column and `footprint(1)` print). On Apple silicon the GPU shares RAM, and MLX's Metal
buffers count in the footprint, while RSS leaves most of them out; so the budget is held against the
footprint. MLX's own active, cache and peak figures are reported beside it.

What a server does with its entry (Bound):
- at start: mx.set_memory_limit(budget) and mx.set_cache_limit(cacheLimitMb): MLX reclaims its buffer
  cache before it grows the process past them (the memory limit is MLX's guideline, not a hard cap);
- after each request, when clearCacheAfterRequest: gc, then mx.clear_cache(), so freed buffers go back to
  the system between requests;
- before each request: if the footprint is over the budget, the cache is cleared and the footprint read
  again; if it is still over, or cannot be read at all (fail closed), the request is refused with 503; the
  gateway restarts a worker whose /health is not ok once it has no request in flight.
GET /health answers {"status": "ok" | "over_budget" | "footprint_unknown", "memory": {...}} with numbers only,
after releasing the cache when no request is in flight.
Every route builder takes its Bound as a required argument: there is no unbounded server.
"""

import ctypes
import ctypes.util
import gc
import os
import sys
import threading
from contextlib import contextmanager

from . import wire

MB = 1024 * 1024
RETRY_AFTER = {"Retry-After": "30"}  # an over-budget worker is restarted by the gateway once it is idle
_RUSAGE_INFO_V2 = 2


class _RusageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups",
        "ri_child_interrupt_wkups", "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread",
        "ri_diskio_byteswritten")]


_libproc = None


def _proc():
    global _libproc
    if _libproc is None and sys.platform == "darwin":
        _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    return _libproc


def usage_mb(pid: int | None = None) -> dict | None:
    """{"footprintMb", "residentMb"} of a process (this one by default), or None when it cannot be read."""
    lib = _proc()
    if lib is None:
        return None
    info = _RusageInfoV2()
    if lib.proc_pid_rusage(os.getpid() if pid is None else pid, _RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
        return None
    return {"footprintMb": round(info.ri_phys_footprint / MB), "residentMb": round(info.ri_resident_size / MB)}


def footprint_mb(pid: int | None = None) -> int | None:
    u = usage_mb(pid)
    return None if u is None else u["footprintMb"]


class OverBudget(Exception):
    """Over the budget after a release, or the footprint cannot be read (footprint None): refused either way."""

    def __init__(self, footprint: int | None, budget: int):
        super().__init__(f"footprint {footprint} MB is over the memory budget of {budget} MB" if footprint is not None
                         else f"the footprint cannot be read, so the memory budget of {budget} MB cannot be held")
        self.footprint, self.budget = footprint, budget


class Bound:
    """One server's memory budget. `mx` is mlx.core (or a stand-in in tests); `footprint` reads this process.
    It fails closed: a footprint that cannot be read refuses work, as an over-budget one does."""

    def __init__(self, name: str, budget_mb: int, cache_limit_mb: int, clear_after: bool, mx, footprint=footprint_mb):
        self.name, self.budget_mb, self.cache_limit_mb, self.clear_after = name, budget_mb, cache_limit_mb, clear_after
        self.mx, self._footprint = mx, footprint
        self._lock = threading.Lock()
        self._in_flight = 0
        # The wrappers' route lock: one request's MLX work at a time. health() takes it without waiting before it
        # releases the cache, so a release never runs beside a request's work (the gate's mlx-vlm holds no such lock).
        self.work_lock = threading.Lock()

    @classmethod
    def from_entry(cls, entry: dict, mx, footprint=footprint_mb) -> "Bound":
        mem = entry["memory"]
        return cls(entry["name"], mem["budgetMb"], mem["cacheLimitMb"], mem["clearCacheAfterRequest"], mx, footprint)

    def configure(self) -> None:
        """At server start, before the model loads."""
        self.mx.set_memory_limit(self.budget_mb * MB)
        self.mx.set_cache_limit(self.cache_limit_mb * MB)

    def release(self) -> None:
        gc.collect()
        self.mx.clear_cache()

    def after_request(self) -> None:
        if self.clear_after:
            self.release()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @contextmanager
    def serving(self):
        """Counts a request as in flight while its model work runs (the gateway never restarts a busy worker)."""
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def admit(self) -> None:
        """Raise OverBudget when the footprint is over the budget even after the cache is released, or unreadable."""
        fp = self._footprint()
        if fp is not None and fp <= self.budget_mb:
            return
        self.release()
        fp = self._footprint()
        if fp is None or fp > self.budget_mb:
            raise OverBudget(fp, self.budget_mb)

    def refusal(self, e: OverBudget) -> str:
        why = f"over its memory budget ({e.footprint} MB of {e.budget} MB)" if e.footprint is not None else "unable to read its own memory footprint"
        return f"{self.name} is {why} and takes no new work; the gateway restarts it once it is idle"

    def health(self) -> dict:
        """Numbers only: nothing about the model, its files, its requests or its settings beyond the budget. When no
        request is in flight the cache is released first, so the footprint read is what the process holds."""
        busy = self._in_flight
        if busy == 0:
            if self.work_lock.acquire(blocking=False):
                try:
                    self.release()
                finally:
                    self.work_lock.release()
            else:
                busy = 1  # a request holds the route lock: no release, reported in flight
        fp = self._footprint()
        out = {"footprintMb": fp, "budgetMb": self.budget_mb, "inFlight": busy,
               "activeMb": round(self.mx.get_active_memory() / MB), "cacheMb": round(self.mx.get_cache_memory() / MB),
               "peakMb": round(self.mx.get_peak_memory() / MB)}
        status = "footprint_unknown" if fp is None else "over_budget" if fp > self.budget_mb else "ok"
        return {"status": status, "memory": out}


@contextmanager
def request(bound: Bound):
    """Around one request's model work: refuse it when over budget or unreadable (503), count it in flight, release
    the cache after it."""
    try:
        bound.admit()
    except OverBudget as e:
        print(f"{bound.name}: refused a request: {e}", file=sys.stderr, flush=True)
        raise wire.WireError(503, bound.refusal(e), "server_error", RETRY_AFTER)
    with bound.serving():
        try:
            yield
        finally:
            bound.after_request()


def mlx_core():
    import mlx.core as mx

    return mx
