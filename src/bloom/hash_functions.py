"""MurmurHash3 and double-hashing for bloom filter probes.

Implements a pure-Python MurmurHash3 (32-bit) and a double-hashing scheme
that generates k hash values from two base hashes. Falls back to mmh3 C
extension when available for better performance.
"""

from __future__ import annotations

import struct
from typing import Sequence

try:
    import mmh3 as _mmh3  # type: ignore[import-untyped]

    _HAS_MMH3 = True
except ImportError:
    _HAS_MMH3 = False


def _rotl32(x: int, r: int) -> int:
    """32-bit left rotate."""
    return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF


def murmur3_32(key: bytes, seed: int = 0) -> int:
    """MurmurHash3 32-bit hash.

    Args:
        key: Raw bytes to hash.
        seed: Hash seed for independent hash families.

    Returns:
        32-bit unsigned hash value.
    """
    if _HAS_MMH3:
        return _mmh3.hash(key, seed, signed=False)

    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed & 0xFFFFFFFF
    length = len(key)
    n_blocks = length // 4

    for i in range(n_blocks):
        k = struct.unpack_from("<I", key, i * 4)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = _rotl32(k, 15)
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = _rotl32(h, 13)
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF

    tail_idx = n_blocks * 4
    k = 0
    tail_len = length & 3
    if tail_len >= 3:
        k ^= key[tail_idx + 2] << 16
    if tail_len >= 2:
        k ^= key[tail_idx + 1] << 8
    if tail_len >= 1:
        k ^= key[tail_idx]
        k = (k * c1) & 0xFFFFFFFF
        k = _rotl32(k, 15)
        k = (k * c2) & 0xFFFFFFFF
        h ^= k

    h ^= length
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def double_hash_probes(key: bytes, num_probes: int, num_bits: int,
                       seed1: int = 0, seed2: int = 42) -> list[int]:
    """Generate *num_probes* bit positions via double hashing.

    Uses h_i(key) = (h1 + i * h2) mod m to derive k independent-ish
    positions from just two base hashes.

    Args:
        key: Raw bytes to hash.
        num_probes: Number of probe positions (k).
        num_bits: Size of the bit array (m).
        seed1: Seed for the first hash.
        seed2: Seed for the second hash.

    Returns:
        List of bit positions in [0, num_bits).
    """
    h1 = murmur3_32(key, seed1)
    h2 = murmur3_32(key, seed2)
    return [(h1 + i * h2) % num_bits for i in range(num_probes)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hash function demo")
    parser.add_argument("--keys", type=int, default=20, help="Number of keys")
    parser.add_argument("--probes", type=int, default=7, help="Probe count")
    parser.add_argument("--bits", type=int, default=128, help="Bit array size")
    args = parser.parse_args()

    print(f"MurmurHash3 demo — {args.keys} keys, {args.probes} probes, "
          f"{args.bits}-bit array\n")

    for i in range(args.keys):
        raw = f"key_{i:04d}".encode()
        h = murmur3_32(raw)
        positions = double_hash_probes(raw, args.probes, args.bits)
        print(f"  {raw.decode():>12s}  hash=0x{h:08X}  probes={positions}")
