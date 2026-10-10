"""Stream a ZIP archive to the client while it is being built.

The archive never exists on the server: entries are read from the job
folder and written into an in-memory sink that is drained after every
chunk. Audio is already compressed, so entries are STORED (no deflate),
which keeps the CPU cost at a plain file copy.
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

CHUNK = 256 * 1024


class _Sink:
    """Write-only, non-seekable file object: zipfile then emits data
    descriptors instead of seeking back to patch local headers."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buf += data
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


def stream_zip(files: list[tuple[Path, str]]) -> Iterator[bytes]:
    """Yield a ZIP of (source path, name inside the archive) pairs."""
    sink = _Sink()
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for src, arcname in files:
            info = zipfile.ZipInfo.from_file(src, arcname)
            info.compress_type = zipfile.ZIP_STORED
            with open(src, "rb") as fh, zf.open(info, mode="w", force_zip64=True) as dest:
                while chunk := fh.read(CHUNK):
                    dest.write(chunk)
                    if data := sink.drain():
                        yield data
            if data := sink.drain():
                yield data
    if data := sink.drain():
        yield data


def stream_file(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            yield chunk


def collect_audio_files(root: Path, extensions: set[str]) -> list[tuple[Path, str]]:
    """Audio files under root as (path, archive name), skipping dotfiles
    (Sunnify's manifest and lock) and anything that isn't a regular file."""
    found: list[tuple[Path, str]] = []
    if not root.is_dir():
        return found
    root_real = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name.startswith(".") or path.suffix.lower() not in extensions:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root_real):
                continue
            found.append((path, path.relative_to(root).as_posix()))
    return found
