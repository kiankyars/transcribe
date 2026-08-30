from __future__ import annotations

import os
import re
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from send2trash import send2trash


class QuotaExhausted(RuntimeError):
    """Gemini rejected the request because the daily quota is gone."""


try:
    from .runtime_support import (
        configured_env,
        ensure_local_file,
        log_error,
        optional_env,
        required_env,
        vault_operation_lock,
    )
    from .simple_endpoints import SimpleEndpoint as SourceConfig
    from .simple_endpoints import load_simple_endpoints
except ImportError:
    from runtime_support import (
        configured_env,
        ensure_local_file,
        log_error,
        optional_env,
        required_env,
        vault_operation_lock,
    )
    from simple_endpoints import SimpleEndpoint as SourceConfig
    from simple_endpoints import load_simple_endpoints

load_dotenv()

DEFAULT_ERROR_LOG = Path(__file__).resolve().parent.parent / "logs" / "siri_errors.log"
GEMINI_REQUEST_TIMEOUT_MS = 120_000
HEADING_RE = re.compile(r"(?m)^## .*$")
NOTE_WRITE_MAX_RETRIES = 5


def load_config() -> tuple[genai.Client, list[SourceConfig], Path, Path]:
    client = genai.Client(api_key=required_env("GEMINI_API_KEY"))
    daily_dir = Path(required_env("OBSIDIAN_DAILY_DIR")).expanduser()
    error_log = DEFAULT_ERROR_LOG
    sources = load_simple_endpoints(
        Path(required_env("VOICE_MEMOS_DIR_0")).expanduser(),
        Path(required_env("VOICE_MEMOS_DIR_1")).expanduser(),
    )
    return client, sources, daily_dir, error_log


def extract_recorded_datetime(file_path: Path) -> datetime:
    match = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{2}\.\d{2}\.\d{2})", file_path.name)
    if not match:
        return datetime.fromtimestamp(file_path.stat().st_mtime).astimezone()
    return datetime.strptime(
        f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H.%M.%S"
    ).astimezone()


def local_now() -> datetime:
    return datetime.now().astimezone()


def is_quota_exhausted(err: BaseException) -> bool:
    if not isinstance(err, errors.APIError):
        return False
    return err.code == 429 or "RESOURCE_EXHAUSTED" in str(err)


def format_transcript_as_bullets(
    client: genai.Client,
    audio_file: Path,
    error_log: Path,
) -> str | None:
    model_name = configured_env("GEMINI_MODEL")
    prompt = (
        "Convert this transcript into markdown hyphen bullets. "
        "Avoid over-splitting: keep consecutive sentences in the same bullet when they express one idea, and only start a new bullet when the topic clearly changes. "
        "Output bullets only. Do not include timestamps. Do not modify the transcript."
    )
    audio_bytes = audio_file.read_bytes()
    contents = [prompt, types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp4")]
    request_config = types.GenerateContentConfig(
        http_options=types.HttpOptions(timeout=GEMINI_REQUEST_TIMEOUT_MS)
    )
    max_retries = 3
    attempt_errors: list[str] = []
    active_client = client
    fallback_key = optional_env("GEMINI_API_KEY_2")
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            response = active_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=request_config,
            )
            return (response.text or "").strip()
        except Exception as err:
            attempt_error = f"attempt {attempt}: {err}"
            attempt_errors.append(attempt_error)
            if is_quota_exhausted(err) and fallback_key:
                active_client = genai.Client(api_key=fallback_key)
                fallback_key = ""
                attempt = 0
                continue
            if is_quota_exhausted(err):
                timestamp = local_now().strftime("%Y-%m-%d %H:%M:%S")
                log_error(
                    error_log,
                    f"[{timestamp}] Quota exhausted for {model_name}: "
                    f"{audio_file}. Remaining inbox files were left unprocessed. "
                    f"Errors: {attempt_error}",
                )
                raise QuotaExhausted(str(err)) from err
            if not isinstance(err, errors.APIError):
                log_error(
                    error_log,
                    f"[{local_now():%Y-%m-%d %H:%M:%S}] "
                    f"Gemini request failed for {audio_file} ({attempt_error})",
                )
            if attempt < max_retries:
                time.sleep(2 ** (attempt - 1))

    timestamp = local_now().strftime("%Y-%m-%d %H:%M:%S")
    details = (
        " | ".join(attempt_errors) if attempt_errors else "no model error captured"
    )
    message = (
        f"[{timestamp}] Failed to process file with {model_name}: "
        f"{audio_file}. Errors: {details}"
    )
    log_error(error_log, message)


def normalize_block(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def join_blocks(*blocks: str) -> str:
    cleaned = [block.strip("\n") for block in blocks if block and block.strip("\n")]
    if not cleaned:
        return ""
    return "\n\n".join(cleaned) + "\n"


def insert_into_root(current_text: str, addition: str) -> str:
    heading_match = HEADING_RE.search(current_text)
    if heading_match is None:
        return join_blocks(current_text, addition)
    return join_blocks(
        current_text[: heading_match.start()],
        addition,
        current_text[heading_match.start() :],
    )


def find_section_bounds(current_text: str, heading: str) -> tuple[int, int, int] | None:
    section_match = re.search(rf"(?m)^{re.escape(heading)}\s*$", current_text)
    if section_match is None:
        return None
    next_heading_match = HEADING_RE.search(current_text, section_match.end())
    section_end = (
        next_heading_match.start() if next_heading_match else len(current_text)
    )
    return section_match.start(), section_match.end(), section_end


def insert_into_section(current_text: str, heading: str, addition: str) -> str:
    bounds = find_section_bounds(current_text, heading)
    if bounds is None:
        heading_match = HEADING_RE.search(current_text)
        new_section = join_blocks(heading, addition)
        if heading_match is None:
            return join_blocks(current_text, new_section)
        return join_blocks(
            current_text[: heading_match.start()],
            new_section,
            current_text[heading_match.start() :],
        )

    section_start, heading_end, section_end = bounds
    section_heading = current_text[section_start:heading_end]
    section_body = current_text[heading_end:section_end]
    return join_blocks(
        current_text[:section_start],
        join_blocks(section_heading, section_body, addition),
        current_text[section_end:],
    )


def build_note_text(
    current_text: str, addition: str, section_heading: str | None
) -> str:
    normalized_addition = normalize_block(addition)
    if not normalized_addition:
        return current_text
    if section_heading is None:
        return insert_into_root(current_text, normalized_addition)
    return insert_into_section(current_text, section_heading, normalized_addition)


def read_note_snapshot(target_file: Path) -> bytes | None:
    try:
        return target_file.read_bytes()
    except FileNotFoundError:
        return None


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_if_unchanged(
    target_file: Path,
    expected: bytes | None,
    text: str,
) -> bool:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if target_file.is_symlink():
        raise RuntimeError(f"Refusing to replace symlinked note: {target_file}")

    mode = 0o644
    if expected is not None:
        try:
            mode = stat.S_IMODE(target_file.stat().st_mode)
        except FileNotFoundError:
            return False

    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target_file.name}.siri-",
        suffix=".tmp",
        dir=target_file.parent,
    )
    temp_file = Path(temp_name)
    try:
        os.fchmod(file_descriptor, mode)
        with os.fdopen(file_descriptor, "wb") as output:
            file_descriptor = -1
            output.write(text.encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())

        if read_note_snapshot(target_file) != expected:
            return False
        if target_file.is_symlink():
            raise RuntimeError(f"Refusing to replace symlinked note: {target_file}")
        if expected is None:
            try:
                os.link(temp_file, target_file)
            except FileExistsError:
                return False
            temp_file.unlink()
        else:
            os.replace(temp_file, target_file)
        fsync_directory(target_file.parent)
        return True
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temp_file.unlink(missing_ok=True)


def write_capture_to_note(
    target_file: Path,
    bullets: str,
    section_heading: str | None,
) -> None:
    for _attempt in range(NOTE_WRITE_MAX_RETRIES):
        snapshot = read_note_snapshot(target_file)
        current_text = snapshot.decode("utf-8") if snapshot is not None else ""
        updated_text = build_note_text(
            current_text,
            bullets,
            section_heading,
        )
        if updated_text == current_text:
            raise RuntimeError("Transcription did not produce note content")
        if atomic_write_if_unchanged(target_file, snapshot, updated_text):
            return
    raise RuntimeError(f"Note stayed busy during ingestion: {target_file}")


def trash_file(file_path: Path) -> None:
    send2trash(str(file_path))


def process_audio(
    client: genai.Client,
    audio_file: Path,
    source: SourceConfig,
    daily_dir: Path,
    error_log: Path,
) -> bool:
    if not ensure_local_file(audio_file):
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Timed out downloading iCloud file: {audio_file}",
        )
        return False
    try:
        recorded_at = extract_recorded_datetime(audio_file)
    except (OSError, ValueError) as err:
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to inspect source file {audio_file}: {err}",
        )
        return False
    date_str = recorded_at.strftime("%Y-%m-%d")
    target_file = daily_dir / f"{date_str}.md"
    try:
        read_note_snapshot(target_file)
    except OSError as err:
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to read target note for {audio_file}: {err}",
        )
        return False
    try:
        bullets = format_transcript_as_bullets(client, audio_file, error_log)
    except QuotaExhausted:
        raise
    except (OSError, ValueError) as err:
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to read source file {audio_file}: {err}",
        )
        return False
    if bullets is None or not normalize_block(bullets):
        return False
    try:
        with vault_operation_lock():
            write_capture_to_note(
                target_file,
                bullets,
                source.section_heading,
            )
            trash_file(audio_file)
    except Exception as err:  # noqa: BLE001 - retain the source for retry
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to process file {audio_file}: {err}",
        )
        return False
    return True


def main() -> int:
    client, sources, daily_dir, error_log = load_config()
    failed = False
    for source in sources:
        source_dir = source.source_dir
        if not source_dir.exists():
            continue
        for audio_file in sorted(source_dir.iterdir()):
            if not audio_file.is_file() or audio_file.name.startswith("."):
                continue
            if audio_file.suffix.lower() != ".m4a":
                continue
            try:
                if not process_audio(client, audio_file, source, daily_dir, error_log):
                    failed = True
            except QuotaExhausted:
                return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
