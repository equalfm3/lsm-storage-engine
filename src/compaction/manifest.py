"""Version manifest tracking live SSTables per level.

The manifest records which SSTables belong to each compaction level and
provides atomic version updates. It is the source of truth for the current
database state — after a crash, the manifest tells us which files are live.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..sstable.writer import SSTableMeta


@dataclass
class LevelInfo:
    """Metadata for a single compaction level.

    Args:
        level: Level number (0 = freshly flushed).
        tables: SSTables in this level, ordered by first_key.
        size_limit: Maximum total bytes for this level.
    """
    level: int
    tables: list[SSTableMeta] = field(default_factory=list)
    size_limit: int = 0

    @property
    def total_size(self) -> int:
        """Total file size of all SSTables in this level."""
        return sum(t.file_size for t in self.tables)

    @property
    def count(self) -> int:
        """Number of SSTables in this level."""
        return len(self.tables)

    def exceeds_limit(self) -> bool:
        """Check if this level has exceeded its size limit."""
        if self.size_limit <= 0:
            return False
        return self.total_size > self.size_limit

    def overlapping(self, first_key: str, last_key: str) -> list[SSTableMeta]:
        """Find all SSTables whose key range overlaps [first_key, last_key].

        Args:
            first_key: Start of the query range.
            last_key: End of the query range.

        Returns:
            List of overlapping SSTableMeta objects.
        """
        result = []
        for t in self.tables:
            if t.first_key <= last_key and first_key <= t.last_key:
                result.append(t)
        return result


class Manifest:
    """Tracks the set of live SSTables across compaction levels.

    Args:
        num_levels: Maximum number of levels.
        base_level_size: Size limit for Level 1 in bytes.
        size_ratio: Size multiplier between adjacent levels.
        l0_file_limit: Max number of L0 files before compaction.
    """

    def __init__(self, num_levels: int = 7, base_level_size: int = 10 * 1024 * 1024,
                 size_ratio: int = 10, l0_file_limit: int = 4) -> None:
        self._levels: list[LevelInfo] = []
        self._l0_file_limit = l0_file_limit
        self._version: int = 0

        for i in range(num_levels):
            limit = 0 if i == 0 else base_level_size * (size_ratio ** (i - 1))
            self._levels.append(LevelInfo(level=i, size_limit=limit))

    @property
    def version(self) -> int:
        """Current manifest version number."""
        return self._version

    @property
    def num_levels(self) -> int:
        """Number of compaction levels."""
        return len(self._levels)

    def level(self, n: int) -> LevelInfo:
        """Get info for level *n*."""
        return self._levels[n]

    def all_tables(self) -> list[SSTableMeta]:
        """Return all live SSTables across all levels, newest first."""
        result = []
        for lvl in self._levels:
            result.extend(lvl.tables)
        return result

    def add_table(self, table: SSTableMeta, level: int = 0) -> None:
        """Add an SSTable to a level.

        Args:
            table: The SSTable metadata.
            level: Target level (default 0 for freshly flushed).
        """
        table.level = level
        self._levels[level].tables.append(table)
        self._levels[level].tables.sort(key=lambda t: t.first_key)
        self._version += 1

    def remove_tables(self, tables: list[SSTableMeta], level: int) -> None:
        """Remove SSTables from a level (after compaction).

        Args:
            tables: Tables to remove.
            level: The level they belong to.
        """
        paths_to_remove = {t.path for t in tables}
        self._levels[level].tables = [
            t for t in self._levels[level].tables
            if t.path not in paths_to_remove
        ]
        self._version += 1

    def l0_needs_compaction(self) -> bool:
        """Check if Level 0 has too many files."""
        return self._levels[0].count >= self._l0_file_limit

    def level_needs_compaction(self, level: int) -> bool:
        """Check if a level has exceeded its size limit."""
        if level == 0:
            return self.l0_needs_compaction()
        return self._levels[level].exceeds_limit()

    def summary(self) -> str:
        """Human-readable summary of all levels."""
        lines = [f"Manifest v{self._version}:"]
        for lvl in self._levels:
            if lvl.count > 0 or lvl.level <= 2:
                limit_str = f"/{lvl.size_limit:,}" if lvl.size_limit else ""
                lines.append(
                    f"  L{lvl.level}: {lvl.count} files, "
                    f"{lvl.total_size:,}{limit_str} bytes"
                )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize manifest state to a dictionary."""
        return {
            "version": self._version,
            "levels": [
                {
                    "level": lvl.level,
                    "size_limit": lvl.size_limit,
                    "tables": [
                        {
                            "path": str(t.path),
                            "num_entries": t.num_entries,
                            "first_key": t.first_key,
                            "last_key": t.last_key,
                            "file_size": t.file_size,
                        }
                        for t in lvl.tables
                    ],
                }
                for lvl in self._levels
            ],
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manifest demo")
    parser.add_argument("--levels", type=int, default=5)
    parser.add_argument("--base-size", type=int, default=1024)
    parser.add_argument("--ratio", type=int, default=10)
    args = parser.parse_args()

    manifest = Manifest(num_levels=args.levels, base_level_size=args.base_size,
                        size_ratio=args.ratio)

    # Simulate adding SSTables
    for i in range(6):
        meta = SSTableMeta(
            path=Path(f"/tmp/sst_{i:04d}.sst"),
            num_entries=1000,
            first_key=f"key_{i * 1000:08d}",
            last_key=f"key_{(i + 1) * 1000 - 1:08d}",
            file_size=500,
            level=0,
        )
        manifest.add_table(meta, level=0)

    print(manifest.summary())
    print(f"\n  L0 needs compaction: {manifest.l0_needs_compaction()}")
    print(f"  Total tables       : {len(manifest.all_tables())}")
    print(f"  Version            : {manifest.version}")
