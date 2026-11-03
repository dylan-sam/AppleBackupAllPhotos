# iPhone Photo Copier

A free for noncommercial use, local Windows GUI for copying iPhone photos, videos, and `.AAE` edit sidecars to a normal PC folder. It reads the iPhone through Apple's USB service, so it can work even when the phone is missing from Windows File Explorer. 
It can also extract camera media from a local backup made by [Apple Devices](https://apps.microsoft.com/detail/9np83lwlpz9k?hl=fr-FR&gl=CH) or iTunes.

The app copies files. It never deletes media from the phone or changes a backup.

AI Disclosure : This project was built with the help of GPT-6 Sol. 

## Requirements

| Requirement | Details |
| --- | --- |
| PC | 64-bit Windows 10 version 1809 or newer, or 64-bit Windows 11. Windows on ARM and 32-bit Windows have not been tested. [Qt platform support](https://doc.qt.io/qtforpython-6/overviews/qtdoc-windows.html). |
| Python | Python 3.11, 3.12, 3.13, or 3.14 from [python.org](https://www.python.org/downloads/windows/), with `pip` and `venv`. Avoid the Microsoft Store Python distribution for PySide6; [Qt documents a compatibility issue](https://doc.qt.io/qtforpython-6/building_from_source/windows.html). |
| Apple connection | [Apple Devices](https://support.apple.com/guide/devices-windows/welcome/windows) or Apple's desktop iTunes installation, providing Apple Mobile Device Service. A USB data cable, an unlocked phone, and the **Trust This Computer** approval are required. [Apple's connection steps](https://support.apple.com/en-gb/120267). |
| Disk space | Enough free space for the media you copy. The first Python dependency installation also needs an Internet connection and additional disk space. |
| Encrypted backup | The app cannot recover an unknown password from an encrypted Apple Devices backup. |

Dependencies are declared once in [`pyproject.toml`](pyproject.toml). `requirements.txt` installs this project and its dependencies. Current direct dependencies are `pymobiledevice3` 11.49 through 11.x, `pyiosbackup` 0.2.4, and `PySide6-Essentials` 6.10 through 6.12. The Qt range stops before 6.13 to retain the documented Windows 10 support. The backup adapter uses internal `pyiosbackup` manifest APIs, so that dependency is pinned to the version tested here.

## Install and run

1. Download or clone this repository and extract it to a regular folder. 
2. Install Python and Apple Devices or iTunes as described above.
3. Connect the iPhone by USB. Unlock it and tap **Trust** or **Allow** if prompted.
4. Double-click `launch.bat`. The first launch creates `.venv` and installs dependencies. Later launches reuse it.
5. Select a destination folder and click **Start copy**.

PowerShell alternative:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe iphone_photo_copier.py
```

To read the photos from a local backup made by the Apple Devices software, choose **Apple Devices backup** in the Source menu. The app detects common backup locations. You can select another folder containing `Manifest.plist`. Enter the backup password only if the backup is encrypted. Finish any backup currently being made before reading it.

## What gets copied

- USB mode walks the accessible `DCIM` tree recursively and preserves its folders. It includes common photo, video, RAW, and `.AAE` file extensions. The code uses the classic AFC service through `pymobiledevice3`, which its [API documentation](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/python-api.md) describes as available across iOS versions without a developer tunnel. Support still depends on the connected phone and Apple's service; only the available local device was tested directly.
- Backup mode reads camera media in `CameraRollDomain` and preserves paths under `Media`. It supports unencrypted backups and encrypted backups when the correct password is supplied. An encrypted backup was not available for an end-to-end test.
- A destination file with the expected size is skipped on a later run. New files are written as `.partial`, checked against the source size, then renamed. A stopped or failed transfer can leave a `.partial` file, which is replaced on the next run. Size checks catch truncation but do not compare every byte of an existing destination file.
- The on-screen log reports each copied, skipped, or failed file. A `COPIED` entry appears only after the file has been finalized in the destination. The app does not write a separate log file; file paths in the on-screen log may reveal private details if shared.

Photos stored only in iCloud are not available through a USB DCIM copy. [Apple recommends downloading full-resolution originals to the device](https://support.apple.com/en-gb/120267) before importing, and [computer backups exclude data already synced to iCloud Photos](https://support.apple.com/en-gb/108771). Original HEIC/HEVC files are copied without conversion; opening them in Windows may require suitable codecs.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No USB device found | Confirm Apple Devices sees the phone, unlock it, approve Trust, reconnect a data-capable cable, and try another USB port. Then run `.\.venv\Scripts\python.exe -m pymobiledevice3 usbmux list`. If the service is unreachable, install or repair Apple Devices/iTunes and Apple Mobile Device Service. [pymobiledevice3 troubleshooting](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/troubleshooting.md). |
| More than one USB device found | Disconnect other Apple devices, then try again. This avoids copying from the wrong phone. |
| DCIM is empty or incomplete | Keep the phone unlocked. Check whether iCloud Photos has downloaded the originals to the phone. The USB connection cannot fetch cloud-only originals. |
| Backup not found | Browse to the individual backup folder that contains `Manifest.plist` and either `Manifest.db` or the older `Manifest.mbdb`. |
| Backup password error | Use the password that was set when encrypted backups were enabled. The Apple Account password may be different. |
| Some files failed | Leave the destination in place and run the copy again. Complete files of the same size are skipped. Review the on-screen error and ensure there is enough free disk space. |

## Privacy

Copying is local. The app has no telemetry and does not upload photos, backups, passwords, or device identifiers. The backup password remains in the running process and is not saved by this project. Apple pairing data may be stored under the current user's local application data folder for the USB connection. See [`SECURITY.md`](SECURITY.md) before sharing logs or screenshots publicly.

## Development

`iphone_photo_copier.py` is the stable launcher. `iphone_copy/core.py` owns copy, retry, cancel, and verification behavior without Qt. `iphone_copy/sources.py` implements the USB and backup adapters through the `MediaSource` interface. `iphone_copy/gui.py` contains Qt widgets and the worker thread. A different interface can call `Copier.run()` and consume its events without changing source code.

Run tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The GitHub Actions workflow checks installation, tests, and Qt startup on Windows with Python 3.11 to 3.14. Hardware tests require a trusted device and are not run in CI. Source comments and docstrings use ASCII English.

## License

This project's own code is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). Commercial use requires a separate license from the copyright holder. This is a source-available, noncommercial license, not an open-source license. Its Python dependencies have their own licenses.
