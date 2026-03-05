# transcribe

Transcribes `.m4a` voice memos from two watch folders into daily Obsidian markdown files.

## Behavior

- For each audio file, it generates markdown hyphen bullets.
- It appends output by date (`YYYY-MM-DD.md`):
  - target file empty: write without a leading blank line
  - target file non-empty: insert one blank line, then append
- Processed audio files are moved to Trash.

## Setup

1. Create `.env` from `.env.example` and fill all values.
2. `uv sync`

Required env vars:

- `GEMINI_API_KEY`
- `VOICE_MEMOS_DIR_0`
- `VOICE_MEMOS_DIR_1`
- `OBSIDIAN_BASE_DIR`
- `OBSIDIAN_SUBDIR_0`
- `OBSIDIAN_SUBDIR_1`

Error logs are written to `logs/transcribe_errors.log` by default.

## Run manually

- `./src/run_transcribe.sh`

## Install launchd watcher

- `./src/install_launchd.sh`

This installs `~/Library/LaunchAgents/com.transcribe.plist` from the repo template and watches:

- `VOICE_MEMOS_DIR_0`
- `VOICE_MEMOS_DIR_1`
