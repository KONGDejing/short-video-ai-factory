"""Independent clock so lengthy rendering cannot delay release checks."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import logging
import time
from pathlib import Path


def serve():
    from services.factory_breaking_news import scan_if_due
    from tasks.factory_tasks import schedule_factory
    directory = Path(__file__).resolve().parents[2] / ".runtime"
    directory.mkdir(mode=0o700, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    with (directory / "release-scheduler.lock").open("a") as lock:
        # Only one clock can create jobs, including across service restarts.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ThreadPoolExecutor(max_workers=1) as network:
            scan = None
            while True:
                if scan is None or scan.done():
                    if scan is not None and scan.exception():
                        logging.error('Hourly technology news check failed: %s', type(scan.exception()).__name__)
                    scan = network.submit(scan_if_due)
                try:
                    schedule_factory.run()
                except Exception:
                    logging.exception("Factory schedule tick failed; retained durable state")
                time.sleep(15)


if __name__ == "__main__":
    serve()
