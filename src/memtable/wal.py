"""Write-ahead log with CRC32 checksums and replay.

Every write is appended to the WAL before entering the memtable. On crash
recovery the WAL is replayed to reconstruct the in-memory state. Entries
are framed as: [CRC32 (4B)] [key_len (4B)] [val_len (4B)] [type (1B)] [key] [value].
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Generator, Optional


class EntryType(IntEnum):
    """WAL entry types."""
    PUT = 1
    DELETE = 2


_HEADER_FMT = "<IIIB"  # crc32, key_len, val_len, entry_type
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


@dataclass(frozen=True)
class WALEntry:
    """A single WAL record."""
    entry_type: EntryType
    key: str
    value: bytes


class WriteAheadLog:
    """Append-only write-ahead log with CRC32 integrity checks.

    Args:
        path: File path for the WAL segment.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._path, "ab")
        self._entry_count = 0

    @property
    def path(self) -> Path:
        """Path to the WAL file."""
        return self._path

    @property
    def entry_count(self) -> int:
        """Number of entries written in this session."""
        return self._entry_count

    def append_put(self, key: str, value: bytes) -> None:
        """Append a PUT entry.

        Args:
            key: The key being written.
            value: The value bytes.
        """
        self._append(EntryType.PUT, key, value)

    def append_delete(self, key: str) -> None:
        """Append a DELETE (tombstone) entry.

        Args:
            key: The key being deleted.
        """
        self._append(EntryType.DELETE, key, b"")

    def _append(self, etype: EntryType, key: str, value: bytes) -> None:
        key_bytes = key.encode("utf-8")
        payload = struct.pack("<IIB", len(key_bytes), len(value), int(etype))
        payload += key_bytes + value
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        self._fd.write(struct.pack("<I", crc) + payload)
        self._fd.flush()
        self._entry_count += 1

    def close(self) -> None:
        """Flush and close the WAL file."""
        if not self._fd.closed:
            self._fd.flush()
            self._fd.close()

    def discard(self) -> None:
        """Close and delete the WAL file (after successful flush)."""
        self.close()
        if self._path.exists():
            self._path.unlink()

    @staticmethod
    def replay(path: str | Path) -> Generator[WALEntry, None, None]:
        """Replay a WAL file, yielding valid entries.

        Stops at the first corrupted or incomplete entry (torn write).

        Args:
            path: Path to the WAL file.

        Yields:
            WALEntry for each valid record.
        """
        path = Path(path)
        if not path.exists():
            return

        with open(path, "rb") as f:
            while True:
                header = f.read(4)  # CRC
                if len(header) < 4:
                    break
                stored_crc = struct.unpack("<I", header)[0]

                sub_header = f.read(_HEADER_SIZE - 4)  # key_len, val_len, type
                if len(sub_header) < _HEADER_SIZE - 4:
                    break

                key_len, val_len, etype = struct.unpack("<IIB", sub_header)
                body = f.read(key_len + val_len)
                if len(body) < key_len + val_len:
                    break

                payload = sub_header + body
                computed_crc = zlib.crc32(payload) & 0xFFFFFFFF
                if computed_crc != stored_crc:
                    break  # corruption detected — stop replay

                key = body[:key_len].decode("utf-8")
                value = body[key_len:]
                yield WALEntry(
                    entry_type=EntryType(etype),
                    key=key,
                    value=value,
                )


if __name__ == "__main__":
    import argparse
    import tempfile
    import time

    parser = argparse.ArgumentParser(description="WAL crash-recovery demo")
    parser.add_argument("--operations", type=int, default=5000)
    parser.add_argument("--crash-after", type=int, default=2500)
    parser.add_argument("--recover", action="store_true", default=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        wal_path = os.path.join(tmpdir, "demo.wal")

        # Phase 1: write entries, simulate crash
        wal = WriteAheadLog(wal_path)
        t0 = time.perf_counter()
        for i in range(args.operations):
            if i == args.crash_after:
                print(f"  ⚡ Simulated crash after {i} entries")
                wal.close()
                # Corrupt last few bytes to simulate torn write
                with open(wal_path, "ab") as f:
                    f.write(b"\x00\x01\x02")
                break
            wal.append_put(f"key_{i:06d}", f"value_{i}".encode())
        else:
            wal.close()
        write_ms = (time.perf_counter() - t0) * 1000

        # Phase 2: replay
        t0 = time.perf_counter()
        recovered = list(WriteAheadLog.replay(wal_path))
        replay_ms = (time.perf_counter() - t0) * 1000

        print(f"\nWAL demo — {args.operations} ops, crash after {args.crash_after}")
        print(f"  Written entries : {args.crash_after}")
        print(f"  Recovered entries: {len(recovered)}")
        print(f"  Write time      : {write_ms:.1f} ms")
        print(f"  Replay time     : {replay_ms:.1f} ms")
        print(f"  Integrity       : {'OK' if len(recovered) == args.crash_after else 'PARTIAL'}")
        if recovered:
            print(f"  First recovered : {recovered[0].key} = {recovered[0].value.decode()}")
            print(f"  Last recovered  : {recovered[-1].key} = {recovered[-1].value.decode()}")
