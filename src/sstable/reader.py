"""SSTable reader: index lookup, block cache, iterator.

Reads SSTable files written by SSTableWriter. Supports point lookups via
the index block and bloom filter, and full iteration over all entries.
"""

from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path
from typing import Generator, Optional

from ..bloom.bloom_filter import BloomFilter
from .block import BlockEntry, BlockReader

_FOOTER_FMT = "<QQII"
_FOOTER_SIZE = struct.calcsize(_FOOTER_FMT)
_MAGIC = 0x4C534D54


class SSTableReader:
    """Reads an SSTable file for point lookups and range scans.

    Args:
        path: Path to the SSTable file.
        cache_blocks: Max number of data blocks to cache in memory.
    """

    def __init__(self, path: str | Path, cache_blocks: int = 64) -> None:
        self._path = Path(path)
        self._data = self._path.read_bytes()
        self._cache_blocks = cache_blocks

        # Parse footer
        footer_start = len(self._data) - _FOOTER_SIZE
        idx_off, bloom_off, self._num_entries, magic = struct.unpack_from(
            _FOOTER_FMT, self._data, footer_start
        )
        if magic != _MAGIC:
            raise ValueError(f"Invalid SSTable magic: 0x{magic:08X}")

        # Parse bloom filter
        bloom_data = self._data[bloom_off:footer_start]
        self._bloom = BloomFilter.from_bytes(bloom_data)

        # Parse index block
        self._index = _decode_index(self._data[idx_off:bloom_off])

        # Block cache (offset -> decoded entries)
        self._read_block = lru_cache(maxsize=cache_blocks)(self._read_block_impl)

    @property
    def path(self) -> Path:
        """Path to the SSTable file."""
        return self._path

    @property
    def num_entries(self) -> int:
        """Total number of entries in this SSTable."""
        return self._num_entries

    @property
    def index_size(self) -> int:
        """Number of data blocks in this SSTable."""
        return len(self._index)

    def might_contain(self, key: str) -> bool:
        """Check the bloom filter for a key.

        Args:
            key: The key to check.

        Returns:
            False if definitely absent, True if possibly present.
        """
        return self._bloom.might_contain(key.encode("utf-8"))

    def get(self, key: str) -> Optional[bytes]:
        """Point lookup for a key.

        Checks the bloom filter first, then binary-searches the index to
        find the right data block, and scans within the block.

        Args:
            key: The key to look up.

        Returns:
            Value bytes if found, None otherwise.
        """
        if not self.might_contain(key):
            return None

        block_offset = self._find_block(key)
        if block_offset is None:
            return None

        entries = self._read_block(block_offset)
        for entry in entries:
            if entry.key == key:
                return entry.value
            if entry.key > key:
                break
        return None

    def __iter__(self) -> Generator[BlockEntry, None, None]:
        """Iterate over all entries in sorted order."""
        for _, offset in self._index:
            for entry in self._read_block(offset):
                yield entry

    def _find_block(self, key: str) -> Optional[int]:
        """Binary search the index to find the block containing *key*.

        Returns the block offset, or None if key is out of range.
        """
        if not self._index:
            return None

        lo, hi = 0, len(self._index) - 1
        result = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._index[mid][0] <= key:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return self._index[result][1]

    def _read_block_impl(self, offset: int) -> list[BlockEntry]:
        """Read and decode a data block at the given offset."""
        block_len = struct.unpack_from("<I", self._data, offset)[0]
        block_data = self._data[offset + 4:offset + 4 + block_len]
        return BlockReader(block_data).entries()


def _decode_index(data: bytes) -> list[tuple[str, int]]:
    """Decode the index block into (first_key, block_offset) pairs."""
    count = struct.unpack_from("<I", data, 0)[0]
    entries: list[tuple[str, int]] = []
    offset = 4
    for _ in range(count):
        key_len, block_offset = struct.unpack_from("<HQ", data, offset)
        offset += struct.calcsize("<HQ")
        key = data[offset:offset + key_len].decode("utf-8")
        offset += key_len
        entries.append((key, block_offset))
    return entries


if __name__ == "__main__":
    import argparse
    import tempfile
    import time

    from .writer import SSTableWriter

    parser = argparse.ArgumentParser(description="SSTable reader demo")
    parser.add_argument("--keys", type=int, default=50000)
    parser.add_argument("--block-size", type=int, default=4096)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "demo.sst"
        writer = SSTableWriter(path, block_size=args.block_size)
        entries = [(f"key_{i:08d}", f"value_{i}".encode()) for i in range(args.keys)]
        meta = writer.write(entries, expected_count=args.keys)

        reader = SSTableReader(path)
        print(f"SSTable reader — {reader.num_entries:,} entries, "
              f"{reader.index_size} blocks\n")

        # Point lookups
        import random
        sample = random.sample(range(args.keys), min(1000, args.keys))
        t0 = time.perf_counter()
        hits = 0
        for i in sample:
            val = reader.get(f"key_{i:08d}")
            if val is not None:
                hits += 1
        lookup_ms = (time.perf_counter() - t0) * 1000

        # Bloom filter effectiveness
        misses_checked = 0
        bloom_filtered = 0
        for i in range(1000):
            k = f"nonexistent_{i:08d}"
            if not reader.might_contain(k):
                bloom_filtered += 1
            misses_checked += 1

        print(f"  Point lookups  : {hits}/{len(sample)} hits in {lookup_ms:.1f} ms")
        print(f"  Bloom filter   : {bloom_filtered}/{misses_checked} "
              f"true negatives ({bloom_filtered / misses_checked:.1%})")

        # Full scan
        t0 = time.perf_counter()
        scan_count = sum(1 for _ in reader)
        scan_ms = (time.perf_counter() - t0) * 1000
        print(f"  Full scan      : {scan_count:,} entries in {scan_ms:.1f} ms")
