"""Size-tiered compaction strategy (STCS).

Groups SSTables of similar size into tiers. When a tier accumulates enough
files (typically 4), they are merged into a single larger SSTable. This is
write-friendly — each byte is rewritten O(log T) times — but can use up to
2x disk space during compaction.
"""

from __future__ import annotations

import heapq
import tempfile
import time
from pathlib import Path
from typing import Generator, Optional

from ..sstable.block import BlockEntry
from ..sstable.reader import SSTableReader
from ..sstable.writer import SSTableMeta, SSTableWriter
from .manifest import Manifest


class SizeTieredCompaction:
    """Size-tiered compaction strategy.

    Args:
        manifest: The version manifest tracking live SSTables.
        output_dir: Directory for compaction output files.
        min_threshold: Minimum files of similar size to trigger compaction.
        max_threshold: Maximum files to compact at once.
        bucket_low: Lower bound for size similarity ratio.
        bucket_high: Upper bound for size similarity ratio.
        block_size: Block size for output SSTables.
    """

    def __init__(self, manifest: Manifest, output_dir: str | Path,
                 min_threshold: int = 4, max_threshold: int = 32,
                 bucket_low: float = 0.5, bucket_high: float = 1.5,
                 block_size: int = 4096) -> None:
        self._manifest = manifest
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._min_threshold = min_threshold
        self._max_threshold = max_threshold
        self._bucket_low = bucket_low
        self._bucket_high = bucket_high
        self._block_size = block_size
        self._compaction_count = 0

    def needs_compaction(self) -> bool:
        """Check if any size bucket has enough files to compact."""
        buckets = self._find_buckets()
        return any(len(b) >= self._min_threshold for b in buckets)

    def run(self) -> Optional[SSTableMeta]:
        """Execute one round of size-tiered compaction.

        Finds the largest bucket of similarly-sized SSTables, merges them
        into a single new SSTable, and updates the manifest.

        Returns:
            Metadata of the new SSTable, or None if no compaction needed.
        """
        buckets = self._find_buckets()
        candidates = [b for b in buckets if len(b) >= self._min_threshold]
        if not candidates:
            return None

        # Pick the bucket with the most files
        bucket = max(candidates, key=len)
        if len(bucket) > self._max_threshold:
            bucket = bucket[:self._max_threshold]

        return self._compact(bucket)

    def _find_buckets(self) -> list[list[SSTableMeta]]:
        """Group SSTables into size-similar buckets."""
        tables = sorted(self._manifest.all_tables(), key=lambda t: t.file_size)
        if not tables:
            return []

        buckets: list[list[SSTableMeta]] = []
        current_bucket: list[SSTableMeta] = [tables[0]]

        for t in tables[1:]:
            avg_size = sum(x.file_size for x in current_bucket) / len(current_bucket)
            if avg_size > 0:
                ratio = t.file_size / avg_size
                if self._bucket_low <= ratio <= self._bucket_high:
                    current_bucket.append(t)
                    continue
            buckets.append(current_bucket)
            current_bucket = [t]

        buckets.append(current_bucket)
        return buckets

    def _compact(self, tables: list[SSTableMeta]) -> SSTableMeta:
        """Merge multiple SSTables into one.

        Args:
            tables: SSTables to merge.

        Returns:
            Metadata of the merged SSTable.
        """
        self._compaction_count += 1
        output_path = self._output_dir / f"stcs_{self._compaction_count:06d}.sst"

        merged = _merge_sstables(tables)
        total_entries = sum(t.num_entries for t in tables)
        writer = SSTableWriter(output_path, block_size=self._block_size)
        meta = writer.write(merged, expected_count=total_entries)

        # Update manifest: remove old, add new
        for t in tables:
            self._manifest.remove_tables([t], t.level)
        self._manifest.add_table(meta, level=0)

        # Clean up old files
        for t in tables:
            if t.path.exists():
                t.path.unlink()

        return meta


def _merge_sstables(tables: list[SSTableMeta]) -> Generator[tuple[str, bytes], None, None]:
    """K-way merge of multiple SSTables, keeping only the newest value per key.

    Args:
        tables: SSTables to merge (newest first by convention).

    Yields:
        (key, value) pairs in sorted order with duplicates resolved.
    """
    readers = [SSTableReader(t.path) for t in tables]
    iterators = [iter(r) for r in readers]

    # Priority queue: (key, table_index, value, iterator)
    heap: list[tuple[str, int, bytes, int]] = []
    for idx, it in enumerate(iterators):
        entry = next(it, None)
        if entry is not None:
            heapq.heappush(heap, (entry.key, idx, entry.value, idx))

    last_key: Optional[str] = None
    while heap:
        key, _, value, src_idx = heapq.heappop(heap)

        # Advance the source iterator
        entry = next(iterators[src_idx], None)
        if entry is not None:
            heapq.heappush(heap, (entry.key, src_idx, entry.value, src_idx))

        # Deduplicate: keep first occurrence (newest table wins)
        if key != last_key:
            yield key, value
            last_key = key


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Size-tiered compaction demo")
    parser.add_argument("--keys", type=int, default=10000)
    parser.add_argument("--files", type=int, default=8)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        manifest = Manifest(num_levels=4, base_level_size=1024 * 1024)
        keys_per_file = args.keys // args.files

        # Create initial SSTables
        for f_idx in range(args.files):
            path = Path(tmpdir) / f"input_{f_idx:04d}.sst"
            writer = SSTableWriter(path, block_size=2048)
            start = f_idx * keys_per_file
            entries = [
                (f"key_{i:08d}", f"val_{i}".encode())
                for i in range(start, start + keys_per_file)
            ]
            meta = writer.write(entries, expected_count=keys_per_file)
            manifest.add_table(meta, level=0)

        print(f"Size-tiered compaction — {args.files} files, "
              f"{args.keys:,} total keys\n")
        print("Before compaction:")
        print(manifest.summary())

        stcs = SizeTieredCompaction(manifest, output_dir=tmpdir, min_threshold=4)

        rounds = 0
        t0 = time.perf_counter()
        while stcs.needs_compaction():
            result = stcs.run()
            rounds += 1
            if result:
                print(f"\n  Round {rounds}: merged -> {result.path.name} "
                      f"({result.num_entries:,} entries, {result.file_size:,} bytes)")
        elapsed = (time.perf_counter() - t0) * 1000

        print(f"\nAfter compaction ({rounds} rounds, {elapsed:.1f} ms):")
        print(manifest.summary())
