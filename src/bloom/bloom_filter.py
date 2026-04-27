"""Bloom filter with configurable bits-per-key and optimal hash count.

A space-efficient probabilistic set that answers membership queries with
either "definitely not present" or "possibly present". False positive rate
is approximately (1 - e^{-kn/m})^k where n = items, m = bits, k = hashes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .hash_functions import double_hash_probes


@dataclass
class BloomFilter:
    """Bloom filter backed by a bytearray bit vector.

    Args:
        expected_items: Anticipated number of insertions.
        bits_per_key: Bits allocated per expected key (controls FPR).
    """

    expected_items: int
    bits_per_key: float = 10.0
    _num_bits: int = field(init=False)
    _num_hashes: int = field(init=False)
    _bits: bytearray = field(init=False, repr=False)
    _count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._num_bits = max(64, int(self.expected_items * self.bits_per_key))
        self._num_hashes = max(1, round(self._num_bits / max(self.expected_items, 1) * math.log(2)))
        byte_count = (self._num_bits + 7) // 8
        self._bits = bytearray(byte_count)

    @classmethod
    def from_fpr(cls, expected_items: int, fpr: float) -> "BloomFilter":
        """Create a bloom filter targeting a specific false-positive rate.

        Uses m = -n ln(p) / (ln 2)^2 to compute optimal bit count.

        Args:
            expected_items: Expected number of keys.
            fpr: Target false positive rate (e.g. 0.01 for 1%).

        Returns:
            Configured BloomFilter instance.
        """
        if fpr <= 0 or fpr >= 1:
            raise ValueError("FPR must be in (0, 1)")
        bits_per_key = -math.log(fpr) / (math.log(2) ** 2)
        return cls(expected_items=expected_items, bits_per_key=bits_per_key)

    @property
    def num_bits(self) -> int:
        """Total number of bits in the filter."""
        return self._num_bits

    @property
    def num_hashes(self) -> int:
        """Number of hash probes per key."""
        return self._num_hashes

    @property
    def count(self) -> int:
        """Number of items added."""
        return self._count

    def theoretical_fpr(self) -> float:
        """Compute the theoretical false positive rate for current load."""
        if self._count == 0:
            return 0.0
        k, n, m = self._num_hashes, self._count, self._num_bits
        return (1 - math.exp(-k * n / m)) ** k

    def add(self, key: bytes) -> None:
        """Insert a key into the filter.

        Args:
            key: Raw bytes of the key to add.
        """
        for pos in double_hash_probes(key, self._num_hashes, self._num_bits):
            self._bits[pos >> 3] |= 1 << (pos & 7)
        self._count += 1

    def might_contain(self, key: bytes) -> bool:
        """Check if a key might be in the filter.

        Returns False if the key is definitely absent, True if possibly present.

        Args:
            key: Raw bytes of the key to query.

        Returns:
            True if possibly present, False if definitely absent.
        """
        for pos in double_hash_probes(key, self._num_hashes, self._num_bits):
            if not (self._bits[pos >> 3] & (1 << (pos & 7))):
                return False
        return True

    def to_bytes(self) -> bytes:
        """Serialize the filter to bytes for on-disk storage."""
        import struct
        header = struct.pack("<III", self._num_bits, self._num_hashes, self._count)
        return header + bytes(self._bits)

    @classmethod
    def from_bytes(cls, data: bytes) -> "BloomFilter":
        """Deserialize a bloom filter from bytes.

        Args:
            data: Serialized filter bytes.

        Returns:
            Reconstructed BloomFilter.
        """
        import struct
        num_bits, num_hashes, count = struct.unpack_from("<III", data)
        bf = object.__new__(cls)
        bf.expected_items = count or 1
        bf.bits_per_key = num_bits / max(bf.expected_items, 1)
        bf._num_bits = num_bits
        bf._num_hashes = num_hashes
        bf._count = count
        bf._bits = bytearray(data[12:])
        return bf


def benchmark_fpr(num_keys: int, bits_per_key: float, num_trials: int = 10000) -> float:
    """Measure empirical false positive rate.

    Args:
        num_keys: Number of keys to insert.
        bits_per_key: Bits per key.
        num_trials: Number of non-member queries.

    Returns:
        Measured false positive rate.
    """
    bf = BloomFilter(expected_items=num_keys, bits_per_key=bits_per_key)
    for i in range(num_keys):
        bf.add(f"member_{i}".encode())

    false_positives = sum(
        1 for i in range(num_trials)
        if bf.might_contain(f"nonmember_{i}".encode())
    )
    return false_positives / num_trials


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bloom filter benchmark")
    parser.add_argument("--keys", type=int, default=100000, help="Keys to insert")
    parser.add_argument("--bits-per-key", type=float, default=10.0, help="Bits per key")
    parser.add_argument("--trials", type=int, default=50000, help="Non-member queries")
    args = parser.parse_args()

    print(f"Bloom filter — {args.keys:,} keys, {args.bits_per_key} bits/key\n")

    bf = BloomFilter(expected_items=args.keys, bits_per_key=args.bits_per_key)
    print(f"  Bit array size : {bf.num_bits:,} bits ({bf.num_bits // 8:,} bytes)")
    print(f"  Hash functions : {bf.num_hashes}")
    print(f"  Theoretical FPR: {bf.theoretical_fpr():.6f} (empty)\n")

    for i in range(args.keys):
        bf.add(f"key_{i}".encode())

    print(f"  After {bf.count:,} insertions:")
    print(f"  Theoretical FPR: {bf.theoretical_fpr():.6f}")

    empirical = benchmark_fpr(args.keys, args.bits_per_key, args.trials)
    print(f"  Empirical FPR  : {empirical:.6f} ({args.trials:,} trials)")

    serialized = bf.to_bytes()
    restored = BloomFilter.from_bytes(serialized)
    print(f"\n  Serialized size: {len(serialized):,} bytes")
    print(f"  Round-trip OK  : {all(restored.might_contain(f'key_{i}'.encode()) for i in range(min(1000, args.keys)))}")
