from __future__ import annotations

import stat
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai import errors, types

from src.simple_endpoints import SimpleEndpoint
from src.transcribe import (
    GEMINI_REQUEST_TIMEOUT_MS,
    atomic_write_if_unchanged,
    format_transcript_as_bullets,
    process_audio,
    read_note_snapshot,
    write_capture_to_note,
)
from src.transcribe import (
    main as run_simple_inbox,
)
from src.transcribe_audio import (
    GEMINI_REQUEST_TIMEOUT_MS as AUDIO_REQUEST_TIMEOUT_MS,
)
from src.transcribe_audio import (
    audio_mime_type,
    build_prompt,
    transcribe_audio,
    transcribe_with_retries,
    wait_for_active_file,
)


class GeminiAudioTranscriptionTests(unittest.TestCase):
    def test_m4a_uses_supported_audio_mime_type(self) -> None:
        self.assertEqual(audio_mime_type(Path("recording.m4a")), "audio/mp4")

    def test_context_is_only_a_spelling_hint(self) -> None:
        prompt = build_prompt("Likely spelling: Austin")

        self.assertIn("spelling hint only", prompt)
        self.assertIn("Austin", prompt)

    def test_waits_for_uploaded_file_to_become_active(self) -> None:
        processing = SimpleNamespace(
            name="files/example",
            state=types.FileState.PROCESSING,
        )
        active = SimpleNamespace(
            name="files/example",
            state=types.FileState.ACTIVE,
        )
        client = Mock()
        client.files.get.return_value = active

        with patch("src.transcribe_audio.time.sleep"):
            result = wait_for_active_file(client, processing)

        self.assertIs(result, active)
        get_call = client.files.get.call_args
        self.assertEqual(get_call.kwargs["name"], "files/example")
        self.assertEqual(
            get_call.kwargs["config"].http_options.timeout,
            AUDIO_REQUEST_TIMEOUT_MS,
        )

    def test_uploads_transcribes_and_deletes_audio(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"audio")
            uploaded = SimpleNamespace(
                name="files/example",
                uri="https://example.test/files/example",
                mime_type="audio/mp4",
                state=types.FileState.ACTIVE,
            )
            client = Mock()
            client.files.upload.return_value = uploaded
            client.models.generate_content.return_value = SimpleNamespace(
                text="Speaker 1: Hello"
            )

            with patch(
                "src.transcribe_audio.configured_env",
                return_value="gemini-test-model",
            ):
                transcript = transcribe_audio(client, audio_file)

        self.assertEqual(transcript, "Speaker 1: Hello")
        upload_config = client.files.upload.call_args.kwargs["config"]
        self.assertEqual(upload_config.mime_type, "audio/mp4")
        self.assertEqual(
            upload_config.http_options.timeout,
            AUDIO_REQUEST_TIMEOUT_MS,
        )
        call = client.models.generate_content.call_args
        self.assertEqual(call.kwargs["model"], "gemini-test-model")
        self.assertIs(call.kwargs["contents"][0], uploaded)
        self.assertEqual(
            call.kwargs["config"].http_options.timeout,
            AUDIO_REQUEST_TIMEOUT_MS,
        )
        delete_call = client.files.delete.call_args
        self.assertEqual(delete_call.kwargs["name"], "files/example")
        self.assertEqual(
            delete_call.kwargs["config"].http_options.timeout,
            AUDIO_REQUEST_TIMEOUT_MS,
        )

    def test_transcription_retries_only_the_pinned_helper_path(self) -> None:
        client = Mock()
        unavailable = errors.ServerError(
            503,
            {"error": {"code": 503, "message": "unavailable", "status": "UNAVAILABLE"}},
        )
        with (
            patch(
                "src.transcribe_audio.transcribe_once",
                side_effect=[unavailable, unavailable, "Speaker 1: Hello"],
            ) as transcribe_once,
            patch(
                "src.transcribe_audio.configured_env",
                return_value="gemini-test-model",
            ),
            patch("src.transcribe_audio.time.sleep"),
        ):
            transcript = transcribe_with_retries(
                client, Path("recording.m4a"), context=None
            )

        self.assertEqual(transcript, "Speaker 1: Hello")
        self.assertEqual(transcribe_once.call_count, 3)
        self.assertTrue(
            all(
                call.kwargs["model_name"] == "gemini-test-model"
                for call in transcribe_once.call_args_list
            )
        )

    def test_transport_timeout_retries_the_same_model(self) -> None:
        client = Mock()
        with (
            patch(
                "src.transcribe_audio.transcribe_once",
                side_effect=[TimeoutError("request timed out"), "Speaker 1: Hello"],
            ) as transcribe_once,
            patch(
                "src.transcribe_audio.configured_env",
                return_value="gemini-test-model",
            ),
            patch("src.transcribe_audio.time.sleep"),
        ):
            transcript = transcribe_with_retries(
                client, Path("recording.m4a"), context=None
            )

        self.assertEqual(transcript, "Speaker 1: Hello")
        self.assertEqual(transcribe_once.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["model_name"] == "gemini-test-model"
                for call in transcribe_once.call_args_list
            )
        )

    def test_invalid_m4a_uses_lossless_compatibility_remux(self) -> None:
        invalid = errors.ClientError(
            400,
            {
                "error": {
                    "code": 400,
                    "message": "Request contains an invalid argument.",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"original")
            with (
                patch(
                    "src.transcribe_audio.transcribe_with_retries",
                    side_effect=[invalid, "Speaker 1: Hello"],
                ) as transcribe_with_retries_mock,
                patch("src.transcribe_audio.remux_m4a") as remux,
                patch(
                    "src.transcribe_audio.configured_env",
                    return_value="gemini-test-model",
                ),
            ):
                transcript = transcribe_audio(Mock(), audio_file)
            self.assertEqual(audio_file.read_bytes(), b"original")

        self.assertEqual(transcript, "Speaker 1: Hello")
        remux.assert_called_once()
        remuxed_path = transcribe_with_retries_mock.call_args_list[1].args[1]
        self.assertEqual(remuxed_path.suffix, ".m4a")
        self.assertNotEqual(remuxed_path, audio_file)


class SimpleInboxTranscriptionTests(unittest.TestCase):
    def test_simple_inbox_uses_configured_gemini_model(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"audio")
            client = Mock()
            client.models.generate_content.return_value = SimpleNamespace(
                text="- Hello"
            )

            with patch(
                "src.transcribe.configured_env",
                return_value="gemini-test-model",
            ):
                transcript = format_transcript_as_bullets(
                    client,
                    audio_file,
                    Path(temp_dir) / "errors.log",
                )

        self.assertEqual(transcript, "- Hello")
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemini-test-model",
        )
        request_config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(
            request_config.http_options.timeout,
            GEMINI_REQUEST_TIMEOUT_MS,
        )

    def test_simple_inbox_logs_timeout_and_retries_same_model(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"audio")
            client = Mock()
            client.models.generate_content.side_effect = [
                TimeoutError("request timed out"),
                SimpleNamespace(text="- Hello"),
            ]

            with (
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.time.sleep"),
                patch("src.transcribe.log_error") as log_error,
            ):
                transcript = format_transcript_as_bullets(
                    client,
                    audio_file,
                    Path(temp_dir) / "errors.log",
                )

        self.assertEqual(transcript, "- Hello")
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["model"] == "gemini-test-model"
                for call in client.models.generate_content.call_args_list
            )
        )
        self.assertTrue(
            all(
                call.kwargs["config"].http_options.timeout == GEMINI_REQUEST_TIMEOUT_MS
                for call in client.models.generate_content.call_args_list
            )
        )
        self.assertIn("attempt 1: request timed out", log_error.call_args.args[1])

    def test_simple_inbox_retries_only_the_pinned_model(self) -> None:
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"audio")
            client = Mock()
            client.models.generate_content.side_effect = RuntimeError("unavailable")

            with (
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.time.sleep"),
                patch("src.transcribe.log_error") as log_error,
            ):
                transcript = format_transcript_as_bullets(
                    client,
                    audio_file,
                    Path(temp_dir) / "errors.log",
                )

        self.assertIsNone(transcript)
        self.assertEqual(client.models.generate_content.call_count, 3)
        self.assertTrue(
            all(
                call.kwargs["model"] == "gemini-test-model"
                for call in client.models.generate_content.call_args_list
            )
        )
        self.assertEqual(log_error.call_count, 4)
        final_log = log_error.call_args_list[-1].args[1]
        self.assertIn("attempt 1: unavailable", final_log)
        self.assertIn("attempt 2: unavailable", final_log)
        self.assertIn("attempt 3: unavailable", final_log)

    def test_quota_exhausted_does_not_retry_or_drain_the_queue(self) -> None:
        quota = errors.APIError(
            429,
            {"error": {"message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}},
        )
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "notes"
            source_dir.mkdir()
            first = source_dir / "2026-08-29 08.49.57.m4a"
            second = source_dir / "2026-08-29 08.50.09.m4a"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            daily_dir = temp_path / "daily"
            error_log = temp_path / "errors.log"
            client = Mock()
            client.models.generate_content.side_effect = quota
            source = SimpleEndpoint("notes", None, source_dir)

            with (
                patch(
                    "src.transcribe.load_config",
                    return_value=(client, [source], daily_dir, error_log),
                ),
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.optional_env", return_value=""),
                patch("src.transcribe.ensure_local_file", return_value=True),
                patch("src.transcribe.time.sleep") as sleep,
                patch("src.transcribe.trash_file") as trash_file,
            ):
                status = run_simple_inbox()
            first_remains = first.exists()
            second_remains = second.exists()
            note_exists = (daily_dir / "2026-08-29.md").exists()

        self.assertEqual(status, 1)
        self.assertEqual(client.models.generate_content.call_count, 1)
        sleep.assert_not_called()
        trash_file.assert_not_called()
        self.assertFalse(note_exists)
        self.assertTrue(first_remains)
        self.assertTrue(second_remains)

    def test_quota_falls_back_to_second_api_key(self) -> None:
        quota = errors.APIError(
            429,
            {"error": {"message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}},
        )
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"audio")
            primary = Mock()
            primary.models.generate_content.side_effect = quota
            fallback = Mock()
            fallback.models.generate_content.return_value = SimpleNamespace(
                text="- Hello"
            )

            with (
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.optional_env", return_value="fallback-key"),
                patch("src.transcribe.genai.Client", return_value=fallback) as client_cls,
            ):
                transcript = format_transcript_as_bullets(
                    primary,
                    audio_file,
                    Path(temp_dir) / "errors.log",
                )

        self.assertEqual(transcript, "- Hello")
        client_cls.assert_called_once_with(api_key="fallback-key")
        self.assertEqual(primary.models.generate_content.call_count, 1)
        self.assertEqual(fallback.models.generate_content.call_count, 1)

    def test_capacity_errors_fall_back_to_second_api_key(self) -> None:
        for code, status in ((503, "UNAVAILABLE"), (504, "DEADLINE_EXCEEDED")):
            with self.subTest(code=code), TemporaryDirectory() as temp_dir:
                audio_file = Path(temp_dir) / "memo.m4a"
                audio_file.write_bytes(b"audio")
                primary = Mock()
                primary.models.generate_content.side_effect = errors.ServerError(
                    code,
                    {"error": {"code": code, "message": status, "status": status}},
                )
                fallback = Mock()
                fallback.models.generate_content.return_value = SimpleNamespace(
                    text="- Hello"
                )

                with (
                    patch(
                        "src.transcribe.configured_env",
                        return_value="gemini-test-model",
                    ),
                    patch("src.transcribe.optional_env", return_value="fallback-key"),
                    patch(
                        "src.transcribe.genai.Client",
                        return_value=fallback,
                    ) as client_cls,
                ):
                    transcript = format_transcript_as_bullets(
                        primary,
                        audio_file,
                        Path(temp_dir) / "errors.log",
                    )

                self.assertEqual(transcript, "- Hello")
                client_cls.assert_called_once_with(api_key="fallback-key")
                self.assertEqual(primary.models.generate_content.call_count, 1)
                self.assertEqual(fallback.models.generate_content.call_count, 1)

    def test_quota_fallback_gets_its_own_retry_budget(self) -> None:
        quota = errors.APIError(
            429,
            {"error": {"message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}},
        )
        with TemporaryDirectory() as temp_dir:
            audio_file = Path(temp_dir) / "memo.m4a"
            audio_file.write_bytes(b"audio")
            primary = Mock()
            primary.models.generate_content.side_effect = [
                TimeoutError("first timeout"),
                TimeoutError("second timeout"),
                quota,
            ]
            fallback = Mock()
            fallback.models.generate_content.return_value = SimpleNamespace(
                text="- Hello"
            )

            with (
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.optional_env", return_value="fallback-key"),
                patch("src.transcribe.genai.Client", return_value=fallback),
                patch("src.transcribe.time.sleep"),
                patch("src.transcribe.log_error"),
            ):
                transcript = format_transcript_as_bullets(
                    primary,
                    audio_file,
                    Path(temp_dir) / "errors.log",
                )

        self.assertEqual(transcript, "- Hello")
        self.assertEqual(primary.models.generate_content.call_count, 3)
        self.assertEqual(fallback.models.generate_content.call_count, 1)

    def test_timed_out_file_does_not_block_the_rest_of_the_queue(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "course"
            source_dir.mkdir()
            first = source_dir / "2026-08-11 10.00.00.m4a"
            second = source_dir / "2026-08-11 10.01.00.m4a"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            daily_dir = temp_path / "notes"
            error_log = temp_path / "errors.log"
            client = Mock()
            client.models.generate_content.side_effect = [
                TimeoutError("request timed out"),
                TimeoutError("request timed out"),
                TimeoutError("request timed out"),
                SimpleNamespace(text="- Second recording"),
            ]
            source = SimpleEndpoint("course", "## Course à Pied", source_dir)

            with (
                patch(
                    "src.transcribe.load_config",
                    return_value=(client, [source], daily_dir, error_log),
                ),
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.ensure_local_file", return_value=True),
                patch("src.transcribe.time.sleep"),
                patch(
                    "src.transcribe.vault_operation_lock",
                    side_effect=nullcontext,
                ),
                patch("src.transcribe.trash_file") as trash_file,
            ):
                status = run_simple_inbox()

            note = (daily_dir / "2026-08-11.md").read_text()

        self.assertEqual(status, 1)
        self.assertEqual(client.models.generate_content.call_count, 4)
        trash_file.assert_called_once_with(second)
        self.assertIn("## Course à Pied\n\n- Second recording", note)
        self.assertNotIn("first", note.lower())

    def test_note_write_rebuilds_after_a_concurrent_edit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            note = Path(temp_dir) / "note.md"
            note.write_text("Original\n", encoding="utf-8")
            real_atomic_write = atomic_write_if_unchanged
            attempts = 0

            def race_once(target_file, expected, text):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    target_file.write_text(
                        "Original\n\nManual edit\n",
                        encoding="utf-8",
                    )
                    return False
                return real_atomic_write(target_file, expected, text)

            with patch(
                "src.transcribe.atomic_write_if_unchanged",
                side_effect=race_once,
            ):
                write_capture_to_note(
                    note,
                    "- Captured idea",
                    None,
                )

            content = note.read_text(encoding="utf-8")

        self.assertEqual(attempts, 2)
        self.assertIn("Manual edit", content)
        self.assertEqual(content.count("- Captured idea"), 1)
        self.assertNotIn("siri-ingest", content)

    def test_atomic_note_replacement_preserves_permissions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            note = Path(temp_dir) / "note.md"
            note.write_text("Original\n", encoding="utf-8")
            note.chmod(0o600)

            replaced = atomic_write_if_unchanged(
                note,
                b"Original\n",
                "Updated\n",
            )

            mode = stat.S_IMODE(note.stat().st_mode)
            content = note.read_text(encoding="utf-8")

        self.assertTrue(replaced)
        self.assertEqual(mode, 0o600)
        self.assertEqual(content, "Updated\n")

    def test_trash_failure_reprocesses_without_recovery_marker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "course"
            source_dir.mkdir()
            audio_file = source_dir / "2026-08-16 10.00.00.m4a"
            audio_file.write_bytes(b"audio")
            daily_dir = temp_path / "notes"
            daily_dir.mkdir()
            note = daily_dir / "2026-08-16.md"
            note.write_text("Existing note\n", encoding="utf-8")
            error_log = temp_path / "errors.log"
            source = SimpleEndpoint("course", "## Course à Pied", source_dir)
            client = Mock()
            client.models.generate_content.return_value = SimpleNamespace(
                text="- Captured idea"
            )
            with (
                patch("src.transcribe.ensure_local_file", return_value=True),
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch(
                    "src.transcribe.vault_operation_lock",
                    side_effect=nullcontext,
                ),
                patch(
                    "src.transcribe.trash_file",
                    side_effect=[OSError("trash unavailable"), None],
                ) as trash_file,
                patch("src.transcribe.log_error") as log_error,
            ):
                process_audio(client, audio_file, source, daily_dir, error_log)
                process_audio(client, audio_file, source, daily_dir, error_log)

            content = note.read_text(encoding="utf-8")

        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(trash_file.call_count, 2)
        self.assertEqual(content.count("- Captured idea"), 2)
        self.assertNotIn("siri-ingest", content)
        self.assertIn("trash unavailable", log_error.call_args_list[0].args[1])

    def test_unreadable_note_does_not_block_later_queue_item(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "course"
            source_dir.mkdir()
            first = source_dir / "2026-08-15 10.00.00.m4a"
            second = source_dir / "2026-08-16 10.00.00.m4a"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            daily_dir = temp_path / "notes"
            error_log = temp_path / "errors.log"
            client = Mock()
            client.models.generate_content.return_value = SimpleNamespace(
                text="- Second capture"
            )
            source = SimpleEndpoint("course", "## Course à Pied", source_dir)

            def fail_one_note(target_file):
                if target_file.name == "2026-08-15.md":
                    raise OSError("note unavailable")
                return read_note_snapshot(target_file)

            with (
                patch(
                    "src.transcribe.load_config",
                    return_value=(client, [source], daily_dir, error_log),
                ),
                patch(
                    "src.transcribe.configured_env",
                    return_value="gemini-test-model",
                ),
                patch("src.transcribe.ensure_local_file", return_value=True),
                patch(
                    "src.transcribe.read_note_snapshot",
                    side_effect=fail_one_note,
                ),
                patch(
                    "src.transcribe.vault_operation_lock",
                    side_effect=nullcontext,
                ),
                patch("src.transcribe.trash_file") as trash_file,
                patch("src.transcribe.log_error") as log_error,
            ):
                run_simple_inbox()

            second_note = (daily_dir / "2026-08-16.md").read_text(encoding="utf-8")
            first_note_exists = (daily_dir / "2026-08-15.md").exists()

        self.assertFalse(first_note_exists)
        self.assertIn("- Second capture", second_note)
        trash_file.assert_called_once_with(second)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertIn("note unavailable", log_error.call_args_list[0].args[1])


if __name__ == "__main__":
    unittest.main()
