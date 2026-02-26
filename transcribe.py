import os
import re
import subprocess
from datetime import datetime
from google import genai
from google.genai import errors, types
from dotenv import load_dotenv
import sys
from send2trash import send2trash

# Load environment variables
load_dotenv()

# Configuration
SOURCE_BASE_DIR = os.path.expanduser("/Users/kian/Library/Mobile Documents/com~apple~CloudDocs/Music")
OBSIDIAN_BASE = os.path.expanduser("~/obsidian")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_FALLBACKS = ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
ERROR_LOG_FILE = os.path.join(os.path.dirname(__file__), "transcribe_errors.log")

client = genai.Client(api_key=GEMINI_API_KEY)


def log_error(message):
    with open(ERROR_LOG_FILE, "a") as f:
        f.write(f"{message}\n")

def extract_recorded_datetime(file_path):
    filename = os.path.basename(file_path)
    match = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{2}\.\d{2}\.\d{2})", filename)
    return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H.%M.%S")


def file_flags(file_path):
    result = subprocess.run(
        ["stat", "-f", "%Sf", file_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ensure_local_file(file_path):
    # iCloud placeholders can be "dataless" when launchd fires before hydration.
    if "dataless" in file_flags(file_path):
        subprocess.run(["brctl", "download", file_path], check=False)


def process_audio(file_path, bucket):
    ensure_local_file(file_path)
    prompt = """Reformat the audio into md hyphen points for my notes, preserving phrasing.
Do not output anything other than the bullets."""
    recorded_at = extract_recorded_datetime(file_path)
    date_str = recorded_at.strftime("%Y-%m-%d")
    target_dir = os.path.join(OBSIDIAN_BASE, bucket)
    target_file = os.path.join(target_dir, f"{date_str}.md")
    if os.path.exists(target_file):
        prompt += "\nOutput should be combined with my current notes:\n" + open(target_file).read()
    with open(file_path, 'rb') as f:
        audio_bytes = f.read()
    response = None
    fallback_errors = []
    contents = [
        prompt,
        types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp4"),
    ]
    for model_name in MODEL_FALLBACKS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
            )
            break
        except errors.APIError as err:
            fallback_errors.append(f"{model_name}: {err}")
            sys.stderr.write(f"{err}\n")
            log_error(str(err))

    if response is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details = " | ".join(fallback_errors) if fallback_errors else "no model error captured"
        message = (
            f"[{timestamp}] Failed to process file: {file_path}. "
            f"All model fallbacks failed. Errors: {details}\n"
        )
        sys.stderr.write(message)
        log_error(message.rstrip("\n"))
        return

    with open(target_file, "w") as f:
        f.write(response.text)
    # Move the file to the macOS Trash
    send2trash(file_path)

def main():
    for bucket in ("course", "notes"):
        source_dir = os.path.join(SOURCE_BASE_DIR, bucket)
        for filename in os.listdir(source_dir):
            if filename.startswith("."):
                continue
            file_path = os.path.join(source_dir, filename)
            process_audio(file_path, bucket)

if __name__ == "__main__":
    main()
