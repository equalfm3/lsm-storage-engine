"""SSTable writer: data blocks, index block, bloom filter, footer.

Writes a complete SSTable file from sorted key-value pairs. The file layout:
  [Data Block 0] [Data Block 1] ... [Data Block N]
  [Index Block]
  [Bloom Filter]
  [Footer: index_offset(8B) bloom_offset(8B) num_entries(4B) magic(4B)]
"""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Iterable, Optional

from ..bloom.bloom_filter import BloomFilter
from .block import BlockBuilder

_FOOTER_FMT = "<QQII"  # index_offset, bloom_offset, num_entries, magic
_FOOTER_SIZE = struct.calcsize(_FOOTER_FMT)
_MAGIC = 0x4C534D54  # "LSMT"


class SSTableWriter:
    """Writes sorted key-value pairs to an SSTable file.

    Args:
        path: Output file path.
        block_size: Target data block size in bytes.
        bloom_bits_per_key: Bloom filter bits per key.
    """

    def __init__(self, path: str | Path, block_size: int = 4096,
                 bloom_bits_per_key: float = 10.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._block_size = block_size
        self._bloom_bits_per_key = bloom_bits_per_key

    @property
    def path(self) -> Path:
        """Output file path."""
        return self._path

    def write(self, entries: Iterable[tuple[str, bytes]],
              expected_count: int = 10000) -> "SSTableMeta":
        """Write all entries to the SSTable.

        Args:
            entries: Sorted (key, value) pairs.
            expected_count: Estimated entry count for bloom filter sizing.

        Returns:
            Metadata about the written SSTable.
        """
        bloom = BloomFilter(expected_items=max(expected_count, 1),
                            bits_per_key=self._bloom_bits_per_key)
        index_entries: list[tuple[str, int]] = []  # (first_key, offset)
        num_entries = 0

        with open(self._path, "wb") as f:
            builder = BlockBuilder(target_size=self._block_size)

            def _flush_block() -> None:
                nonlocal num_entries
                if builder.entry_count == 0:
                    return
                first = builder.first_key()
                offset = f.tell()
                block_data = builder.finish()
                f.write(struct.pack("<I", len(block_data)))
                f.write(block_data)
                if first is not None:
                    index_entries.append((first, offset))
                builder.reset()

            for key, value in entries:
                bloom.add(key.encode("utf-8"))
                if not builder.add(key, value):
                    _flush_block()
                    builder.add(key, value)
                num_entries += 1

            _flush_block()

            # Write index block
            index_offset = f.tell()
            index_data = _encode_index(index_entries)
            f.write(index_data)

            # Write bloom filter
            bloom_offset = f.tell()
            bloom_data = bloom.to_bytes()
            f.write(bloom_data)

            # Write footer
            footer = struct.pack(_FOOTER_FMT, index_offset, bloom_offset,
                                 num_entries, _MAGIC)
            f.write(footer)

        first_key = index_entries[0][0] if index_entries else ""
        last_key = ""
        if index_entries:
            last_key = index_entries[-1][0]

        return SSTableMeta(
            path=self._path,
            num_entries=num_entries,
            first_key=first_key,
            last_key=last_key,
            file_size=self._path.stat().st_size,
            level=0,
        )


class SSTableMeta:
    """Metadata for a written SSTable.

    Args:
        path: File path.
        num_entries: Total key-value count.
        first_key: Smallest key in the table.
        last_key: Largest key in the table.
        file_size: File size in bytes.
        level: Compaction level (0 = freshly flushed).
    """

    def __init__(self, path: Path, num_entries: int, first_key: str,
                 last_key: str, file_size: int, level: int = 0) -> None:
        self.path = path
        self.num_entries = num_entries
        self.first_key = first_key
        self.last_key = last_key
        self.file_size = file_size
        self.level = level
        self.created_at = time.time()

    def overlaps(self, other: "SSTableMeta") -> bool:
        """Check if this SSTable's key range overlaps with another."""
        return self.first_key <= other.last_key and other.first_key <= self.last_key

    def __repr__(self) -> str:
        return (f"SSTableMeta(path={self.path.name!r}, entries={self.num_entries}, "
                f"keys=[{self.first_key!r}..{self.last_key!r}], "
                f"level={self.level}, size={self.file_size:,})")


def _encode_index(entries: list[tuple[str, int]]) -> bytes:
    """Encode the index block: [count(4B)] then [key_len(2B) key offset(8B)]..."""
    parts = [struct.pack("<I", len(entries))]
    for key, offset in entries:
        kb = key.encode("utf-8")
        parts.append(struct.pack("<HQ", len(kb), offset))
        parts.append(kb)
    return b"".join(parts)


if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="SSTable writer demo")
    parser.add_argument("--keys", type=int, default=50000)
    parser.add_argument("--block-size", type=int, default=4096)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "demo.sst"
        writer = SSTableWriter(path, block_size=args.block_size)

        def gen_entries():
            for i in range(args.keys):
                yield f"key_{i:08d}", f"value_{i}".encode()

        t0 = time.perf_counter()
        meta = writer.write(gen_entries(), expected_count=args.keys)
        elapsed = (time.perf_counter() - t0) * 1000

        print(f"SSTable writer — {args.keys:,} keys, {args.block_size} byte blocks")
        print(f"  File size    : {meta.file_size:,} bytes")
        print(f"  Entries      : {meta.num_entries:,}")
        print(f"  First key    : {meta.first_key}")
        print(f"  Last key     : {meta.last_key}")
        print(f"  Write time   : {elapsed:.1f} ms")
        print(f"  Throughput   : {args.keys / elapsed * 1000:,.0f} keys/sec")
