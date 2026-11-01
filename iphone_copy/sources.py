"""Adapters for Apple USB storage and local Apple Devices backups."""

from __future__ import annotations

import os
import plistlib
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from pathlib import Path

from .core import Cancelled, MediaItem, is_media, safe_name


CHUNK_SIZE = 1024 * 1024


def app_data_dir() -> Path:
    """Return the per-user directory used for Apple pairing records."""
    override = os.environ.get("IPHONE_COPY_DATA_DIR")
    base = Path(override or os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    directory = base / "iPhone Photo Copier"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def find_backups() -> list[Path]:
    """Find local backups in the usual Apple Devices and iTunes locations."""
    roots = [Path.home() / "Apple/MobileSync/Backup"]
    if appdata := os.environ.get("APPDATA"):
        roots.append(Path(appdata) / "Apple Computer/MobileSync/Backup")
    backups: list[Path] = []
    for root in roots:
        if root.is_dir():
            backups.extend(
                path for path in root.iterdir()
                if path.is_dir() and (path / "Manifest.plist").is_file()
            )
    return backups


def backup_media_path(relative_path: str) -> Path | None:
    """Accept a safe media path from the backup's Media directory."""
    parts = relative_path.split("/")
    if not parts or parts[0] != "Media" or not is_media(parts[-1]):
        return None
    try:
        return Path(*(safe_name(part) for part in parts))
    except ValueError:
        return None


class UsbSource:
    """Read DCIM through Apple's AFC service, independently of File Explorer."""

    max_attempts = 3

    def __init__(self, device_serial: str | None = None) -> None:
        """Optionally target one connected Apple device by its runtime identifier."""
        self.device_serial = device_serial
        self._stack = AsyncExitStack()
        self.afc = None
        self.device_name = "Apple device"

    async def __aenter__(self) -> UsbSource:
        """Open Apple's USB lockdown and AFC services for the selected device."""
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService
        from pymobiledevice3.usbmux import list_devices

        devices = [device for device in await list_devices() if device.connection_type == "USB"]
        if self.device_serial:
            devices = [device for device in devices if device.serial == self.device_serial]
        if not devices:
            raise RuntimeError(
                "No USB iPhone found by Apple Mobile Device Service. Unlock it and tap Trust."
            )
        if len(devices) > 1:
            raise RuntimeError("More than one USB device found. Disconnect the others and retry.")

        cache = app_data_dir() / "pairing"
        cache.mkdir(parents=True, exist_ok=True)
        try:
            lockdown = await create_using_usbmux(
                serial=devices[0].serial, pairing_records_cache_folder=cache,
            )
            lockdown = await self._stack.enter_async_context(lockdown)
            self.device_name = f"Apple device ({lockdown.product_type})"
            self.afc = await self._stack.enter_async_context(AfcService(lockdown))
        except Exception:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Close the AFC and lockdown connections even after a copy error."""
        await self._stack.__aexit__(exc_type, exc, tb)

    async def scan(
        self,
        check_cancel: Callable[[], None],
        report_error: Callable[[Path, Exception], None],
        log: Callable[[str], None],
    ) -> list[MediaItem]:
        """Walk DCIM recursively and collect regular media files with their sizes."""
        log(f"Connected: {self.device_name}")
        items: list[MediaItem] = []
        pending = [("/DCIM", Path("DCIM"), 0)]
        while pending:
            check_cancel()
            parent, relative_parent, depth = pending.pop()
            try:
                names = await self.afc.listdir(parent)
            except Cancelled:
                raise
            except Exception as exc:
                if parent == "/DCIM":
                    raise
                report_error(relative_parent, exc)
                continue
            for name in names:
                check_cancel()
                if name in {".", ".."}:
                    continue
                relative = relative_parent / name
                try:
                    safe_name(name)
                    source_id = parent + "/" + name
                    stat = await self.afc.stat(source_id)
                    kind = stat.get("st_ifmt")
                    if kind == "S_IFDIR":
                        if depth >= 20:
                            raise ValueError(f"Directory nesting is too deep: {relative}")
                        pending.append((source_id, relative, depth + 1))
                    elif kind == "S_IFREG" and is_media(name):
                        items.append(MediaItem(
                            source_id, relative, stat["st_size"],
                            stat["st_mtime"].timestamp(),
                        ))
                except Cancelled:
                    raise
                except Exception as exc:
                    report_error(relative, exc)
        return items

    async def chunks(self, item: MediaItem) -> AsyncIterator[bytes]:
        """Read one iPhone file in bounded blocks and close its device handle."""
        handle = await self.afc.fopen(item.source_id, "r")
        try:
            remaining = item.size
            while remaining:
                block = await self.afc.fread(handle, min(CHUNK_SIZE, remaining))
                if not block:
                    raise IOError(f"Device returned end of file early: {item.relative_path}")
                remaining -= len(block)
                yield block
        finally:
            await self.afc.fclose(handle)


class BackupSource:
    """Read CameraRollDomain from an existing backup without modifying it."""

    max_attempts = 1

    def __init__(self, path: Path, password: str = "") -> None:
        """Remember the backup location and optional decryption password."""
        self.path = path
        self.password = password
        self.backup = None
        self.entries = {}

    async def __aenter__(self) -> BackupSource:
        """Validate the manifest and open the backup through pyiosbackup."""
        from pyiosbackup import Backup

        manifest_path = self.path / "Manifest.plist"
        if not manifest_path.is_file():
            raise ValueError("Select a backup folder containing Manifest.plist.")
        manifest = plistlib.loads(manifest_path.read_bytes())
        if manifest.get("IsEncrypted") and not self.password:
            raise ValueError("This backup is encrypted. Enter its backup password.")
        self.backup = Backup.from_path(self.path, self.password)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Close the manifest and remove only temporary decrypted copies."""
        manifest_db = self.backup._manifest_db
        connection = getattr(manifest_db, "_conn", None)
        if connection is not None:
            connection.close()
        # Only remove a temporary manifest. Older backups keep Manifest.mbdb in place.
        if manifest_db.path != self.path / manifest_db.NAME:
            Path(manifest_db.path).unlink(missing_ok=True)
        self.entries.clear()

    async def scan(
        self,
        check_cancel: Callable[[], None],
        report_error: Callable[[Path, Exception], None],
        log: Callable[[str], None],
    ) -> list[MediaItem]:
        """Find camera media in modern SQLite or older MBDB manifests."""
        manifest_db = self.backup._manifest_db
        connection = getattr(manifest_db, "_conn", None)
        if connection is not None:
            rows = connection.execute(
                "SELECT fileID, relativePath FROM Files WHERE domain = ? AND flags = 1",
                ("CameraRollDomain",),
            ).fetchall()
            candidates = ((row["fileID"], row["relativePath"], None) for row in rows)
        else:
            candidates = (
                (entry.file_id, entry.relative_path, entry)
                for entry in self.backup.iter_files()
                if entry.domain == "CameraRollDomain"
            )
        items: list[MediaItem] = []
        for file_id, path, loaded_entry in candidates:
            check_cancel()
            relative = backup_media_path(path)
            if relative is None:
                continue
            try:
                entry = loaded_entry or self.backup.get_entry_by_id(file_id)
                if entry.is_file():
                    self.entries[entry.file_id] = entry
                    items.append(MediaItem(
                        entry.file_id, relative, entry.size,
                        entry.last_modified.timestamp(),
                    ))
            except Exception as exc:
                report_error(relative, exc)
        if not items:
            log(
                "The backup contains no camera media. "
                "iCloud Photos may keep it outside the backup."
            )
        return items

    async def chunks(self, item: MediaItem) -> AsyncIterator[bytes]:
        """Yield backup file bytes, decrypting them as they are read if needed."""
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap
        from pyiosbackup.keybag import encryption_key_struct

        entry = self.entries[item.source_id]
        with entry.real_path.open("rb") as source:
            if not self.backup.is_encrypted:
                while block := source.read(CHUNK_SIZE):
                    yield block
                return

            key_info = encryption_key_struct.parse(entry.encryption_key)
            key = aes_key_unwrap(
                self.backup.keybag.get_key(key_info.class_), key_info.key,
            )
            decryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
            unpadder = padding.PKCS7(128).unpadder()
            while block := source.read(CHUNK_SIZE):
                data = unpadder.update(decryptor.update(block))
                if data:
                    yield data
            tail = unpadder.update(decryptor.finalize()) + unpadder.finalize()
            if tail:
                yield tail
