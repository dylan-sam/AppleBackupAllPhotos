"""Source traversal tests that do not require a connected phone."""

import unittest
import tempfile
import struct
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

from iphone_copy.core import MediaItem
from iphone_copy.sources import BackupSource, UsbSource


class FakeAfc:
    """Model a small nested DCIM tree without a connected device."""

    async def listdir(self, path):
        """Return the synthetic children for one device directory."""
        return {
            "/DCIM": ["2026", "README.txt"],
            "/DCIM/2026": ["November", "IMG_1.HEIC"],
            "/DCIM/2026/November": ["MOV_1.MOV", "ignore.db"],
        }[path]

    async def stat(self, path):
        """Describe synthetic directories and regular media files."""
        if path in {"/DCIM/2026", "/DCIM/2026/November"}:
            return {"st_ifmt": "S_IFDIR"}
        return {
            "st_ifmt": "S_IFREG",
            "st_size": 12,
            "st_mtime": datetime(2026, 1, 1),
        }


class UsbScanTests(unittest.IsolatedAsyncioTestCase):
    """Verify USB discovery behavior with a fake AFC service."""

    async def test_nested_dcim_media_is_found(self):
        """Find media at multiple depths and ignore unrelated files."""
        source = UsbSource()
        source.afc = FakeAfc()
        errors = []
        files = await source.scan(
            lambda: None,
            lambda path, exc: errors.append(path),
            lambda _: None,
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            {item.relative_path for item in files},
            {
                Path("DCIM/2026/IMG_1.HEIC"),
                Path("DCIM/2026/November/MOV_1.MOV"),
            },
        )


class OlderBackupTests(unittest.IsolatedAsyncioTestCase):
    """Check backup compatibility and streamed decryption behavior."""

    async def test_older_manifest_is_not_deleted(self):
        """Keep an original MBDB manifest after closing its backup source."""
        with tempfile.TemporaryDirectory() as folder:
            backup_path = Path(folder)
            manifest = backup_path / "Manifest.mbdb"
            manifest.write_bytes(b"old-format-manifest")
            entry = SimpleNamespace(
                file_id="example-id",
                domain="CameraRollDomain",
                relative_path="Media/DCIM/100APPLE/IMG_1.JPG",
                size=12,
                last_modified=datetime(2026, 1, 1),
                is_file=lambda: True,
            )
            backup = SimpleNamespace(
                _manifest_db=SimpleNamespace(path=manifest, NAME="Manifest.mbdb"),
                iter_files=lambda: iter([entry]),
            )
            source = BackupSource(backup_path)
            source.backup = backup
            items = await source.scan(lambda: None, lambda *_: None, lambda _: None)
            self.assertEqual(len(items), 1)
            await source.__aexit__(None, None, None)
            self.assertTrue(manifest.is_file())

    async def test_encrypted_media_is_streamed_in_chunks(self):
        """Recover a synthetic encrypted media file without loading it all at once."""
        content = b"sample-media" * 100000
        wrapping_key = bytes(range(32))
        file_key = bytes(range(32, 64))
        padder = padding.PKCS7(128).padder()
        padded = padder.update(content) + padder.finalize()
        encryptor = Cipher(algorithms.AES(file_key), modes.CBC(bytes(16))).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        with tempfile.TemporaryDirectory() as folder:
            payload = Path(folder) / "payload"
            payload.write_bytes(encrypted)
            entry = SimpleNamespace(
                real_path=payload,
                encryption_key=struct.pack("<I", 1) + aes_key_wrap(wrapping_key, file_key),
            )
            source = BackupSource(Path(folder))
            source.backup = SimpleNamespace(
                is_encrypted=True,
                keybag=SimpleNamespace(get_key=lambda _: wrapping_key),
            )
            source.entries["sample"] = entry
            item = MediaItem("sample", Path("Media/sample.MOV"), len(content), None)
            decoded = b"".join([block async for block in source.chunks(item)])
            self.assertEqual(decoded, content)


if __name__ == "__main__":
    unittest.main()
