"""Behavior tests for copying without a phone or Qt window."""

import tempfile
import threading
import unittest
from pathlib import Path

from iphone_copy.core import Copier, LogEvent, MediaItem, ProgressEvent, safe_name
from iphone_copy.sources import backup_media_path


class FakeSource:
    """Provide predictable media bytes and optional read failures for tests."""

    max_attempts = 3

    def __init__(self, content: bytes, failures: int = 0) -> None:
        """Configure the source payload and number of initial failed reads."""
        self.content = content
        self.failures = failures
        self.attempts = 0
        self.item = MediaItem("sample", Path("DCIM/100APPLE/IMG_1.JPG"), len(content), None)

    async def __aenter__(self):
        """Return this in-memory source without opening external resources."""
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Complete the source context without cleanup work."""
        return None

    async def scan(self, check_cancel, report_error, log):
        """Expose the single synthetic media item to the copier."""
        return [self.item]

    async def chunks(self, item):
        """Yield the payload in two chunks or simulate a temporary failure."""
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OSError("Temporary read failure")
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        if len(self.content) > midpoint:
            yield self.content[midpoint:]


class CopierTests(unittest.IsolatedAsyncioTestCase):
    """Exercise copying, retries, cancellation, and incomplete transfers."""

    async def asyncSetUp(self):
        """Create a clean destination and event collector for each test."""
        self.temp = tempfile.TemporaryDirectory()
        self.destination = Path(self.temp.name)
        self.cancel = threading.Event()
        self.events = []

    async def asyncTearDown(self):
        """Remove files written by the current test."""
        self.temp.cleanup()

    async def test_copy_and_skip_on_next_run(self):
        """Check that a verified file is copied once and skipped on rerun."""
        source = FakeSource(b"photo-data")
        first = await Copier(self.destination, self.cancel, self.events.append).run(source)
        target = self.destination / source.item.relative_path
        self.assertEqual(first.counts.copied, 1)
        self.assertEqual(target.read_bytes(), b"photo-data")
        self.assertFalse(target.with_name(target.name + ".partial").exists())
        self.assertIn(
            "COPIED DCIM\\100APPLE\\IMG_1.JPG (10 bytes)",
            [event.message for event in self.events if isinstance(event, LogEvent)],
        )

        self.events.clear()
        second_source = FakeSource(b"photo-data")
        second = await Copier(self.destination, self.cancel, self.events.append).run(second_source)
        self.assertEqual(second.counts.skipped, 1)
        self.assertEqual(second_source.attempts, 0)
        self.assertIn(
            "SKIPPED DCIM\\100APPLE\\IMG_1.JPG (already present)",
            [event.message for event in self.events if isinstance(event, LogEvent)],
        )

    async def test_retry_after_temporary_read_failure(self):
        """Confirm that a transient source read error triggers one retry."""
        source = FakeSource(b"photo-data", failures=1)
        result = await Copier(self.destination, self.cancel, self.events.append).run(source)
        self.assertEqual(result.counts.copied, 1)
        self.assertEqual(source.attempts, 2)

    async def test_copy_log_is_emitted_after_final_file_exists(self):
        """Report success only after the destination has the complete bytes."""
        source = FakeSource(b"photo-data")
        target = self.destination / source.item.relative_path
        finalized_at_log = []

        def emit(event):
            """Inspect the destination when a successful copy is logged."""
            if isinstance(event, LogEvent) and event.message.startswith("COPIED "):
                finalized_at_log.append(target.read_bytes() == b"photo-data")

        await Copier(self.destination, self.cancel, emit).run(source)
        self.assertEqual(finalized_at_log, [True])

    async def test_stop_keeps_partial_file(self):
        """Ensure cancellation leaves no completed target file behind."""
        source = FakeSource(b"photo-data")

        def emit(event):
            """Request cancellation after the first incomplete progress event."""
            if isinstance(event, ProgressEvent) and event.done < event.size:
                self.cancel.set()

        result = await Copier(self.destination, self.cancel, emit).run(source)
        target = self.destination / source.item.relative_path
        self.assertEqual(result.status, "Stopped")
        self.assertFalse(target.exists())
        self.assertTrue(target.with_name(target.name + ".partial").exists())

    async def test_short_read_is_reported_and_not_finalized(self):
        """Reject a stream that ends before its reported size is reached."""
        source = FakeSource(b"photo-data")
        source.item = MediaItem("sample", source.item.relative_path, 100, None)
        result = await Copier(self.destination, self.cancel, self.events.append).run(source)
        self.assertEqual(result.counts.failed, 1)
        self.assertFalse((self.destination / source.item.relative_path).exists())


class PathTests(unittest.TestCase):
    """Check that output paths stay inside safe media locations."""

    def test_reject_unsafe_names(self):
        """Reject traversal, reserved Windows names, and invalid suffixes."""
        for name in ("..", "CON.txt", "bad/name.jpg", "photo.jpg "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_name(name)

    def test_backup_path_is_limited_to_media(self):
        """Accept media under Media while rejecting traversal and other roots."""
        self.assertEqual(
            backup_media_path("Media/DCIM/100APPLE/IMG_1.JPG"),
            Path("Media/DCIM/100APPLE/IMG_1.JPG"),
        )
        self.assertIsNone(backup_media_path("Media/../secret.JPG"))
        self.assertIsNone(backup_media_path("Library/photo.JPG"))


if __name__ == "__main__":
    unittest.main()
