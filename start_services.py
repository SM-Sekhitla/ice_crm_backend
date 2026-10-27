"""Run the API and mail worker together in a single Docker service."""

import os
import signal
import subprocess
import sys
import time


def main():
    processes = []
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        for name, module in (("API", "app"), ("email worker", "app.services.worker")):
            if stopping:
                return 0
            process = subprocess.Popen(
                [sys.executable, "-m", module], start_new_session=True
            )
            processes.append((name, process))
            print(f"Started CRM {name} (pid={process.pid})", flush=True)
        while not stopping:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"CRM {name} exited (code={code}); stopping service", flush=True)
                    return code if code > 0 else 1
            time.sleep(0.5)
        return 0
    finally:
        for _, process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 20
        for _, process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for _, process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


if __name__ == "__main__":
    sys.exit(main())
