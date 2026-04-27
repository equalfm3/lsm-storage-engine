"""Memtable wrapper with size tracking and freeze/flush logic.

The memtable buffers writes in a red-black tree and tracks approximate byte
usage. When the size threshold is reached the memtable is frozen (becomes
immutable) and a new active memtable is created. The frozen memtable is then
flushed to an SSTable on disk.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Optional

from .red_black_tree import RedBlackTree
from .wal import EntryType, WriteAheadLog


_TOMBSTONE = b"__TOMBSTONE__"


@dataclass
class MemtableConfig:
    """Configuration for the memtable layer.

    Args:
        max_size_bytes: Flush threshold in bytes.
        wal_dir: Directory for WAL segments.
    """
    max_size_bytes: int = 4 * 1024 * 1024  # 4 MB
    wal_dir: str = "/tmp/lsm_wal"


class Memtable:
    """In-memory sorted buffer backed by a red-black tree and WAL.

    Args:
        config: Memtable configuration.
    """

    def __init__(self, config: Optional[MemtableConfig] = None) -> None:
        self._config = config or MemtableConfig()
        self._tree = RedBlackTree()
        self._approx_bytes: int = 0
        self._frozen: bool = False

        wal_dir = Path(self._config.wal_dir)
        wal_dir.mkdir(parents=True, exist_ok=True)
        wal_name = f"wal_{int(time.time() * 1e6)}.log"
        self._wal = WriteAheadLog(wal_dir / wal_name)

    @property
    def size_bytes(self) -> int:
        """Approximate byte usage of the memtable."""
        return self._approx_bytes

    @property
    def count(self) -> int:
        """Number of entries (including tombstones)."""
        return self._tree.size

    @property
    def is_frozen(self) -> bool:
        """Whether this memtable has been frozen for flushing."""
        return self._frozen

    def should_flush(self) -> bool:
        """Check if the memtable has exceeded its size threshold."""
        return self._approx_bytes >= self._config.max_size_bytes

    def put(self, key: str, value: bytes) -> None:
        """Insert or update a key-value pair.

        Args:
            key: The key string.
            value: The value bytes.

        Raises:
            RuntimeError: If the memtable is frozen.
        """
        if self._frozen:
            raise RuntimeError("Cannot write to a frozen memtable")
        self._wal.append_put(key, value)
        self._tree.put(key, value)
        self._approx_bytes += len(key) + len(value) + 64  # overhead estimate

    def delete(self, key: str) -> None:
        """Mark a key as deleted by inserting a tombstone.

        Args:
            key: The key to delete.

        Raises:
            RuntimeError: If the memtable is frozen.
        """
        if self._frozen:
            raise RuntimeError("Cannot write to a frozen memtable")
        self._wal.append_delete(key)
        self._tree.put(key, _TOMBSTONE)
        self._approx_bytes += len(key) + len(_TOMBSTONE) + 64

    def get(self, key: str) -> Optional[bytes]:
        """Look up a key. Returns None if absent, _TOMBSTONE if deleted.

        Args:
            key: The key to look up.

        Returns:
            Value bytes, _TOMBSTONE sentinel, or None.
        """
        return self._tree.get(key)

    def items(self) -> Generator[tuple[str, bytes], None, None]:
        """Yield all (key, value) pairs in sorted order, including tombstones."""
        yield from self._tree.items()

    def freeze(self) -> None:
        """Freeze the memtable — no further writes allowed."""
        self._frozen = True
        self._wal.close()

    def discard_wal(self) -> None:
        """Delete the WAL after a successful flush to SSTable."""
        self._wal.discard()

    @classmethod
    def from_wal(cls, wal_path: str | Path,
                 config: Optional[MemtableConfig] = None) -> "Memtable":
        """Recover a memtable by replaying a WAL file.

        Args:
            wal_path: Path to the WAL segment.
            config: Optional configuration override.

        Returns:
            Reconstructed Memtable.
        """
        mt = cls(config=config)
        for entry in WriteAheadLog.replay(wal_path):
            if entry.entry_type == EntryType.PUT:
                mt._tree.put(entry.key, entry.value)
                mt._approx_bytes += len(entry.key) + len(entry.value) + 64
            elif entry.entry_type == EntryType.DELETE:
                mt._tree.put(entry.key, _TOMBSTONE)
                mt._approx_bytes += len(entry.key) + len(_TOMBSTONE) + 64
        return mt

    @staticmethod
    def is_tombstone(value: bytes) -> bool:
        """Check if a value is a deletion tombstone."""
        return value == _TOMBSTONE


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Memtable demo")
    parser.add_argument("--operations", type=int, default=10000)
    parser.add_argument("--memtable-size", type=int, default=4096)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = MemtableConfig(max_size_bytes=args.memtable_size, wal_dir=tmpdir)
        mt = Memtable(config=cfg)

        flush_count = 0
        t0 = time.perf_counter()
        for i in range(args.operations):
            mt.put(f"key_{i:06d}", f"value_{i}".encode())
            if mt.should_flush():
                mt.freeze()
                flush_count += 1
                mt = Memtable(config=cfg)
        elapsed = (time.perf_counter() - t0) * 1000

        print(f"Memtable demo — {args.operations:,} ops, "
              f"{args.memtable_size:,} byte threshold")
        print(f"  Flushes triggered: {flush_count}")
        print(f"  Final entries    : {mt.count}")
        print(f"  Final size       : {mt.size_bytes:,} bytes")
        print(f"  Elapsed          : {elapsed:.1f} ms")

        # Verify ordering
        keys = [k for k, _ in mt.items()]
        assert keys == sorted(keys), "Memtable iteration not sorted!"
        print(f"  Sorted order     : OK ({len(keys)} keys)")
