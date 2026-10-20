"""Hidden Task Scheduler entry points. No bot, VPN, or report polling here."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import re
import subprocess
import time
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class TunnelAccessDenied(RuntimeError):
    """The provider explicitly refused this connection; retries must stop."""


def validate_url(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname
            or not re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname) or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment or url.port):
        raise ValueError("Use an HTTPS hostname without credentials, port, or path")
    return value.rstrip("/")


def store_ngrok_path() -> Path:
    """Use the installed Store alias, never the quarantined standalone binary."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ValueError("LOCALAPPDATA is not set")
    executable = Path(local_app_data) / "Microsoft" / "WindowsApps" / "ngrok.exe"
    if not executable.is_file():
        raise ValueError("Install ngrok from Microsoft Store for this Windows user")
    # Do not resolve the app execution alias into a version-specific package path.
    return executable


class ChildJob:
    """Ensure a stopped/killed tunnel task cannot leave its child behind."""

    def __init__(self):
        class Limits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Limits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def attach(self, pid: int):
        process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_tunnel(command: list[str], root: Path) -> int:
    job = ChildJob()
    process = None
    try:
        process = subprocess.Popen(
            command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        job.attach(process.pid)
        # Never persist raw tunnel output: config errors may contain sensitive values.
        tail = b""
        seen = set()
        while chunk := process.stdout.read(4096):
            data = tail + chunk
            for match in re.finditer(rb"ERR_NGROK_[0-9]{1,10}(?![0-9])", data):
                code = match.group().decode("ascii")
                if code not in seen and len(seen) < 32:
                    seen.add(code)
                    logger.error("ngrok diagnostic code: %s", code)
            tail = data[-64:]
        result = process.wait()
        if "ERR_NGROK_9040" in seen:
            raise TunnelAccessDenied("ERR_NGROK_9040")
        return result
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        job.close()


def supervise_tunnel(command: list[str], root: Path) -> int:
    """Recover from early network/startup failures without relying on task retries."""
    while True:
        logger.info("Starting tunnel (request inspection disabled)")
        try:
            code = run_tunnel(command, root)
        except TunnelAccessDenied:
            logger.error("ngrok refused the source IP (ERR_NGROK_9040); "
                         "retries stopped; contact provider support")
            return 0
        except OSError as error:
            # Do not repeatedly retry OS denial, quarantine, or a missing installation.
            # A zero exit prevents Task Scheduler from retrying such a failure itself.
            logger.error("Tunnel launch stopped; OS error=%s; manual check required",
                         getattr(error, "winerror", None) or error.errno)
            return 0
        logger.warning("Tunnel exited, code=%s; retry in 60 seconds", code)
        time.sleep(60)


def zrok_command(root: Path, share: str) -> list[str]:
    """Use the installed v1 binary and an existing reservation, never an account token."""
    if not re.fullmatch(r"[a-z0-9]{4,32}", share):
        raise ValueError("Use a reserved zrok share name: 4-32 lowercase letters/digits")
    executable = root / "tools" / "zrok-1.1.11" / "zrok.exe"
    if not executable.is_file():
        raise ValueError("Missing installed zrok 1.1.11 installation")
    return [str(executable), "share", "reserved", share, "--headless"]


def start_zrok(command: list[str], root: Path) -> int:
    """Keep the reserved tunnel alive across ordinary network/process exits."""
    while True:
        logger.info("Starting reserved zrok tunnel in background")
        try:
            code = run_tunnel(command, root)
        except (OSError, TunnelAccessDenied) as error:
            # An OS launch denial cannot recover through repeated attempts. Avoid
            # logging the exception text because it may contain a private path.
            logger.error("zrok launch stopped; error type=%s; manual check required",
                         type(error).__name__)
            return 0
        logger.warning("zrok exited, code=%s; retry in 60 seconds", code)
        time.sleep(60)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=("api", "tunnel", "zrok-tunnel"))
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--ngrok-url")
    parser.add_argument("--ngrok-config", type=Path)
    parser.add_argument("--zrok-share")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.app_dir.resolve(strict=True)
    env_file = root / ".env.forecast"
    if not env_file.is_file():
        raise ValueError("Missing .env.forecast")
    command = None
    if args.component == "tunnel":
        url = validate_url(args.ngrok_url or "")
        executable = store_ngrok_path()
        if not args.ngrok_config:
            raise ValueError("Missing ngrok configuration")
        config = args.ngrok_config.resolve(strict=True)
        command = [str(executable), "http", "http://127.0.0.1:8000",
                   "--url", url, "--inspect=false", "--config", str(config)]
    elif args.component == "zrok-tunnel":
        command = zrok_command(root, args.zrok_share or "")
    if args.check:
        print("STARTUP_CHECK_OK")
        return 0
    if os.name != "nt":
        raise RuntimeError("This launcher is for Windows Task Scheduler")
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        logs / f"forecast-{args.component}.log", maxBytes=1_000_000,
        backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    os.chdir(root)
    os.environ["FORECAST_ENV_FILE"] = str(env_file)
    if args.component == "api":
        import uvicorn

        uvicorn.run("ozon_forecast.api:app", host="127.0.0.1", port=8000,
                    workers=1, access_log=False, log_config=None)
    elif args.component == "zrok-tunnel":
        return start_zrok(command, root)
    else:
        return supervise_tunnel(command, root)
    # Restart via Task Scheduler even if the worker ended normally.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
