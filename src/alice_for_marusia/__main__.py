"""Entry point used by the on-device service."""

import signal
from .app import main, shutdown


def run():
    signal.signal(signal.SIGTERM, shutdown)
    try:
        main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
