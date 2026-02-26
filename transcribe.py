from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from send2trash import send2trash

load_dotenv()

MODEL_FALLBACKS = ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite"]


@dataclass(frozen=True)
class BucketConfig:
    source_dir: Path
    target_dir: Path


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def load_config() -> tuple[genai.Client, dict[str, BucketConfig], Path]:
    client = genai.Client(api_key=required_env("GEMINI_API_KEY"))
    notes_source = Path(required_env("NOTES_VOICE_MEMOS_DIR")).expanduser()
    course_source = Path(required_env("COURSE_VOICE_MEMOS_DIR")).expanduser()
    notes_target = Path(required_env("OBSIDIAN_NOTES_DIR")).expanduser()
    course_target = Path(required_env("OBSIDIAN_COURSE_DIR")).expanduser()
    error_log = Path(required_env("TRANSCRIBE_ERROR_LOG")).expanduser()
    buckets = {
        "notes": BucketConfig(notes_source, notes_target),
        "course": BucketConfig(course_source, course_target),
    }
    return client, buckets, error_log


def log_error(error_log: Path, message: str) -> None:
    error_log.parent.mkdir(parents=True, exist_ok=True)
    with error_log.open("a") as handle:
        handle.write(f"{message}\n")


def extract_recorded_datetime(file_path: Path) -> datetime:
    match = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{2}\.\d{2}\.\d{2})", file_path.name)
    if not match:
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H.%M.%S")


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


def ensure_local_file(file_path: Path) -> None:
    if "dataless" in file_flags(file_path):
        subprocess.run(["brctl", "download", str(file_path)], check=False)


def format_transcript_as_bullets(
    client: genai.Client,
    audio_file: Path,
    error_log: Path,
) -> str | None:
    prompt = (
        "Convert this transcript into markdown hyphen bullets. "
        "Keep bullets concise but complete by merging related fragments. "
        "Avoid over-splitting. Output bullets only."
    )
    audio_bytes = audio_file.read_bytes()
    contents = [prompt, types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp4")]
    fallback_errors: list[str] = []
    for model_name in MODEL_FALLBACKS:
        try:
            response = client.models.generate_content(model=model_name, contents=contents)
            return (response.text or "").strip()
        except errors.APIError as err:
            fallback_errors.append(f"{model_name}: {err}")
            sys.stderr.write(f"{err}\n")
            log_error(error_log, str(err))
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details = " | ".join(fallback_errors) if fallback_errors else "no model error captured"
    message = f"[{timestamp}] Failed to process file: {audio_file}. All model fallbacks failed. Errors: {details}"
    sys.stderr.write(f"{message}\n")
    log_error(error_log, message)
    return None


def append_with_spacing(target_file: Path, addition: str) -> None:
    text = addition.strip()
    if not text:
        return
    target_file.parent.mkdir(parents=True, exist_ok=True)
    current = target_file.read_text() if target_file.exists() else ""
    if current.strip():
        target_file.write_text(current.rstrip("\n") + "\n\n" + text + "\n")
        return
    target_file.write_text(text + "\n")


def process_audio(
    client: genai.Client,
    audio_file: Path,
    bucket: BucketConfig,
    error_log: Path,
) -> None:
    ensure_local_file(audio_file)
    recorded_at = extract_recorded_datetime(audio_file)
    date_str = recorded_at.strftime("%Y-%m-%d")
    target_file = bucket.target_dir / f"{date_str}.md"
    bullets = format_transcript_as_bullets(client, audio_file, error_log)
    if bullets is None:
        return
    append_with_spacing(target_file, bullets)
    send2trash(str(audio_file))


def main() -> None:
    client, buckets, error_log = load_config()
    for bucket in ("notes", "course"):
        source_dir = buckets[bucket].source_dir
        if not source_dir.exists():
            continue
        for audio_file in sorted(source_dir.iterdir()):
            if not audio_file.is_file() or audio_file.name.startswith("."):
                continue
            if audio_file.suffix.lower() != ".m4a":
                continue
            process_audio(client, audio_file, buckets[bucket], error_log)


if __name__ == "__main__":
    main()
