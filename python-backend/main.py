import argparse
import os
import sys
import threading
import time

from fastapi import FastAPI
import uvicorn

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "backend OK"}


def _bundle_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def run_full_selftest() -> int:
    """Storage/crypto self-test + the pytest suite, run INSIDE this binary."""
    import selftest_storage
    rc = selftest_storage.run()
    import pytest
    tests = os.path.join(_bundle_dir(), "tests")
    # -p no:cacheprovider: don't write .pytest_cache into the bundle dir.
    rc_tests = pytest.main([tests, "-q", "-p", "no:cacheprovider", "--rootdir", tests])
    print(f"SELFTEST storage_rc={rc} pytest_rc={int(rc_tests)}")
    return 0 if rc == 0 and int(rc_tests) == 0 else 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_parent_watchdog(parent_pid: int, also_kill: list[int]) -> None:
    """Exit if the desktop app dies without shutting us down (crash, SIGKILL).
    PyInstaller one-file runs us as a child of a bootloader, so a SIGKILL to
    the bootloader would otherwise orphan this process and keep port 8000
    bound. On the way out, also terminate llama-server (`also_kill`)."""
    import signal

    def loop():
        while _pid_alive(parent_pid):
            time.sleep(1.0)
        for pid in also_kill:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        os._exit(0)

    threading.Thread(target=loop, daemon=True, name="parent-watchdog").start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest-storage", action="store_true",
                        help="Run encrypted-storage self-test and exit")
    parser.add_argument("--selftest-full", action="store_true",
                        help="Storage self-test + full pytest suite, then exit")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="PID of the desktop app; exit when it disappears")
    parser.add_argument("--kill-on-exit", type=int, action="append", default=[],
                        help="PID to SIGTERM if the parent dies (llama-server)")
    args, _ = parser.parse_known_args()

    if args.selftest_storage:
        import selftest_storage
        sys.exit(selftest_storage.run())
    if args.selftest_full:
        sys.exit(run_full_selftest())

    if args.parent_pid:
        start_parent_watchdog(args.parent_pid, args.kill_on_exit)

    # Localhost-only sidecar. Never bind 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=8000)
