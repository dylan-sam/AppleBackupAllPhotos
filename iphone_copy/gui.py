"""Qt widgets and worker-thread wiring for the media copier."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGridLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .core import Copier, CopyResult, Counts, LogEvent, ProgressEvent
from .sources import BackupSource, UsbSource, find_backups


USB_SOURCE = "usb"
BACKUP_SOURCE = "backup"


@dataclass(frozen=True)
class CopySettings:
    """Hold the options for one copy job without exposing its password in repr."""

    source: str
    destination: Path
    backup_path: Path
    password: str = field(repr=False)


class CopyWorker(QThread):
    """Run one copy job and send immutable events to the GUI thread."""

    event = Signal(object)
    copy_done = Signal(object)

    def __init__(self, settings: CopySettings, parent=None) -> None:
        """Create a cancellable Qt thread for the requested copy job."""
        super().__init__(parent)
        self.settings = settings
        self.cancel = threading.Event()

    def run(self) -> None:
        """Run the copier outside the GUI thread and emit its final result."""
        try:
            source = (
                UsbSource() if self.settings.source == USB_SOURCE
                else BackupSource(self.settings.backup_path, self.settings.password)
            )
            copier = Copier(self.settings.destination, self.cancel, self.event.emit)
            result = asyncio.run(copier.run(source))
        except Exception as exc:
            result = CopyResult("Could not continue", Counts(), (str(exc),))
            self.event.emit(LogEvent(f"{type(exc).__name__}: {exc}"))
        self.copy_done.emit(result)


class App(QWidget):
    """Present source and destination controls while a worker copies files."""

    def __init__(self) -> None:
        """Build the window and connect its controls to copy actions."""
        super().__init__()
        self.setWindowTitle("iPhone Photo Copier")
        self.resize(760, 530)
        self.setMinimumSize(630, 420)
        self.worker: CopyWorker | None = None
        self.close_when_done = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("Copy iPhone photos and videos")
        title.setStyleSheet("font-size: 22px; font-weight: 600")
        layout.addWidget(title)

        fields = QGridLayout()
        fields.setColumnStretch(1, 1)
        layout.addLayout(fields)

        fields.addWidget(QLabel("Source"), 0, 0)
        self.source = QComboBox()
        self.source.addItem("Connected iPhone (USB)", USB_SOURCE)
        self.source.addItem("Apple Devices backup", BACKUP_SOURCE)
        self.source.currentIndexChanged.connect(self.update_source)
        fields.addWidget(self.source, 0, 1, 1, 2)

        fields.addWidget(QLabel("Backup folder"), 1, 0)
        backups = find_backups()
        latest = max(backups, key=lambda path: path.stat().st_mtime) if backups else None
        self.backup_entry = QLineEdit(str(latest) if latest else "")
        fields.addWidget(self.backup_entry, 1, 1)
        self.backup_button = QPushButton("Browse…")
        self.backup_button.clicked.connect(self.choose_backup)
        fields.addWidget(self.backup_button, 1, 2)

        fields.addWidget(QLabel("Backup password"), 2, 0)
        self.password_entry = QLineEdit()
        self.password_entry.setEchoMode(QLineEdit.EchoMode.Password)
        fields.addWidget(self.password_entry, 2, 1)
        fields.addWidget(QLabel("Only if encrypted"), 2, 2)

        fields.addWidget(QLabel("Copy to"), 3, 0)
        self.destination = QLineEdit(str(Path.home() / "Pictures" / "iPhone Import"))
        fields.addWidget(self.destination, 3, 1)
        browse_destination = QPushButton("Browse…")
        browse_destination.clicked.connect(self.choose_destination)
        fields.addWidget(browse_destination, 3, 2)

        buttons = QGridLayout()
        buttons.setColumnStretch(2, 1)
        layout.addLayout(buttons)
        self.start_button = QPushButton("Start copy")
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button, 0, 0)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button, 0, 1)

        self.status = QLabel("Ready. Unlock your iPhone and tap Trust if prompted.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        layout.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.update_source()

    def update_source(self) -> None:
        """Enable backup fields only when the backup source is selected."""
        enabled = self.source.currentData() == BACKUP_SOURCE
        for widget in (self.backup_entry, self.backup_button, self.password_entry):
            widget.setEnabled(enabled)

    def choose_backup(self) -> None:
        """Let the user select a local Apple backup directory."""
        selected = QFileDialog.getExistingDirectory(self, "Choose Apple Devices backup folder")
        if selected:
            self.backup_entry.setText(selected)

    def choose_destination(self) -> None:
        """Let the user choose where completed media files will be stored."""
        selected = QFileDialog.getExistingDirectory(self, "Choose destination folder")
        if selected:
            self.destination.setText(selected)

    def start(self) -> None:
        """Validate inputs and start a background copy without blocking Qt."""
        if self.worker is not None:
            return
        if not self.destination.text().strip():
            QMessageBox.critical(self, "Destination required", "Choose a destination folder.")
            return
        settings = CopySettings(
            self.source.currentData(),
            Path(self.destination.text()).expanduser(),
            Path(self.backup_entry.text()),
            self.password_entry.text(),
        )
        self.worker = CopyWorker(settings, self)
        self.worker.event.connect(self.show_event)
        self.worker.copy_done.connect(self.show_result)
        self.worker.finished.connect(self.worker_finished)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.bar.setValue(0)
        self.status.setText("Connecting and scanning…")
        self.worker.start()

    def stop(self) -> None:
        """Ask the active worker to stop at its next cancellation check."""
        if self.worker is not None:
            self.worker.cancel.set()
            self.status.setText("Stopping after the current device operation…")

    def show_event(self, event: LogEvent | ProgressEvent) -> None:
        """Display log messages or update file and overall progress."""
        if isinstance(event, LogEvent):
            self.log.appendPlainText(event.message)
            return
        counts = event.counts
        fraction = event.done / event.size if event.size else 1
        active = fraction if event.done < event.size else 0
        percent = min(100, 100 * (counts.completed + active) / max(1, counts.total))
        self.bar.setValue(int(percent))
        self.status.setText(
            f"{counts.completed}/{counts.total} completed · {event.filename} "
            f"· {event.done:,}/{event.size:,} bytes"
        )

    def show_result(self, result: CopyResult) -> None:
        """Show the final copy counts and any file-level errors."""
        counts = result.counts
        summary = (
            f"{result.status}: {counts.copied} copied, "
            f"{counts.skipped} already present, {counts.failed} failed."
        )
        self.status.setText(summary)
        self.log.appendPlainText(summary)
        if result.errors and not self.close_when_done:
            QMessageBox.warning(
                self, "Copy finished with errors", summary + "\nSee the log in the window."
            )

    def worker_finished(self) -> None:
        """Release the finished worker and clear the password field."""
        worker = self.worker
        self.worker = None
        self.password_entry.clear()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        worker.deleteLater()
        if self.close_when_done:
            self.close()

    def closeEvent(self, event) -> None:
        """Delay closing until an active copy has stopped safely."""
        if self.worker is not None:
            self.close_when_done = True
            self.stop()
            event.ignore()
            return
        super().closeEvent(event)


def main() -> int:
    """Create the Qt application and run its event loop."""
    application = QApplication([])
    window = App()
    window.show()
    return application.exec()
