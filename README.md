# siri

Transcribes `.m4a` voice memos into Obsidian notes and can also process routed Voice Memos directly from the synced macOS Voice Memos library.

## Behavior

- For each simple-inbox audio file, it generates markdown hyphen bullets.
- Each Gemini request has a two-minute deadline; timed-out attempts are logged and
  use the existing same-model retry path without holding the rest of the queue forever.
- A `429 RESOURCE_EXHAUSTED` response stops the rest of the inbox immediately so
  later files do not burn remaining quota. The LaunchAgent retries hourly in
  addition to WatchPaths, because quota recovery does not create a filesystem event.
- The simple ingest exits nonzero when any capture failed, so launchd's last exit
  code is honest.
- It writes into `notes/YYYY-MM-DD.md`:
  - files from the resolved `notes` inbox append into the root body of the daily note
  - files from the resolved `course` inbox append into `## Course`
  - if the daily note does not exist, it is created
- `notes` is the catch-all simple inbox for podcasts, books, reading thoughts, and other uncategorized captures.
- After a simple-ingest `.m4a` is successfully appended into the daily note, the source file is moved to macOS Trash.
- Simple note updates use the vault operation lock and atomic replacement. They do not add internal IDs, hashes, or recovery markers to the Markdown. If moving a source file to Trash fails after the note is written, a later retry may transcribe and append that capture again.
  - The lock coordinates Siri with the vault synchronizer. External editors do not participate in that advisory lock; the writer rebuilds on changes it detects before replacement, while the synchronizer's settle window provides the broader safety net.
- Siri ingestion never stages, commits, pulls, fetches, merges, rebases, or pushes
  a repository. Repository synchronization belongs to separate vault automation.
- Agentic Voice Memos processing:
  - a process-wide importer lock prevents the watcher, manual runner, and direct entry point from loading and applying the same stale state concurrently; successful records are atomically checkpointed immediately
  - watches the macOS Voice Memos store
  - rescans recordings that arrive or finish syncing during an active importer run before exiting
  - processes recordings renamed exactly `monde` or `réflexion`
  - reads the original recording directly from the Voice Memos library
  - transcribes with `GEMINI_MODEL` from `~/.env`, with no local or alternate-model fallback, before starting Codex
  - gives the sandbox a temporary audio copy, leaving the original Voice Memo outside the agent's writable roots
  - preserves only the configured Codex model, reasoning effort, and service tier while excluding user tools and execution rules
  - starts Codex in the Obsidian vault with workspace-only writes, approval escalation disabled, and shell network access disabled
  - holds the same vault operation lock used by the independent vault synchronizer while Codex edits vault content
  - gives Codex the temporary recording path, date, route, and transcript, then lets it use the vault context and its judgment to make every appropriate content edit
  - the skill identifies `people/{first-name}.md`, `notes/YYYY-MM-DD.md`, and `audio/` as the stable vault environment without prescribing a rigid output format
  - after editing, Codex re-reads the affected content and leaves all repository synchronization outside the Siri workflow
  - leaves source memos in Voice Memos for manual deletion

The agentic skill lives in the vault at `.agents/skills/process-voice-memo/SKILL.md`. The Siri repository only detects routed recordings and hands each one to the vault agent.

## Setup

1. Create `.env` from `.env.example` and fill all values.
2. `uv sync`

Required env vars:

- `GEMINI_API_KEY`
- `VOICE_MEMOS_DIR_0`
- `VOICE_MEMOS_DIR_1`
- `OBSIDIAN_DAILY_DIR`

Set `GEMINI_MODEL` once in `~/.env`; both transcription paths read that shared value.

Error logs are written to `logs/siri_errors.log` by default.
Agentic Voice Memos processed-file state is written to `logs/voice_memos_import_state.json`.

## Run manually

- `./src/siri.sh`
- `./src/run_simple_ingest.sh`
- `./src/run_voice_memos_ingest.sh`
- `uv run siri-transcribe-audio /path/to/recording.m4a -o /tmp/transcript.txt`

## Install launchd watchers

Both LaunchAgents run on the **same machine** (the personal Mac that has access to the iCloud audio sources). The primary (and only needed) command is:

- `./src/install_launchd.sh`
  - Installs/refreshes **both** agents:
    - `com.siri.simple`: watches the resolved `notes`/`course` iCloud folders and runs `src/run_simple_ingest.sh`
    - `com.siri.voice-memos`: watches the Voice Memos library and runs `src/run_voice_memos_ingest.sh`

Uninstall:

- `./src/uninstall_launchd.sh`

These are built from the templates `com.siri.simple.plist.template` and `com.siri.voice-memos.plist.template`.
