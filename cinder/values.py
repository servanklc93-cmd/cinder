"""SQL value semantics, matching SQLite where it matters.

Values are None (NULL), int, float or str. Comparisons and boolean operators
return 1, 0 or None, like SQLite. Every rule here was checked against the
sqlite3 module; the differential tests keep it that way.
"""

import math
import re

from .keys import INT64_MAX, INT64_MIN


class SQLError(Exception):
    pass


def rank(v):
    # SQLite's sort order between types: NULL < numbers < text
    if v is None:
        return 0
    if isinstance(v, str):
        return 2
    return 1


def sort_key(v):
    # str compares by code point, which is the same order as UTF-8 bytes,
    # so this matches SQLite's default BINARY collation
    return (rank(v), 0 if v is None else v)


def compare(a, b):
    """-1 / 0 / 1 for non-NULL values, using the cross-type order."""
    ra, rb = rank(a), rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    return (a > b) - (a < b)


def truth(v):
    """SQL truthiness: None stays unknown, numbers are true unless zero."""
    if v is None:
        return None
    if isinstance(v, str):
        v = to_number(v)
    return 1 if v != 0 else 0


# --- conversions --------------------------------------------------------------

_NUM_PREFIX = re.compile(r"\s*[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)")


def to_number(v):
    """Text used as a number: the longest numeric prefix, or 0 (SQLite's rule)."""
    if v is None or isinstance(v, (int, float)):
        return v
    m = _NUM_PREFIX.match(v)
    if not m:
        return 0
    text = m.group().strip()
    try:
        return int(text)
    except ValueError:
        f = float(text)
        return int(f) if f.is_integer() and "." not in text and "e" not in text.lower() else f


def real_to_text(x):
    if math.isinf(x):
        return "Inf" if x > 0 else "-Inf"
    if x == 0:
        return "0.0"
    s = "%.15g" % x
    if "e" in s:
        mantissa, exp = s.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{exp}"
    return s if "." in s else s + ".0"


def to_text(v):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, float):
        return real_to_text(v)
    return str(v)


def _int_or_real(n):
    # integer overflow turns into a real, as in SQLite
    return n if INT64_MIN <= n <= INT64_MAX else float(n)


# --- operators ----------------------------------------------------------------


def arith(op, a, b):
    if a is None or b is None:
        return None
    a, b = to_number(a), to_number(b)
    if op == "+":
        r = a + b
    elif op == "-":
        r = a - b
    elif op == "*":
        r = a * b
    elif op == "/":
        if b == 0:
            return None
        if isinstance(a, int) and isinstance(b, int):
            q = abs(a) // abs(b)
            r = q if (a < 0) == (b < 0) else -q
        else:
            r = a / b
    elif op == "%":
        # SQLite casts both sides to integers first, and keeps a real result real
        ia, ib = int(a), int(b)
        if ib == 0:
            return None
        rem = abs(ia) % abs(ib)
        r = rem if ia >= 0 else -rem
        if isinstance(a, float) or isinstance(b, float):
            return float(r)
    else:
        raise SQLError(f"unknown operator {op}")
    if isinstance(r, float) and r != r:
        return None  # inf - inf and friends: SQLite has no NaN, it gives NULL
    return _int_or_real(r) if isinstance(r, int) else r


def negate(v):
    if v is None:
        return None
    v = to_number(v)
    return _int_or_real(-v) if isinstance(v, int) else -v


def cmp_op(op, a, b):
    if a is None or b is None:
        return None
    c = compare(a, b)
    if op == "=":
        return int(c == 0)
    if op == "!=":
        return int(c != 0)
    if op == "<":
        return int(c < 0)
    if op == "<=":
        return int(c <= 0)
    if op == ">":
        return int(c > 0)
    return int(c >= 0)


def sql_and(a, b):
    ta, tb = truth(a), truth(b)
    if ta == 0 or tb == 0:
        return 0
    if ta is None or tb is None:
        return None
    return 1


def sql_or(a, b):
    ta, tb = truth(a), truth(b)
    if ta == 1 or tb == 1:
        return 1
    if ta is None or tb is None:
        return None
    return 0


def sql_not(a):
    t = truth(a)
    return None if t is None else 1 - t


def concat(a, b):
    if a is None or b is None:
        return None
    return to_text(a) + to_text(b)


_like_cache = {}


def like(value, pattern):
    if value is None or pattern is None:
        return None
    value, pattern = to_text(value), to_text(pattern)
    rx = _like_cache.get(pattern)
    if rx is None:
        parts = []
        for ch in pattern:
            if ch == "%":
                parts.append(".*")
            elif ch == "_":
                parts.append(".")
            else:
                parts.append(re.escape(ch))
        # case-insensitive for ASCII letters only, like SQLite
        rx = re.compile("".join(parts), re.DOTALL | re.IGNORECASE | re.ASCII)
        if len(_like_cache) > 256:
            _like_cache.clear()
        _like_cache[pattern] = rx
    return int(rx.fullmatch(value) is not None)


def in_list(value, items):
    if value is None:
        return None
    saw_null = False
    for item in items:
        if item is None:
            saw_null = True
        elif compare(value, item) == 0:
            return 1
    return None if saw_null else 0


# --- storage coercion (STRICT tables) -----------------------------------------


def coerce(value, type_, where):
    """Convert a value for storage in a column, or raise like SQLite does."""
    if value is None:
        return None
    if type_ == "INTEGER":
        if isinstance(value, int):
            if not INT64_MIN <= value <= INT64_MAX:
                raise SQLError(f"integer overflow storing into {where}")
            return value
        if isinstance(value, float):
            if value.is_integer() and INT64_MIN <= value <= INT64_MAX:
                return int(value)
            raise SQLError(f"cannot store REAL value in INTEGER column {where}")
        n = _exact_number(value)
        if isinstance(n, int) or (isinstance(n, float) and n.is_integer()):
            return coerce(n, type_, where)
        raise SQLError(f"cannot store TEXT value in INTEGER column {where}")
    if type_ == "REAL":
        if isinstance(value, (int, float)):
            return float(value)
        n = _exact_number(value)
        if n is None:
            raise SQLError(f"cannot store TEXT value in REAL column {where}")
        return float(n)
    return to_text(value)


def _exact_number(text):
    """The number a string spells, if the whole string is one (no prefix rule)."""
    t = text.strip()
    try:
        return int(t)
    except ValueError:
        pass
    try:
        f = float(t)
    except ValueError:
        return None
    return None if math.isnan(f) else f


def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, str):
        return "text"
    return "real" if isinstance(v, float) else "integer"
