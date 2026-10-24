import os
import random

import pytest

from cinder.btree import MAX_CELL, BTree, DuplicateKey, TreeError
from cinder.pager import Pager


@pytest.fixture
def pager(tmp_path):
    p = Pager(str(tmp_path / "t.db"))
    yield p
    p.close()


def k(i):
    return i.to_bytes(4, "big")


def test_insert_get_delete(pager):
    t = BTree.create(pager)
    t.insert(b"b", b"2")
    t.insert(b"a", b"1")
    assert t.get(b"a") == b"1"
    assert t.get(b"zz") is None
    assert t.delete(b"a") is True
    assert t.delete(b"a") is False
    assert list(t.scan()) == [(b"b", b"2")]


def test_duplicates_and_replace(pager):
    t = BTree.create(pager)
    t.insert(b"k", b"v1")
    with pytest.raises(DuplicateKey):
        t.insert(b"k", b"v2")
    t.insert(b"k", b"v2", replace=True)
    assert t.get(b"k") == b"v2"


@pytest.mark.parametrize("order", ["ascending", "descending", "random"])
def test_many_inserts_stay_balanced(pager, order):
    keys = list(range(20000))
    if order == "descending":
        keys.reverse()
    elif order == "random":
        random.Random(1).shuffle(keys)

    t = BTree.create(pager)
    for i in keys:
        t.insert(k(i), b"x" * (i % 50))
    depth, count = t.check()
    assert count == 20000
    # ~120 cells per leaf, ~400 per interior node: one interior level is enough
    assert depth == 1
    assert [key for key, _ in t.scan()] == [k(i) for i in range(20000)]


def test_big_keys_make_a_deep_tree(pager):
    t = BTree.create(pager)
    for i in range(20000):
        t.insert(k(i) + b"-" * 300, b"")
    depth, count = t.check()
    assert depth >= 2 and count == 20000


def test_random_operations_match_a_dict(pager):
    rng = random.Random(7)
    t = BTree.create(pager)
    model = {}
    for step in range(30000):
        key = k(rng.randrange(3000))
        if rng.random() < 0.65:
            value = os.urandom(rng.randrange(0, 200))
            t.insert(key, value, replace=True)
            model[key] = value
        else:
            assert t.delete(key) == (key in model)
            model.pop(key, None)
        if step % 5000 == 0:
            t.check()
    t.check()
    assert dict(t.scan()) == model
    for key in list(model)[:200]:
        assert t.get(key) == model[key]


def test_range_scans(pager):
    t = BTree.create(pager)
    for i in range(0, 1000, 2):
        t.insert(k(i), b"")
    got = [int.from_bytes(key, "big") for key, _ in t.scan(k(101), k(111))]
    assert got == [102, 104, 106, 108, 110]
    got = [int.from_bytes(key, "big") for key, _ in t.scan(k(100), k(110), end_inclusive=True)]
    assert got == [100, 102, 104, 106, 108, 110]
    assert list(t.scan(k(5000))) == []


def test_prefix_scan(pager):
    t = BTree.create(pager)
    for word in [b"apple", b"apricot", b"banana", b"ap", b"b"]:
        t.insert(word, b"")
    assert [key for key, _ in t.scan_prefix(b"ap")] == [b"ap", b"apple", b"apricot"]


def test_last_key_after_deletes(pager):
    t = BTree.create(pager)
    for i in range(5000):
        t.insert(k(i), b"v" * 30)
    for i in range(3000, 5000):
        t.delete(k(i))
    assert t.last_key() == k(2999)
    assert BTree.create(pager).last_key() is None


def test_mixed_cell_sizes_split_safely(pager):
    # big and tiny cells interleaved: splitting by count alone would overflow
    rng = random.Random(3)
    t = BTree.create(pager)
    for i in range(3000):
        size = MAX_CELL - 10 if rng.random() < 0.2 else 1
        key = os.urandom(8) + (b"K" * (size // 2))
        t.insert(key, b"V" * (size - len(key)) if size > len(key) else b"")
    t.check()


def test_rejects_oversized_rows(pager):
    t = BTree.create(pager)
    with pytest.raises(TreeError, match="row too large"):
        t.insert(b"k", b"x" * (MAX_CELL + 1))


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "p.db")
    p = Pager(path)
    t = BTree.create(p)
    for i in range(5000):
        t.insert(k(i), k(i * 2))
    root = t.root
    p.commit()
    p.close()

    p = Pager(path)
    t = BTree(p, root)
    assert t.get(k(4321)) == k(8642)
    assert t.check()[1] == 5000


def test_destroy_frees_every_page(pager):
    t = BTree.create(pager)
    for i in range(5000):
        t.insert(k(i), b"x" * 40)
    used = pager.page_count
    t.destroy()
    # all of those pages are on the freelist now and get handed out again
    reused = {pager.allocate() for _ in range(used - 1)}
    assert pager.page_count == used
    assert len(reused) == used - 1
