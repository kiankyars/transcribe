from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

HOME_ENV_PATH = Path.home() / ".env"
VAULT_OPERATION_LOCK_PATH = (
    Path.home() / "Library" / "Caches" / "com.obsidian.vault-operation.lock"
)
VOICE_MEMOS_IMPORT_LOCK_PATH = (
    Path.home() / "Library" / "Caches" / "com.siri.voice-memos-import.lock"
)


def configured_env(name: str) -> str:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()

    home_value = dotenv_values(HOME_ENV_PATH).get(name)
    if isinstance(home_value, str) and home_value.strip():
        return home_value.strip()

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


def load_state(state_path: Path) -> dict[str, object]:
    if not state_path.exists():
        return {"schema_version": 1, "records": {}}
    return json.loads(state_path.read_text())


def save_state(state_path: Path, state: dict[str, object]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode()
    try:
        mode = state_path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=state_path.parent,
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, state_path)
        directory_descriptor = os.open(state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


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


@contextmanager
def voice_memos_import_lock(
    timeout: float = 0,
    poll_interval: float = 0.2,
) -> Iterator[None]:
    with exclusive_file_lock(VOICE_MEMOS_IMPORT_LOCK_PATH, timeout, poll_interval):
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
