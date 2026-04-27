"""Block encoding/decoding with prefix compression.

Data blocks store sorted key-value pairs using prefix compression: for each
entry we store the shared prefix length with the previous key, then only the
differing suffix. This reduces block size by 30-50% for keys with common
prefixes (e.g. "user:1001", "user:1002").

Block format:
  [num_entries (4B)]
  For each entry:
    [shared_prefix_len (2B)] [suffix_len (2B)] [value_len (4B)]
    [suffix bytes] [value bytes]
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional


_ENTRY_HEADER = "<HHI"  # shared_prefix_len, suffix_len, value_len
_ENTRY_HEADER_SIZE = struct.calcsize(_ENTRY_HEADER)


@dataclass(frozen=True)
class BlockEntry:
    """A decoded key-value entry from a data block."""
    key: str
    value: bytes


class BlockBuilder:
    """Builds a data block with prefix-compressed key-value entries.

    Args:
        target_size: Approximate target block size in bytes.
    """

    def __init__(self, target_size: int = 4096) -> None:
        self._target_size = target_size
        self._entries: list[tuple[str, bytes]] = []
        self._size: int = 4  # 4 bytes for num_entries header
        self._last_key: str = ""

    @property
    def estimated_size(self) -> int:
        """Current estimated block size in bytes."""
        return self._size

    @property
    def entry_count(self) -> int:
        """Number of entries added so far."""
        return len(self._entries)

    def is_full(self) -> bool:
        """Check if the block has reached its target size."""
        return self._size >= self._target_size

    def add(self, key: str, value: bytes) -> bool:
        """Add a key-value pair to the block.

        Args:
            key: The key (must be >= last added key for sorted order).
            value: The value bytes.

        Returns:
            True if the entry was added, False if the block is full.
        """
        if self._entries and self._size >= self._target_size:
            return False

        shared = _shared_prefix_len(self._last_key, key)
        suffix = key[shared:]
        entry_size = _ENTRY_HEADER_SIZE + len(suffix.encode()) + len(value)
        self._entries.append((key, value))
        self._size += entry_size
        self._last_key = key
        return True

    def first_key(self) -> Optional[str]:
        """Return the first key in this block, or None if empty."""
        return self._entries[0][0] if self._entries else None

    def last_key(self) -> Optional[str]:
        """Return the last key in this block, or None if empty."""
        return self._entries[-1][0] if self._entries else None

    def finish(self) -> bytes:
        """Serialize the block to bytes.

        Returns:
            Encoded block bytes with prefix compression.
        """
        parts: list[bytes] = [struct.pack("<I", len(self._entries))]
        prev_key = ""
        for key, value in self._entries:
            shared = _shared_prefix_len(prev_key, key)
            suffix = key[shared:].encode("utf-8")
            parts.append(struct.pack(_ENTRY_HEADER, shared, len(suffix), len(value)))
            parts.append(suffix)
            parts.append(value)
            prev_key = key
        return b"".join(parts)

    def reset(self) -> None:
        """Clear the builder for reuse."""
        self._entries.clear()
        self._size = 4
        self._last_key = ""


class BlockReader:
    """Decodes a prefix-compressed data block.

    Args:
        data: Raw block bytes.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._num_entries = struct.unpack_from("<I", data, 0)[0]

    @property
    def num_entries(self) -> int:
        """Number of entries in this block."""
        return self._num_entries

    def entries(self) -> list[BlockEntry]:
        """Decode and return all entries in the block.

        Returns:
            List of BlockEntry with fully reconstructed keys.
        """
        result: list[BlockEntry] = []
        offset = 4
        prev_key = ""
        for _ in range(self._num_entries):
            shared, suffix_len, val_len = struct.unpack_from(
                _ENTRY_HEADER, self._data, offset
            )
            offset += _ENTRY_HEADER_SIZE
            suffix = self._data[offset:offset + suffix_len].decode("utf-8")
            offset += suffix_len
            value = self._data[offset:offset + val_len]
            offset += val_len
            full_key = prev_key[:shared] + suffix
            result.append(BlockEntry(key=full_key, value=value))
            prev_key = full_key
        return result

    def find(self, target_key: str) -> Optional[bytes]:
        """Search for a key within the block via linear scan.

        Args:
            target_key: The key to find.

        Returns:
            Value bytes if found, None otherwise.
        """
        for entry in self.entries():
            if entry.key == target_key:
                return entry.value
            if entry.key > target_key:
                break
        return None


def _shared_prefix_len(a: str, b: str) -> int:
    """Compute the length of the shared prefix between two strings."""
    i = 0
    limit = min(len(a), len(b))
    while i < limit and a[i] == b[i]:
        i += 1
    return i


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Block encoding demo")
    parser.add_argument("--entries", type=int, default=50)
    parser.add_argument("--block-size", type=int, default=4096)
    args = parser.parse_args()

    builder = BlockBuilder(target_size=args.block_size)
    for i in range(args.entries):
        key = f"user:profile:{i:06d}"
        val = f"data_{i}".encode()
        if not builder.add(key, val):
            break

    raw = builder.finish()
    reader = BlockReader(raw)
    decoded = reader.entries()

    uncompressed = sum(len(e.key.encode()) + len(e.value) for e in decoded)
    ratio = len(raw) / max(uncompressed, 1)

    print(f"Block encoding — {builder.entry_count} entries")
    print(f"  Uncompressed : {uncompressed:,} bytes")
    print(f"  Compressed   : {len(raw):,} bytes")
    print(f"  Ratio        : {ratio:.2%}")
    print(f"  First key    : {decoded[0].key}")
    print(f"  Last key     : {decoded[-1].key}")

    # Verify round-trip
    found = reader.find(decoded[len(decoded) // 2].key)
    print(f"  Lookup test  : {'OK' if found else 'FAIL'}")
