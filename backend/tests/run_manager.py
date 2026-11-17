"""For the orphan test: a manager in its own process starts one fake worker, prints the worker's pid, then waits
(to be killed with SIGKILL by the test). Usage: python tests/run_manager.py TMP_DIR [FAKE_ENV=VALUE ...]"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_gateway import Harness  # noqa: E402


def main():
    tmp = Path(sys.argv[1])
    behaviours = {"m-a": dict(a.split("=", 1) for a in sys.argv[2:])}
    h = Harness(tmp, {"m-a": 1000}, 10_000, behaviours=behaviours)
    h.manager.load("m-a")
    print(h.manager.workers["m-a"].pid, flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
