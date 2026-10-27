#!/usr/bin/env python3
"""Set up and run the ICE CRM FastAPI backend for local development."""

from __future__ import annotations

import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"


def environment_value(name: str, default: str) -> str:
    if value := os.environ.get(name):
        return value
    env_file = PROJECT_ROOT / ".env"
    result = default
    if env_file.exists():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            if key.strip() != name:
                continue
            value = value.strip()
            if value.startswith(('"', "'")):
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value.strip(quote)
            else:
                value = value.split(" #", 1)[0].strip()
            result = value or default
    return result


def run_command(command: list[str]) -> None:
    print(f"Running: {subprocess.list2cmdline(command)}", flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def start_service(command: list[str]) -> subprocess.Popen:
    print(f"Running: {subprocess.list2cmdline(command)}", flush=True)
    kwargs = {"cwd": PROJECT_ROOT}
    if os.name == "posix":
        # Own process groups let cleanup reach Uvicorn's reload children too.
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, **kwargs)


def wait_for_api(process: subprocess.Popen, host: str, port: str, timeout: float = 30) -> None:
    local_host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    url = f"http://{local_host}:{port}/health/ready"
    # Readiness checks stay local, even if the shell has HTTP_PROXY configured.
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The API exited during startup. Check the output above.")
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.2)
    raise RuntimeError("The API did not become ready. Check its startup output above.")


def signal_service(process: subprocess.Popen, force: bool = False) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif process.poll() is None:
            if force:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
            else:
                process.send_signal(signal.CTRL_BREAK_EVENT)
    except ProcessLookupError:
        pass


def stop_services(processes: list[subprocess.Popen]) -> None:
    print("Stopping CRM API and mail worker...", flush=True)
    for process in reversed(processes):
        signal_service(process)
    deadline = time.monotonic() + 10
    for process in reversed(processes):
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Also remove reload descendants if their parent exited unexpectedly.
            signal_service(process, force=True)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            print(f"Process {process.pid} has not exited after shutdown.", file=sys.stderr, flush=True)


def supervise_services(python_executable: Path, host: str, port: str) -> None:
    processes: list[subprocess.Popen] = []
    previous_handlers = {}

    def request_shutdown(signum, frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, request_shutdown)
        api = start_service(
            [
                str(python_executable),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                host,
                "--port",
                port,
                "--reload",
                "--no-proxy-headers",
            ]
        )
        processes.append(api)
        wait_for_api(api, host, port)
        print("API ready. Starting the invitation/campaign mail worker...", flush=True)
        worker = start_service([str(python_executable), "-u", "-m", "app.services.worker"])
        processes.append(worker)
        print("API and mail worker are running in this terminal. Press Ctrl+C to stop both.", flush=True)
        while True:
            for name, process in (("API", api), ("Mail worker", worker)):
                status = process.poll()
                if status is not None:
                    raise RuntimeError(f"{name} exited with code {status}; stopping both services.")
            time.sleep(0.25)
    finally:
        # A second Ctrl+C must not interrupt cleanup and leave an orphan worker.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            stop_services(processes)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def main() -> None:
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12 or newer is required.")
    os.chdir(PROJECT_ROOT)
    print(f"Working directory: {PROJECT_ROOT}", flush=True)

    port = environment_value("APP_PORT", environment_value("BACKEND_PORT", "5002"))
    try:
        if not 1 <= int(port) <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("APP_PORT/BACKEND_PORT must be an integer between 1 and 65535.") from None
    host = environment_value("APP_HOST", "127.0.0.1")

    requirements = PROJECT_ROOT / "requirements.txt"
    if not requirements.is_file():
        raise SystemExit("requirements.txt is missing from the backend directory.")

    executable_dir = VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin")
    python_executable = executable_dir / ("python.exe" if platform.system() == "Windows" else "python")
    if not python_executable.is_file():
        print("Creating virtual environment...", flush=True)
        run_command([sys.executable, "-m", "venv", str(VENV_DIR)])
    else:
        print("Virtual environment already exists.", flush=True)

    print("Installing dependencies...", flush=True)
    run_command([str(python_executable), "-m", "pip", "install", "-r", str(requirements)])

    if not (PROJECT_ROOT / ".env").exists():
        print(
            "Warning: .env does not exist. Copy .env.example to .env and configure "
            "SECRET_KEY and DATABASE_URL, or provide them as environment variables. "
            "POSTGRES_PASSWORD is also required when using Docker Compose.",
            flush=True,
        )

    print("Checking PostgreSQL connection...", flush=True)
    run_command([str(python_executable), "-m", "app.check_database"])

    # Detect occupied ports before changing the schema or starting a reload worker.
    try:
        addresses = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
        for family, socktype, protocol, _, address in addresses:
            with socket.socket(family, socktype, protocol) as probe:
                probe.bind(address)
    except OSError:
        raise SystemExit(
            f"Cannot bind the API to {host}:{port}. The port may already be in use. "
            "Stop the application using it, or change APP_PORT and the frontend proxy target."
        ) from None

    print("Applying database migrations...", flush=True)
    run_command([str(python_executable), "-m", "alembic", "upgrade", "head"])

    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Starting FastAPI server at http://{display_host}:{port}...", flush=True)
    supervise_services(python_executable, host, port)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBackend stopped.", flush=True)
        raise SystemExit(130) from None
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
