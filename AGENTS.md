# Repository Guidelines

## Project Structure & Module Organization
- Simple inbox transcription lives in `src/transcribe.py`.
- Reusable Gemini audio transcription lives in `src/transcribe_audio.py`.
- Routed Voice Memos discovery and agent invocation live in `src/import_voice_memos.py`.
- Operational scripts are in `src/`:
  - `src/siri.sh` runs both ingestion flows locally.
  - `src/run_simple_ingest.sh` runs the iCloud inbox transcription flow.
  - `src/run_voice_memos_ingest.sh` runs the Voice Memos import flow.
  - `src/install_launchd.sh` installs/refreshes both LaunchAgents (the only command needed; both run on the personal Mac).
  - `src/uninstall_launchd.sh` removes both LaunchAgents.
- Launchd templates are `com.siri.simple.plist.template` and `com.siri.voice-memos.plist.template`.
- Runtime logs are written under `logs/` (e.g. `launchd_simple_*.log`, `launchd_voice_memos_*.log`, `siri_errors.log`).
- Project metadata and dependencies are defined in `pyproject.toml`.

## Runtime Boundary
- Ingestion may write vault content and its operational logs, state, and temporary
  files. The simple inbox retains its documented source-to-Trash behavior.
- Vault writes must use the shared kernel-held vault operation lock. Simple note
  replacement must remain atomic and idempotent across Trash or process failure.
- Routed Voice Memo entry points must share the importer lock so only one process
  can load, apply, and save routing state at a time.
- Runtime code, wrappers, and invoked-agent prompts must not mutate or synchronize
  Git repositories. Vault synchronization belongs to separate vault automation.

## Build, Test, and Development Commands
- `uv sync`: install/update the virtual environment and dependencies.
- `./src/siri.sh`: run the transcription flow manually.
- `./src/install_launchd.sh`: install and start both `com.siri.simple` and `com.siri.voice-memos` LaunchAgents (the only command needed).
- `./src/uninstall_launchd.sh`: remove both.
- `uvx ruff check src/import_voice_memos.py src/test_ingest.py`: lint agentic Voice Memos code.
- `uvx ruff check src/transcribe.py`: lint simple inbox transcription code.
- `uvx ruff check src/transcribe_audio.py src/test_transcribe_audio.py`: lint the reusable Gemini transcription helper.
- `uv run siri-transcribe-audio /path/to/recording.m4a`: transcribe one recording with the `GEMINI_MODEL` selected in `~/.env`.
- `uv run python -m unittest discover -s src -p 'test_*.py'`: run focused unit tests.
- `uv run python -m py_compile src/transcribe.py src/transcribe_audio.py src/import_voice_memos.py`: quick syntax validation.

## Coding Style & Naming Conventions
- Python 3.10+ with 4-space indentation and type hints where practical.
- Prefer `pathlib.Path` for filesystem paths.
- Environment variables drive runtime configuration:
  - `VOICE_MEMOS_DIR_0`, `VOICE_MEMOS_DIR_1`
  - `OBSIDIAN_DAILY_DIR`
  - `GEMINI_API_KEY`
- Shared home configuration: `GEMINI_MODEL` is read from `~/.env`.
- Use `ruff` as the formatting/lint quality gate for Python.

## Testing Guidelines
- Validate changes with:
  1. `uv run python -m unittest discover -s src -p 'test_*.py'`
  2. `uvx ruff check src/import_voice_memos.py src/test_ingest.py`
  3. `uv run python -m py_compile src/transcribe.py src/transcribe_audio.py src/import_voice_memos.py`
  4. Manual smoke run with a sample `.m4a` in a configured voice memo directory.
- Verify expected output file append behavior and confirm no duplicate processing in the relevant `logs/launchd_*_stderr.log`.

## Commit & Pull Request Guidelines
- Follow concise, imperative commit messages (current history style):
- Prefer one logical change per commit.
- PRs should include:
  - What changed and why
  - Any env/config changes (`.env.example`, launchd behavior)
  - Manual verification steps and log evidence when relevant
