"""A B+tree of byte-string keys and values, stored in pager pages.

Keys compare as plain bytes. Tables and indexes encode their keys so that
byte order matches SQL order (see keys.py), which keeps this module
completely ignorant of types.

Node layout on a page:

    kind u8 (1 = leaf, 2 = interior) | count u16 | link u32 | cells...

    leaf cell:      keylen u16 | vallen u16 | key | value     link = next leaf
    interior cell:  child u32  | keylen u16 | key             link = rightmost child

An interior node with keys k1..kn and children c1..cn, cn+1 sends a key to
the first child whose separator is greater than it: child ci holds keys
< ki, and the rightmost child holds everything >= kn.

Deletes don't merge underfull nodes. Most engines don't bother either; the
space is reused by later inserts, and VACUUM rebuilds the file compactly.
"""

import struct
from bisect import bisect_left, bisect_right

from .pager import PAGE_SIZE

LEAF, INTERIOR = 1, 2
NODE_HEADER = struct.Struct(">BHI")
LEAF_CELL = struct.Struct(">HH")
INTERIOR_CELL = struct.Struct(">IH")
USABLE = PAGE_SIZE - NODE_HEADER.size

# every node must hold at least 4 cells, or a split could still overflow
MAX_CELL = USABLE // 4 - LEAF_CELL.size


class TreeError(Exception):
    pass


class DuplicateKey(TreeError):
    pass


class Node:
    __slots__ = ("pgno", "kind", "keys", "values", "children", "link")

    def __init__(self, pgno, kind, keys=None, values=None, children=None, link=0):
        self.pgno = pgno
        self.kind = kind
        self.keys = keys if keys is not None else []
        self.values = values if values is not None else []      # leaf only
        self.children = children if children is not None else []  # interior only
        self.link = link

    @property
    def is_leaf(self):
        return self.kind == LEAF

    def size(self):
        if self.is_leaf:
            return sum(LEAF_CELL.size + len(k) + len(v) for k, v in zip(self.keys, self.values))
        return sum(INTERIOR_CELL.size + len(k) for k in self.keys)

    @classmethod
    def load(cls, pager, pgno):
        page = pager.read(pgno)
        kind, count, link = NODE_HEADER.unpack_from(page, 0)
        if kind not in (LEAF, INTERIOR):
            raise TreeError(f"page {pgno} is not a tree node")
        node = cls(pgno, kind, link=link)
        pos = NODE_HEADER.size
        if kind == LEAF:
            for _ in range(count):
                klen, vlen = LEAF_CELL.unpack_from(page, pos)
                pos += LEAF_CELL.size
                node.keys.append(bytes(page[pos:pos + klen]))
                pos += klen
                node.values.append(bytes(page[pos:pos + vlen]))
                pos += vlen
        else:
            for _ in range(count):
                child, klen = INTERIOR_CELL.unpack_from(page, pos)
                pos += INTERIOR_CELL.size
                node.children.append(child)
                node.keys.append(bytes(page[pos:pos + klen]))
                pos += klen
            node.children.append(link)
        return node

    def save(self, pager):
        page = pager.write(self.pgno)
        page[:] = bytes(PAGE_SIZE)
        link = self.link if self.is_leaf else self.children[-1]
        NODE_HEADER.pack_into(page, 0, self.kind, len(self.keys), link)
        pos = NODE_HEADER.size
        if self.is_leaf:
            for k, v in zip(self.keys, self.values):
                LEAF_CELL.pack_into(page, pos, len(k), len(v))
                pos += LEAF_CELL.size
                page[pos:pos + len(k)] = k
                pos += len(k)
                page[pos:pos + len(v)] = v
                pos += len(v)
        else:
            for child, k in zip(self.children, self.keys):
                INTERIOR_CELL.pack_into(page, pos, child, len(k))
                pos += INTERIOR_CELL.size
                page[pos:pos + len(k)] = k
                pos += len(k)


class BTree:
    def __init__(self, pager, root):
        self.pager = pager
        self.root = root

    @classmethod
    def create(cls, pager):
        root = pager.allocate()
        Node(root, LEAF).save(pager)
        return cls(pager, root)

    def _load(self, pgno):
        return Node.load(self.pager, pgno)

    # --- lookups --------------------------------------------------------------

    def _find_leaf(self, key):
        node = self._load(self.root)
        while not node.is_leaf:
            node = self._load(node.children[bisect_right(node.keys, key)])
        return node

    def get(self, key):
        leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            return leaf.values[i]
        return None

    def scan(self, start=None, end=None, end_inclusive=False):
        """Yield (key, value) for start <= key < end (or <= end), in order.

        The tree must not be modified while this generator is running.
        """
        leaf = self._find_leaf(start) if start is not None else self._leftmost_leaf()
        i = bisect_left(leaf.keys, start) if start is not None else 0
        while True:
            for j in range(i, len(leaf.keys)):
                k = leaf.keys[j]
                if end is not None and (k > end or (k == end and not end_inclusive)):
                    return
                yield k, leaf.values[j]
            if not leaf.link:
                return
            leaf = self._load(leaf.link)
            i = 0

    def scan_prefix(self, prefix):
        for k, v in self.scan(prefix):
            if not k.startswith(prefix):
                return
            yield k, v

    def _leftmost_leaf(self):
        node = self._load(self.root)
        while not node.is_leaf:
            node = self._load(node.children[0])
        return node

    def last_key(self):
        # deletes can leave empty leaves on the right, so step left if needed
        def last(pgno):
            node = self._load(pgno)
            if node.is_leaf:
                return node.keys[-1] if node.keys else None
            for child in reversed(node.children):
                found = last(child)
                if found is not None:
                    return found
            return None
        return last(self.root)

    # --- insert ---------------------------------------------------------------

    def insert(self, key, value, replace=False):
        if len(key) + len(value) > MAX_CELL:
            raise TreeError(f"row too large ({len(key) + len(value)} bytes, max {MAX_CELL})")
        split = self._insert(self.root, key, value, replace)
        if split is None:
            return
        # the root splits: keep its page number (the catalog points at it)
        # by moving its content to a new page and making it the parent
        sep, right = split
        root = self._load(self.root)
        left = Node(self.pager.allocate(), root.kind, root.keys, root.values, root.children, root.link)
        left.save(self.pager)
        Node(self.root, INTERIOR, [sep], children=[left.pgno, right]).save(self.pager)

    def _insert(self, pgno, key, value, replace):
        node = self._load(pgno)
        if node.is_leaf:
            i = bisect_left(node.keys, key)
            if i < len(node.keys) and node.keys[i] == key:
                if not replace:
                    raise DuplicateKey(key)
                node.values[i] = value
            else:
                node.keys.insert(i, key)
                node.values.insert(i, value)
        else:
            i = bisect_right(node.keys, key)
            split = self._insert(node.children[i], key, value, replace)
            if split is None:
                return None
            sep, right = split
            node.keys.insert(i, sep)
            node.children.insert(i + 1, right)

        if node.size() <= USABLE:
            node.save(self.pager)
            return None
        return self._split(node)

    def _split(self, node):
        right = Node(self.pager.allocate(), node.kind)
        if node.is_leaf:
            # split by bytes, not count, so both halves fit even with mixed sizes
            total = node.size()
            acc = 0
            mid = 0
            while acc < total // 2:
                acc += LEAF_CELL.size + len(node.keys[mid]) + len(node.values[mid])
                mid += 1
            mid = min(max(mid, 1), len(node.keys) - 1)
            right.keys, node.keys = node.keys[mid:], node.keys[:mid]
            right.values, node.values = node.values[mid:], node.values[:mid]
            right.link, node.link = node.link, right.pgno
            sep = right.keys[0]
        else:
            # also by bytes: a few huge keys next to many tiny ones would
            # otherwise leave one half over the page size
            total = node.size()
            acc = 0
            mid = 0
            while acc < total // 2:
                acc += INTERIOR_CELL.size + len(node.keys[mid])
                mid += 1
            mid = min(max(mid, 1), len(node.keys) - 2)
            sep = node.keys[mid]
            right.keys, node.keys = node.keys[mid + 1:], node.keys[:mid]
            right.children, node.children = node.children[mid + 1:], node.children[:mid + 1]
        node.save(self.pager)
        right.save(self.pager)
        return sep, right.pgno

    # --- delete ---------------------------------------------------------------

    def delete(self, key):
        leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            del leaf.keys[i]
            del leaf.values[i]
            leaf.save(self.pager)
            return True
        return False

    def destroy(self):
        """Free every page of the tree, root included."""
        def walk(pgno):
            node = self._load(pgno)
            if not node.is_leaf:
                for child in node.children:
                    walk(child)
            self.pager.free(pgno)
        walk(self.root)

    # --- debugging ------------------------------------------------------------

    def check(self):
        """Verify the tree's invariants. Returns (depth, key count)."""
        depths = set()
        count = 0
        prev = None

        def walk(pgno, low, high, depth):
            nonlocal count, prev
            node = self._load(pgno)
            if node.keys != sorted(node.keys):
                raise TreeError(f"keys out of order in page {pgno}")
            for k in node.keys:
                if (low is not None and k < low) or (high is not None and k >= high):
                    raise TreeError(f"key outside its separator range in page {pgno}")
            if node.is_leaf:
                depths.add(depth)
                count += len(node.keys)
                return
            bounds = [low, *node.keys, high]
            for i, child in enumerate(node.children):
                walk(child, bounds[i], bounds[i + 1], depth + 1)

        walk(self.root, None, None, 0)
        if len(depths) > 1:
            raise TreeError(f"leaves at different depths: {sorted(depths)}")

        # the leaf chain must visit every key, in order
        chained = 0
        for k, _ in self.scan():
            if prev is not None and k <= prev:
                raise TreeError("leaf chain out of order")
            prev = k
            chained += 1
        if chained != count:
            raise TreeError(f"leaf chain has {chained} keys, tree has {count}")
        return depths.pop(), count
