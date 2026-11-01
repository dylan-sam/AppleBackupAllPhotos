"""UI-independent copy workflow and transfer events."""

from __future__ import annotations

import asyncio
import os
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MEDIA_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif",
    ".tiff", ".dng", ".bmp", ".webp", ".mov", ".mp4", ".m4v",
    ".avi", ".3gp", ".3g2", ".aae",
})
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def safe_name(name: str) -> str:
    """Reject names that could escape or collide in a Windows destination."""
    if (
        not name or name in {".", ".."} or name != name.rstrip(" .")
        or INVALID_NAME.search(name) or RESERVED_NAME.match(name)
    ):
        raise ValueError(f"Unsafe filename from source: {name!r}")
    return name


def is_media(name: str) -> bool:
    """Return whether a filename has a supported photo, video, or sidecar suffix."""
    return Path(name).suffix.lower() in MEDIA_EXTENSIONS


@dataclass(frozen=True)
class MediaItem:
    """Describe one source file and where it should be copied locally."""

    source_id: str
    relative_path: Path
    size: int
    modified: float | None


@dataclass(frozen=True)
class Counts:
    """Track discovered files and the outcomes of completed copy attempts."""

    total: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def completed(self) -> int:
        """Return the number of files that have a final outcome."""
        return self.copied + self.skipped + self.failed


@dataclass(frozen=True)
class LogEvent:
    """Carry a human-readable status message to the interface."""

    message: str


@dataclass(frozen=True)
class ProgressEvent:
    """Carry a snapshot of file and overall transfer progress."""

    counts: Counts
    filename: str
    done: int
    size: int


@dataclass(frozen=True)
class CopyResult:
    """Summarize a finished, stopped, or failed copy session."""

    status: str
    counts: Counts
    errors: tuple[str, ...]


class Cancelled(Exception):
    """Raised when the user stops the current transfer."""


class MediaSource(Protocol):
    """Small interface for adding another media source without changing the copier."""

    max_attempts: int

    async def __aenter__(self) -> MediaSource:
        """Open the underlying source and return its adapter."""
        ...

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Release device or backup resources when scanning ends."""
        ...

    async def scan(
        self,
        check_cancel: Callable[[], None],
        report_error: Callable[[Path, Exception], None],
        log: Callable[[str], None],
    ) -> list[MediaItem]:
        """Find media files and report individual listing errors."""
        ...

    def chunks(self, item: MediaItem) -> AsyncIterator[bytes]:
        """Yield the original bytes for one media item in bounded chunks."""
        ...


class Copier:
    """Owns skip, retry, partial-file, verification, and progress behavior."""

    def __init__(
        self,
        destination: Path,
        cancel: threading.Event,
        emit: Callable[[LogEvent | ProgressEvent], None],
    ) -> None:
        """Set the destination, cancellation signal, and event callback."""
        self.destination = destination
        self.cancel = cancel
        self.emit = emit
        self.counts = Counts()
        self.errors: list[str] = []

    def check_cancel(self) -> None:
        """Stop promptly when the user has requested cancellation."""
        if self.cancel.is_set():
            raise Cancelled()

    def log(self, message: str) -> None:
        """Send a status message to the interface."""
        self.emit(LogEvent(message))

    def error(self, relative: Path, exc: Exception) -> None:
        """Record one file error while allowing the remaining files to continue."""
        self.counts = Counts(
            self.counts.total,
            self.counts.copied,
            self.counts.skipped,
            self.counts.failed + 1,
        )
        line = f"{relative}: {type(exc).__name__}: {exc}"
        self.errors.append(line)
        self.log("FAILED " + line)

    def progress(self, item: MediaItem, done: int) -> None:
        """Publish a stable snapshot of the current file's progress."""
        self.emit(ProgressEvent(self.counts, str(item.relative_path), done, item.size))

    async def run(self, source: MediaSource) -> CopyResult:
        """Scan the source and copy each item until completion or cancellation."""
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
            self.check_cancel()
            async with source:
                items = await source.scan(self.check_cancel, self.error, self.log)
                self.counts = Counts(
                    len(items) + self.counts.failed,
                    self.counts.copied,
                    self.counts.skipped,
                    self.counts.failed,
                )
                self.log(f"Found {len(items)} photos, videos, and edit sidecars.")
                for item in items:
                    self.check_cancel()
                    try:
                        await self._copy_item(source, item)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        self.error(item.relative_path, exc)
            status = "Complete"
        except Cancelled:
            status = "Stopped"
        except Exception as exc:
            status = "Could not continue"
            line = f"{type(exc).__name__}: {exc}"
            self.errors.append(line)
            self.log(line)
        return CopyResult(status, self.counts, tuple(self.errors))

    async def _copy_item(self, source: MediaSource, item: MediaItem) -> None:
        """Skip or copy one item, retrying reads before publishing the final file."""
        if item.size < 0:
            raise ValueError(f"Invalid source size for {item.relative_path}: {item.size}")
        target = self.destination / item.relative_path
        if target.is_file() and target.stat().st_size == item.size:
            self.counts = Counts(
                self.counts.total, self.counts.copied, self.counts.skipped + 1,
                self.counts.failed,
            )
            self.progress(item, item.size)
            self.log(f"SKIPPED {item.relative_path} (already present)")
            return
        if target.exists() and not target.is_file():
            raise OSError(f"Destination is not a file: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")

        for attempt in range(1, source.max_attempts + 1):
            self.check_cancel()
            try:
                done = await self._write_partial(source, item, partial)
                if done != item.size:
                    raise IOError(
                        f"Size mismatch for {item.relative_path}: expected {item.size}, got {done}"
                    )
                break
            except Cancelled:
                raise
            except Exception:
                if attempt == source.max_attempts:
                    raise
                await asyncio.sleep(attempt)

        os.replace(partial, target)
        if item.modified is not None:
            try:
                os.utime(target, (item.modified, item.modified))
            except OSError as exc:
                self.log(f"Could not set file date for {item.relative_path}: {exc}")
        self.counts = Counts(
            self.counts.total, self.counts.copied + 1,
            self.counts.skipped, self.counts.failed,
        )
        self.progress(item, item.size)
        self.log(f"COPIED {item.relative_path} ({item.size:,} bytes)")

    async def _write_partial(self, source: MediaSource, item: MediaItem, partial: Path) -> int:
        """Stream one item to a temporary file and flush its bytes to disk."""
        done = 0
        with partial.open("wb") as output:
            async with aclosing(source.chunks(item)) as chunks:
                async for block in chunks:
                    self.check_cancel()
                    if not block:
                        raise IOError(f"Empty data block for {item.relative_path}")
                    output.write(block)
                    done += len(block)
                    if done > item.size:
                        raise IOError(f"Source exceeded reported size for {item.relative_path}")
                    self.progress(item, done)
            output.flush()
            os.fsync(output.fileno())
        return done
