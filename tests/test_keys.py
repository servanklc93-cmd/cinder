import functools
import math
import random

import pytest

from cinder.keys import (
    INT64_MAX, INT64_MIN, decode_row, encode_key, encode_row, rowid_from_index_key, rowid_key,
)


def sql_compare(a, b):
    """SQLite's ordering: NULL < numbers < text, numbers by value, text by bytes."""
    rank = lambda v: 0 if v is None else 1 if isinstance(v, (int, float)) else 2
    ra, rb = rank(a), rank(b)
    if ra != rb:
        return ra - rb
    if a is None:
        return 0
    if isinstance(a, str):
        a, b = a.encode(), b.encode()
    return (a > b) - (a < b)


def random_value(rng):
    kind = rng.randrange(6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.randint(-1000, 1000)
    if kind == 2:
        return rng.choice([INT64_MIN, INT64_MAX, 0, -1, 2 ** 53, 2 ** 53 + 1, -(2 ** 53) - 1, rng.randint(INT64_MIN, INT64_MAX)])
    if kind == 3:
        return rng.choice([0.0, -0.0, 1.5, -1.5, 1e300, -1e300, 1e-300, math.inf, -math.inf, rng.uniform(-1e6, 1e6)])
    alphabet = ["a", "b", "z", "A", "\x00", "é", "日本", "🔥", ""]
    return "".join(rng.choice(alphabet) for _ in range(rng.randrange(5)))


def test_byte_order_matches_sql_order():
    rng = random.Random(11)
    values = [random_value(rng) for _ in range(20000)]
    by_sql = sorted(values, key=functools.cmp_to_key(sql_compare))
    by_bytes = sorted(values, key=encode_key)
    # equal values (1 and 1.0, 0.0 and -0.0) may come out in either order
    assert [sql_compare(a, b) for a, b in zip(by_sql, by_bytes)] == [0] * len(values)


def test_huge_integers_next_to_doubles():
    # 2**53 + 3 rounds up to the double 2**53 + 4, but must still sort below it
    assert encode_key(2 ** 53 + 3) < encode_key(float(2 ** 53 + 4))
    assert encode_key(2 ** 53 + 1) > encode_key(float(2 ** 53))
    assert encode_key(1) == encode_key(1.0)


def test_composite_keys_sort_by_first_column_then_second():
    keys = [encode_key("b", 1), encode_key("a", 2), encode_key("a", 1), encode_key(None, 9)]
    assert sorted(keys) == [encode_key(None, 9), encode_key("a", 1), encode_key("a", 2), encode_key("b", 1)]


def test_text_prefix_sorts_first():
    assert encode_key("ab") < encode_key("ab\x00") < encode_key("abc")
    assert encode_key("ab", 5) < encode_key("abc", 1)  # terminator keeps columns apart


def test_rowid_round_trip():
    for n in [INT64_MIN, -1, 0, 1, INT64_MAX]:
        assert rowid_from_index_key(encode_key("x") + rowid_key(n)) == n
    assert rowid_key(-1) < rowid_key(0) < rowid_key(1)


@pytest.mark.parametrize("row", [
    [],
    [None, 0, -5, INT64_MAX, INT64_MIN],
    [1.5, -0.0, math.inf, "", "héllo 🔥", "a\x00b"],
])
def test_row_round_trip(row):
    assert decode_row(encode_row(row)) == row
