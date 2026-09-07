# Repository Guidelines

## Project Structure & Module Organization
- Simple inbox transcription lives in `src/transcribe.py`.
- `src/simple_endpoints.py` routes running dictations from `course` into `## Course à Pied`.
- Reusable Gemini audio transcription lives in `src/transcribe_audio.py`.
- Operational scripts are in `src/`:
  - `src/siri.sh` runs the simple inbox flow locally.
  - `src/run_simple_ingest.sh` runs the iCloud inbox transcription flow.
  - `src/install_launchd.sh` installs or refreshes `com.siri.simple`.
  - `src/uninstall_launchd.sh` removes it.
- The launchd template is `com.siri.simple.plist.template`.
- Runtime logs are written under `logs/` (for example, `launchd_simple_*.log` and `siri_errors.log`).
- Project metadata and dependencies are defined in `pyproject.toml`.

## Runtime Boundary
- Ingestion may write vault content and its operational logs, state, and temporary
  files. The simple inbox retains its documented source-to-Trash behavior.
- Vault writes must use the shared kernel-held vault operation lock. Simple note
  replacement must remain atomic under concurrent edits. Do not add ingestion IDs,
  hashes, or recovery markers to vault notes.
- Runtime code and wrappers must not mutate or synchronize
  Git repositories. Vault synchronization belongs to separate vault automation.

## Build, Test, and Development Commands
- `uv sync`: install/update the virtual environment and dependencies.
- `./src/siri.sh`: run the transcription flow manually.
- `./src/install_launchd.sh`: install and start `com.siri.simple`.
- `./src/uninstall_launchd.sh`: remove it.
- `uvx ruff check src/transcribe.py`: lint simple inbox transcription code.
- `uvx ruff check src/transcribe_audio.py src/test_transcribe_audio.py`: lint the reusable Gemini transcription helper.
- `uv run siri-transcribe-audio /path/to/recording.m4a`: transcribe one recording with the `GEMINI_MODEL` selected in `~/.env`.
- `uv run python -m unittest discover -s src -p 'test_*.py'`: run focused unit tests.
- `uv run python -m py_compile src/transcribe.py src/transcribe_audio.py src/runtime_support.py src/simple_endpoints.py`: quick syntax validation.

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
  2. `uvx ruff check src/transcribe.py src/transcribe_audio.py src/runtime_support.py src/simple_endpoints.py src/test_transcribe_audio.py`
  3. `uv run python -m py_compile src/transcribe.py src/transcribe_audio.py src/runtime_support.py src/simple_endpoints.py`
  4. Manual smoke run with a sample `.m4a` in a configured inbox.
- Verify expected output file append behavior. Confirm source-to-Trash failures are
  logged and retain the source for a full retry.

## Commit & Pull Request Guidelines
- Follow concise, imperative commit messages (current history style):
- Prefer one logical change per commit.
- PRs should include:
  - What changed and why
  - Any env/config changes (`.env.example`, launchd behavior)
  - Manual verification steps and log evidence when relevant
