from __future__ import annotations

import hashlib
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

try:
    from .runtime_support import (
        configured_env,
        ensure_local_file,
        log_error,
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
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=request_config,
            )
            return (response.text or "").strip()
        except Exception as err:  # noqa: BLE001 - retry transient SDK failures
            attempt_error = f"attempt {attempt + 1}: {err}"
            attempt_errors.append(attempt_error)
            if not isinstance(err, errors.APIError):
                log_error(
                    error_log,
                    f"[{local_now():%Y-%m-%d %H:%M:%S}] "
                    f"Gemini request failed for {audio_file} ({attempt_error})",
                )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)

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


def audio_capture_marker(audio_file: Path) -> str:
    digest = hashlib.sha256()
    digest.update(audio_file.name.encode("utf-8"))
    digest.update(b"\0")
    with audio_file.open("rb") as audio:
        while chunk := audio.read(1024 * 1024):
            digest.update(chunk)
    return f"<!-- siri-ingest:{digest.hexdigest()} -->"


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
    marker: str,
) -> None:
    for _attempt in range(NOTE_WRITE_MAX_RETRIES):
        snapshot = read_note_snapshot(target_file)
        current_text = snapshot.decode("utf-8") if snapshot is not None else ""
        if marker in current_text:
            return
        updated_text = build_note_text(
            current_text,
            join_blocks(bullets, marker),
            section_heading,
        )
        if updated_text == current_text:
            raise RuntimeError("Transcription did not produce note content")
        if atomic_write_if_unchanged(target_file, snapshot, updated_text):
            committed_text = target_file.read_text(encoding="utf-8")
            if marker not in committed_text:
                raise RuntimeError(
                    f"Note changed before ingestion could be verified: {target_file}"
                )
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
) -> None:
    if not ensure_local_file(audio_file):
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Timed out downloading iCloud file: {audio_file}",
        )
        return
    try:
        recorded_at = extract_recorded_datetime(audio_file)
        marker = audio_capture_marker(audio_file)
    except (OSError, ValueError) as err:
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to inspect source file {audio_file}: {err}",
        )
        return
    date_str = recorded_at.strftime("%Y-%m-%d")
    target_file = daily_dir / f"{date_str}.md"
    try:
        current_snapshot = read_note_snapshot(target_file)
    except OSError as err:
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to read target note for {audio_file}: {err}",
        )
        return
    if current_snapshot is not None and marker.encode("utf-8") in current_snapshot:
        try:
            with vault_operation_lock():
                if marker in target_file.read_text(encoding="utf-8"):
                    trash_file(audio_file)
                    return
        except Exception as err:  # noqa: BLE001 - retain the source for retry
            log_error(
                error_log,
                f"[{local_now():%Y-%m-%d %H:%M:%S}] "
                f"Failed to finalize previously written file {audio_file}: {err}",
            )
            return

    try:
        bullets = format_transcript_as_bullets(client, audio_file, error_log)
    except (OSError, ValueError) as err:
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to read source file {audio_file}: {err}",
        )
        return
    if bullets is None or not normalize_block(bullets):
        return
    try:
        with vault_operation_lock():
            write_capture_to_note(
                target_file,
                bullets,
                source.section_heading,
                marker,
            )
            trash_file(audio_file)
    except Exception as err:  # noqa: BLE001 - retain source and committed marker for retry
        log_error(
            error_log,
            f"[{local_now():%Y-%m-%d %H:%M:%S}] "
            f"Failed to process file {audio_file}: {err}",
        )


def main() -> None:
    client, sources, daily_dir, error_log = load_config()
    for source in sources:
        source_dir = source.source_dir
        if not source_dir.exists():
            continue
        for audio_file in sorted(source_dir.iterdir()):
            if not audio_file.is_file() or audio_file.name.startswith("."):
                continue
            if audio_file.suffix.lower() != ".m4a":
                continue
            process_audio(client, audio_file, source, daily_dir, error_log)


if __name__ == "__main__":
    main()
