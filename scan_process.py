"""Run browser work behind a wall-clock deadline, including browser cleanup."""

import os
import signal
import subprocess
import logging

import psutil


def run_bounded(command, timeout):
    """Kill the worker's entire process group on POSIX, not only its Python parent.

    Browser/driver cleanup can itself hang, so the supervisor must never call
    Playwright or depend on a cooperative cancellation inside that worker.
    """
    process = subprocess.Popen(command, start_new_session=(os.name == "posix"))
    try:
        return process.wait(timeout=timeout)
    finally:
        # Playwright can launch Chromium in a separate process group. Capture
        # and stop descendants too, before killing/reaping their parent.
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        except (psutil.Error, OSError) as exc:
            logging.getLogger(__name__).warning("Cannot enumerate browser descendants: %s", type(exc).__name__)
            descendants = []
        for child in reversed(descendants):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError) as exc:
                logging.getLogger(__name__).warning("Cannot stop browser descendant: %s", type(exc).__name__)
        # Always stop the worker group, even if descendant inspection failed.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
