"""Red-black tree with insert, delete, lookup, and in-order traversal.

Self-balancing BST with O(log n) worst-case for all operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Generator, Optional


class Color(IntEnum):
    """Node color in a red-black tree."""
    RED = 0
    BLACK = 1


@dataclass
class RBNode:
    """A single node in the red-black tree."""
    key: str
    value: bytes
    color: Color = Color.RED
    left: Optional["RBNode"] = None
    right: Optional["RBNode"] = None
    parent: Optional["RBNode"] = None


class RedBlackTree:
    """Red-black tree supporting insert, delete, lookup, and ordered iteration.

    Maintains the five red-black invariants to guarantee O(log n) height.
    """

    def __init__(self) -> None:
        self._sentinel: RBNode = RBNode(key="", value=b"", color=Color.BLACK)
        self._root: RBNode = self._sentinel
        self._size: int = 0

    @property
    def size(self) -> int:
        """Number of key-value pairs stored."""
        return self._size

    # ── Lookup ──────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[bytes]:
        """Return the value for *key*, or None if absent."""
        node = self._search(key)
        return None if node is self._sentinel else node.value

    def contains(self, key: str) -> bool:
        """Check whether *key* exists in the tree."""
        return self._search(key) is not self._sentinel

    def _search(self, key: str) -> RBNode:
        cur = self._root
        while cur is not self._sentinel:
            if key == cur.key:
                return cur
            cur = cur.left if key < cur.key else cur.right
        return self._sentinel

    # ── Ordered iteration ───────────────────────────────────────────

    def items(self) -> Generator[tuple[str, bytes], None, None]:
        """Yield (key, value) pairs in sorted order."""
        yield from self._inorder(self._root)

    def _inorder(self, node: RBNode) -> Generator[tuple[str, bytes], None, None]:
        if node is not self._sentinel:
            yield from self._inorder(node.left)
            yield (node.key, node.value)
            yield from self._inorder(node.right)

    # ── Rotations ───────────────────────────────────────────────────

    def _rotate_left(self, x: RBNode) -> None:
        y = x.right
        x.right = y.left
        if y.left is not self._sentinel:
            y.left.parent = x
        y.parent = x.parent
        if x.parent is None:
            self._root = y
        elif x is x.parent.left:
            x.parent.left = y
        else:
            x.parent.right = y
        y.left = x
        x.parent = y

    def _rotate_right(self, x: RBNode) -> None:
        y = x.left
        x.left = y.right
        if y.right is not self._sentinel:
            y.right.parent = x
        y.parent = x.parent
        if x.parent is None:
            self._root = y
        elif x is x.parent.right:
            x.parent.right = y
        else:
            x.parent.left = y
        y.right = x
        x.parent = y

    # ── Insert ──────────────────────────────────────────────────────

    def put(self, key: str, value: bytes) -> None:
        """Insert or update a key-value pair."""
        parent: Optional[RBNode] = None
        cur = self._root
        while cur is not self._sentinel:
            parent = cur
            if key == cur.key:
                cur.value = value
                return
            cur = cur.left if key < cur.key else cur.right

        node = RBNode(key=key, value=value, color=Color.RED,
                      left=self._sentinel, right=self._sentinel, parent=parent)
        if parent is None:
            self._root = node
        elif key < parent.key:
            parent.left = node
        else:
            parent.right = node
        self._size += 1
        self._insert_fixup(node)

    def _insert_fixup(self, z: RBNode) -> None:
        while z.parent is not None and z.parent.color == Color.RED:
            gp = z.parent.parent
            if gp is None:
                break
            if z.parent is gp.left:
                uncle = gp.right
                if uncle.color == Color.RED:
                    z.parent.color = Color.BLACK
                    uncle.color = Color.BLACK
                    gp.color = Color.RED
                    z = gp
                else:
                    if z is z.parent.right:
                        z = z.parent
                        self._rotate_left(z)
                    z.parent.color = Color.BLACK
                    gp.color = Color.RED
                    self._rotate_right(gp)
            else:
                uncle = gp.left
                if uncle.color == Color.RED:
                    z.parent.color = Color.BLACK
                    uncle.color = Color.BLACK
                    gp.color = Color.RED
                    z = gp
                else:
                    if z is z.parent.left:
                        z = z.parent
                        self._rotate_right(z)
                    z.parent.color = Color.BLACK
                    gp.color = Color.RED
                    self._rotate_left(gp)
        self._root.color = Color.BLACK

    # ── Delete ──────────────────────────────────────────────────────

    def delete(self, key: str) -> bool:
        """Remove *key* from the tree. Returns True if the key existed."""
        z = self._search(key)
        if z is self._sentinel:
            return False
        self._delete_node(z)
        self._size -= 1
        return True

    def _transplant(self, u: RBNode, v: RBNode) -> None:
        if u.parent is None:
            self._root = v
        elif u is u.parent.left:
            u.parent.left = v
        else:
            u.parent.right = v
        v.parent = u.parent

    def _tree_minimum(self, node: RBNode) -> RBNode:
        while node.left is not self._sentinel:
            node = node.left
        return node

    def _delete_node(self, z: RBNode) -> None:
        y = z
        y_orig_color = y.color
        if z.left is self._sentinel:
            x = z.right
            self._transplant(z, z.right)
        elif z.right is self._sentinel:
            x = z.left
            self._transplant(z, z.left)
        else:
            y = self._tree_minimum(z.right)
            y_orig_color = y.color
            x = y.right
            if y.parent is z:
                x.parent = y
            else:
                self._transplant(y, y.right)
                y.right = z.right
                y.right.parent = y
            self._transplant(z, y)
            y.left = z.left
            y.left.parent = y
            y.color = z.color
        if y_orig_color == Color.BLACK:
            self._delete_fixup(x)

    def _delete_fixup(self, x: RBNode) -> None:
        while x is not self._root and x.color == Color.BLACK:
            if x is x.parent.left:
                w = x.parent.right
                if w.color == Color.RED:
                    w.color = Color.BLACK
                    x.parent.color = Color.RED
                    self._rotate_left(x.parent)
                    w = x.parent.right
                if w.left.color == Color.BLACK and w.right.color == Color.BLACK:
                    w.color = Color.RED
                    x = x.parent
                else:
                    if w.right.color == Color.BLACK:
                        w.left.color = Color.BLACK
                        w.color = Color.RED
                        self._rotate_right(w)
                        w = x.parent.right
                    w.color = x.parent.color
                    x.parent.color = Color.BLACK
                    w.right.color = Color.BLACK
                    self._rotate_left(x.parent)
                    x = self._root
            else:
                w = x.parent.left
                if w.color == Color.RED:
                    w.color = Color.BLACK
                    x.parent.color = Color.RED
                    self._rotate_right(x.parent)
                    w = x.parent.left
                if w.right.color == Color.BLACK and w.left.color == Color.BLACK:
                    w.color = Color.RED
                    x = x.parent
                else:
                    if w.left.color == Color.BLACK:
                        w.right.color = Color.BLACK
                        w.color = Color.RED
                        self._rotate_left(w)
                        w = x.parent.left
                    w.color = x.parent.color
                    x.parent.color = Color.BLACK
                    w.left.color = Color.BLACK
                    self._rotate_right(x.parent)
                    x = self._root
        x.color = Color.BLACK


if __name__ == "__main__":
    import argparse, random, time

    parser = argparse.ArgumentParser(description="Red-black tree demo")
    parser.add_argument("--keys", type=int, default=10000, help="Number of keys")
    args = parser.parse_args()

    tree = RedBlackTree()
    keys = [f"key_{i:06d}" for i in range(args.keys)]
    random.shuffle(keys)

    t0 = time.perf_counter()
    for k in keys:
        tree.put(k, k.encode())
    insert_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for k in keys:
        assert tree.get(k) == k.encode()
    lookup_ms = (time.perf_counter() - t0) * 1000

    sorted_keys = [kv[0] for kv in tree.items()]
    assert sorted_keys == sorted(keys), "In-order traversal not sorted!"

    random.shuffle(keys)
    t0 = time.perf_counter()
    for k in keys[:len(keys) // 2]:
        tree.delete(k)
    delete_ms = (time.perf_counter() - t0) * 1000

    print(f"Red-black tree — {args.keys:,} keys")
    print(f"  Insert : {insert_ms:8.1f} ms")
    print(f"  Lookup : {lookup_ms:8.1f} ms")
    print(f"  Delete : {delete_ms:8.1f} ms  ({len(keys) // 2:,} deletions)")
    print(f"  Size   : {tree.size:,} remaining")
