from __future__ import annotations

import fcntl
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

HOME_ENV_PATH = Path.home() / ".env"
VAULT_OPERATION_LOCK_PATH = (
    Path.home() / "Library" / "Caches" / "com.obsidian.vault-operation.lock"
)


def optional_env(name: str) -> str:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    home_value = dotenv_values(HOME_ENV_PATH).get(name)
    if isinstance(home_value, str) and home_value.strip():
        return home_value.strip()
    return ""


def configured_env(name: str) -> str:
    value = optional_env(name)
    if value:
        return value
    raise RuntimeError(f"Missing required env var: {name} (set it in {HOME_ENV_PATH})")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def log_error(error_log: Path, message: str) -> None:
    error_log.parent.mkdir(parents=True, exist_ok=True)
    with error_log.open("a") as handle:
        handle.write(f"{message}\n")


@contextmanager
def exclusive_file_lock(
    lock_path: Path,
    timeout: float = 120,
    poll_interval: float = 0.2,
) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for operation lock: {lock_path}"
                    )
                time.sleep(poll_interval)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def vault_operation_lock(
    timeout: float = 120,
    poll_interval: float = 0.2,
) -> Iterator[None]:
    with exclusive_file_lock(VAULT_OPERATION_LOCK_PATH, timeout, poll_interval):
        yield


def file_flags(file_path: Path) -> str:
    result = subprocess.run(
        ["stat", "-f", "%Sf", str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ensure_local_file(
    file_path: Path, timeout: int = 120, poll_interval: float = 2.0
) -> bool:
    if "dataless" not in file_flags(file_path):
        return True
    subprocess.run(["brctl", "download", str(file_path)], check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "dataless" not in file_flags(file_path):
            return True
        time.sleep(poll_interval)
    return False
