"""Entry point: serve, or `--self-test` to check the parsers and exit."""

import shutil
import sys
import threading

from . import config
from .cache import CACHES
from .collectors.nvidia import GPU_STREAM
from .journal.follower import JOURNAL
from .payload import FAST_CACHES, refresher
from .selftest import self_test
from .server import Handler, Server


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    serve()


def serve():
    # Bind first, warm second. Discovery can take a few seconds,
    # and a port that is not listening yet is indistinguishable from a dead
    # agent to the dashboard - it would log a spurious outage on every restart.
    server = Server((config.LISTEN_HOST, config.LISTEN_PORT), Handler, config.ALLOW)
    slow = tuple(n for n in CACHES if n not in FAST_CACHES)
    if shutil.which("nvidia-smi"):
        threading.Thread(target=GPU_STREAM.run, daemon=True).start()
    threading.Thread(target=refresher, args=(FAST_CACHES, 0.1), daemon=True).start()
    threading.Thread(target=refresher, args=(slow, 0.5), daemon=True).start()
    threading.Thread(target=JOURNAL.run, daemon=True).start()
    threading.Thread(target=JOURNAL.watch_target, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
