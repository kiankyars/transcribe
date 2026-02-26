# transcribe

Transcribes `.m4a` voice memos from `notes` and `course` watch folders into daily Obsidian markdown files.

## Behavior

- For each audio file, it generates markdown hyphen bullets.
- It does **not** pass existing Obsidian note content into the prompt.
- It appends output by date (`YYYY-MM-DD.md`):
  - target file empty: write without a leading blank line
  - target file non-empty: insert one blank line, then append
- Processed audio files are moved to Trash.

## Setup

1. Create `.env` from `.env.example` and fill all values.
2. Create and activate a virtual environment.
3. Install dependencies:
   - `pip install -e .`

Required env vars:

- `GEMINI_API_KEY`
- `NOTES_VOICE_MEMOS_DIR`
- `COURSE_VOICE_MEMOS_DIR`
- `OBSIDIAN_NOTES_DIR`
- `OBSIDIAN_COURSE_DIR`
- `TRANSCRIBE_ERROR_LOG`

## Run manually

- `./run_transcribe.sh`

## Install launchd watcher

- `./scripts/install_launchd.sh`

This installs `~/Library/LaunchAgents/com.kian.transcribe.plist` from the repo template and watches:

- `NOTES_VOICE_MEMOS_DIR`
- `COURSE_VOICE_MEMOS_DIR`
