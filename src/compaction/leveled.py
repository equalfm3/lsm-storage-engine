"""Leveled compaction strategy (LCS).

Organizes SSTables into levels with strict size limits. Level 0 holds
unsorted flush output. Each subsequent level is T times larger than the
previous. Within each level (except L0), SSTables have non-overlapping
key ranges. When a level overflows, one SSTable is picked and merged with
all overlapping SSTables in the next level.
"""

from __future__ import annotations

import heapq
import tempfile
import time
from pathlib import Path
from typing import Generator, Optional

from ..sstable.reader import SSTableReader
from ..sstable.writer import SSTableMeta, SSTableWriter
from .manifest import Manifest


class LeveledCompaction:
    """Leveled compaction strategy.

    Args:
        manifest: The version manifest.
        output_dir: Directory for compaction output files.
        block_size: Block size for output SSTables.
        target_file_size: Target size for individual output SSTables.
    """

    def __init__(self, manifest: Manifest, output_dir: str | Path,
                 block_size: int = 4096,
                 target_file_size: int = 2 * 1024 * 1024) -> None:
        self._manifest = manifest
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._block_size = block_size
        self._target_file_size = target_file_size
        self._compaction_count = 0

    def needs_compaction(self) -> bool:
        """Check if any level needs compaction."""
        for i in range(self._manifest.num_levels):
            if self._manifest.level_needs_compaction(i):
                return True
        return False

    def run(self) -> list[SSTableMeta]:
        """Execute one round of leveled compaction.

        Finds the first level that needs compaction, picks an SSTable,
        and merges it with overlapping SSTables in the next level.

        Returns:
            List of new SSTables produced, or empty if nothing to do.
        """
        # Check L0 first
        if self._manifest.l0_needs_compaction():
            return self._compact_l0()

        # Check other levels
        for level in range(1, self._manifest.num_levels - 1):
            if self._manifest.level_needs_compaction(level):
                return self._compact_level(level)

        return []

    def _compact_l0(self) -> list[SSTableMeta]:
        """Compact all L0 files into L1.

        L0 files may have overlapping key ranges, so we merge all of them
        with any overlapping L1 files.
        """
        l0_tables = list(self._manifest.level(0).tables)
        if not l0_tables:
            return []

        # Find the key range of all L0 files
        first_key = min(t.first_key for t in l0_tables)
        last_key = max(t.last_key for t in l0_tables)

        # Find overlapping L1 files
        l1_overlap = self._manifest.level(1).overlapping(first_key, last_key)

        # Merge all inputs
        all_inputs = l0_tables + l1_overlap
        merged = _merge_sorted_tables(all_inputs)
        total_entries = sum(t.num_entries for t in all_inputs)

        new_tables = self._write_split(merged, total_entries, target_level=1)

        # Update manifest
        self._manifest.remove_tables(l0_tables, 0)
        if l1_overlap:
            self._manifest.remove_tables(l1_overlap, 1)
        for t in new_tables:
            self._manifest.add_table(t, level=1)

        # Clean up old files
        for t in all_inputs:
            if t.path.exists():
                t.path.unlink()

        return new_tables

    def _compact_level(self, level: int) -> list[SSTableMeta]:
        """Compact one SSTable from *level* into *level+1*.

        Picks the SSTable with the smallest key range overlap with the
        next level to minimize write amplification.
        """
        lvl = self._manifest.level(level)
        next_lvl = self._manifest.level(level + 1)

        if not lvl.tables:
            return []

        # Pick the table with least overlap in the next level
        best_table = lvl.tables[0]
        best_overlap_size = float("inf")
        for t in lvl.tables:
            overlap = next_lvl.overlapping(t.first_key, t.last_key)
            overlap_size = sum(o.file_size for o in overlap)
            if overlap_size < best_overlap_size:
                best_overlap_size = overlap_size
                best_table = t

        overlap_tables = next_lvl.overlapping(best_table.first_key, best_table.last_key)
        all_inputs = [best_table] + overlap_tables
        merged = _merge_sorted_tables(all_inputs)
        total_entries = sum(t.num_entries for t in all_inputs)

        new_tables = self._write_split(merged, total_entries, target_level=level + 1)

        # Update manifest
        self._manifest.remove_tables([best_table], level)
        if overlap_tables:
            self._manifest.remove_tables(overlap_tables, level + 1)
        for t in new_tables:
            self._manifest.add_table(t, level=level + 1)

        for t in all_inputs:
            if t.path.exists():
                t.path.unlink()

        return new_tables

    def _write_split(self, entries: Generator[tuple[str, bytes], None, None],
                     total_entries: int,
                     target_level: int) -> list[SSTableMeta]:
        """Write merged entries, splitting into multiple SSTables if needed.

        Args:
            entries: Sorted, deduplicated (key, value) stream.
            total_entries: Estimated total entries.
            target_level: Level for the output files.

        Returns:
            List of new SSTableMeta objects.
        """
        results: list[SSTableMeta] = []
        buffer: list[tuple[str, bytes]] = []
        buffer_size = 0

        for key, value in entries:
            buffer.append((key, value))
            buffer_size += len(key) + len(value)

            if buffer_size >= self._target_file_size:
                meta = self._flush_buffer(buffer, target_level)
                results.append(meta)
                buffer.clear()
                buffer_size = 0

        if buffer:
            meta = self._flush_buffer(buffer, target_level)
            results.append(meta)

        return results

    def _flush_buffer(self, entries: list[tuple[str, bytes]],
                      level: int) -> SSTableMeta:
        """Write a buffer of entries to a new SSTable file."""
        self._compaction_count += 1
        path = self._output_dir / f"lcs_L{level}_{self._compaction_count:06d}.sst"
        writer = SSTableWriter(path, block_size=self._block_size)
        meta = writer.write(iter(entries), expected_count=len(entries))
        meta.level = level
        return meta


def _merge_sorted_tables(tables: list[SSTableMeta]) -> Generator[tuple[str, bytes], None, None]:
    """K-way merge of SSTables, deduplicating by key (first wins).

    Args:
        tables: SSTables to merge.

    Yields:
        Deduplicated (key, value) pairs in sorted order.
    """
    readers = [SSTableReader(t.path) for t in tables]
    iterators = [iter(r) for r in readers]

    heap: list[tuple[str, int, bytes, int]] = []
    for idx, it in enumerate(iterators):
        entry = next(it, None)
        if entry is not None:
            heapq.heappush(heap, (entry.key, idx, entry.value, idx))

    last_key: Optional[str] = None
    while heap:
        key, _, value, src_idx = heapq.heappop(heap)
        entry = next(iterators[src_idx], None)
        if entry is not None:
            heapq.heappush(heap, (entry.key, src_idx, entry.value, src_idx))
        if key != last_key:
            yield key, value
            last_key = key


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Leveled compaction demo")
    parser.add_argument("--keys", type=int, default=100000)
    parser.add_argument("--size-ratio", type=int, default=10)
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        manifest = Manifest(
            num_levels=4,
            base_level_size=5000,
            size_ratio=args.size_ratio,
            l0_file_limit=4,
        )

        # Simulate flushing memtables to L0
        keys_per_flush = 500
        num_flushes = min(args.keys // keys_per_flush, 20)

        print(f"Leveled compaction — {num_flushes} flushes, "
              f"{keys_per_flush} keys each\n")

        lcs = LeveledCompaction(manifest, output_dir=tmpdir,
                                block_size=2048, target_file_size=3000)

        t0 = time.perf_counter()
        compaction_rounds = 0

        for flush_idx in range(num_flushes):
            path = Path(tmpdir) / f"flush_{flush_idx:04d}.sst"
            writer = SSTableWriter(path, block_size=2048)
            start = flush_idx * keys_per_flush
            entries = [
                (f"key_{i:08d}", f"val_{i}".encode())
                for i in range(start, start + keys_per_flush)
            ]
            meta = writer.write(entries, expected_count=keys_per_flush)
            manifest.add_table(meta, level=0)

            while lcs.needs_compaction():
                new_tables = lcs.run()
                compaction_rounds += 1
                if args.verbose and new_tables:
                    print(f"  Compaction round {compaction_rounds}: "
                          f"{len(new_tables)} new SSTable(s)")

        elapsed = (time.perf_counter() - t0) * 1000

        print(f"\nFinal state ({compaction_rounds} compaction rounds, "
              f"{elapsed:.1f} ms):")
        print(manifest.summary())
