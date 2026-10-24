"""Scalar functions and aggregates."""

import math

from .values import (
    SQLError, compare, rank, to_number, to_text, type_name, _int_or_real,
)


def _ascii_upper(s):
    return "".join(c.upper() if c.isascii() else c for c in s)


def _ascii_lower(s):
    return "".join(c.lower() if c.isascii() else c for c in s)


def _null_in(args):
    return any(a is None for a in args)


def f_length(v):
    return None if v is None else len(to_text(v))


def f_upper(v):
    return None if v is None else _ascii_upper(to_text(v))


def f_lower(v):
    return None if v is None else _ascii_lower(to_text(v))


def f_abs(v):
    if v is None:
        return None
    v = to_number(v)
    return _int_or_real(abs(v)) if isinstance(v, int) else abs(v)


def f_round(v, digits=0):
    if v is None or digits is None:
        return None
    x = float(to_number(v))
    n = int(to_number(digits))
    if math.isinf(x) or math.isnan(x):
        return x
    # SQLite rounds half away from zero; Python's round() is banker's rounding
    scale = 10.0 ** max(n, 0)
    r = math.floor(abs(x) * scale + 0.5) / scale
    return math.copysign(r, x) if r != 0 else 0.0


def f_substr(s, start, length=None):
    if s is None or start is None:
        return None
    s = to_text(s)
    start = int(to_number(start))
    if length is not None:
        length = int(to_number(length))
    n = len(s)
    # SQLite's 1-based indexing, including its handling of 0 and negatives
    if start > 0:
        begin = start - 1
    elif start < 0:
        begin = n + start
    else:
        begin = -1
    if length is None:
        end = n
    elif length >= 0:
        end = begin + length
    else:
        end, begin = begin, begin + length
    return s[max(begin, 0):max(end, 0)]


def f_instr(s, sub):
    if s is None or sub is None:
        return None
    return to_text(s).find(to_text(sub)) + 1


def f_replace(s, old, new):
    if _null_in((s, old, new)):
        return None
    s, old, new = to_text(s), to_text(old), to_text(new)
    return s if old == "" else s.replace(old, new)


def f_trim(s, chars=" "):
    return None if s is None or chars is None else to_text(s).strip(to_text(chars))


def f_ltrim(s, chars=" "):
    return None if s is None or chars is None else to_text(s).lstrip(to_text(chars))


def f_rtrim(s, chars=" "):
    return None if s is None or chars is None else to_text(s).rstrip(to_text(chars))


def f_coalesce(*args):
    for a in args:
        if a is not None:
            return a
    return None


def f_nullif(a, b):
    return None if a is not None and b is not None and compare(a, b) == 0 else a


def f_scalar_min(*args):
    if _null_in(args):
        return None
    best = args[0]
    for a in args[1:]:
        if compare(a, best) < 0:
            best = a
    return best


def f_scalar_max(*args):
    if _null_in(args):
        return None
    best = args[0]
    for a in args[1:]:
        if compare(a, best) > 0:
            best = a
    return best


# name -> (function, min args, max args)
SCALARS = {
    "LENGTH": (f_length, 1, 1),
    "UPPER": (f_upper, 1, 1),
    "LOWER": (f_lower, 1, 1),
    "ABS": (f_abs, 1, 1),
    "ROUND": (f_round, 1, 2),
    "SUBSTR": (f_substr, 2, 3),
    "SUBSTRING": (f_substr, 2, 3),
    "INSTR": (f_instr, 2, 2),
    "REPLACE": (f_replace, 3, 3),
    "TRIM": (f_trim, 1, 2),
    "LTRIM": (f_ltrim, 1, 2),
    "RTRIM": (f_rtrim, 1, 2),
    "COALESCE": (f_coalesce, 2, 99),
    "IFNULL": (f_coalesce, 2, 2),
    "NULLIF": (f_nullif, 2, 2),
    "TYPEOF": (type_name, 1, 1),
    # MIN/MAX with two or more arguments are scalar, with one they aggregate
    "MIN": (f_scalar_min, 2, 99),
    "MAX": (f_scalar_max, 2, 99),
}


# --- aggregates -----------------------------------------------------------------


class Count:
    def __init__(self):
        self.n = 0

    def step(self, v):
        if v is not None:
            self.n += 1

    def result(self):
        return self.n


class CountStar(Count):
    def step(self, v):
        self.n += 1


class Sum:
    def __init__(self):
        self.total = 0
        self.any = False
        self.real = False

    def step(self, v):
        if v is None:
            return
        v = to_number(v)
        self.any = True
        if isinstance(v, float):
            self.real = True
        self.total += v

    def result(self):
        if not self.any:
            return None
        if self.real:
            return float(self.total)
        if not -(2 ** 63) <= self.total < 2 ** 63:
            raise SQLError("integer overflow")
        return self.total


class Total(Sum):
    def result(self):
        return float(self.total)


class Avg(Sum):
    def __init__(self):
        super().__init__()
        self.n = 0

    def step(self, v):
        if v is not None:
            self.n += 1
        super().step(v)

    def result(self):
        return None if not self.n else self.total / self.n


class Min:
    def __init__(self):
        self.best = None

    def step(self, v):
        if v is not None and (self.best is None or compare(v, self.best) < 0):
            self.best = v

    def result(self):
        return self.best


class Max(Min):
    def step(self, v):
        if v is not None and (self.best is None or compare(v, self.best) > 0):
            self.best = v


class GroupConcat:
    def __init__(self, sep=","):
        self.parts = []
        self.sep = sep

    def step(self, v):
        if v is not None:
            self.parts.append(to_text(v))

    def result(self):
        return self.sep.join(self.parts) if self.parts else None


class Distinct:
    """Wraps an aggregate so it only sees each distinct value once."""

    def __init__(self, inner):
        self.inner = inner
        self.seen = set()

    def step(self, v):
        key = (rank(v), v)
        if key not in self.seen:
            self.seen.add(key)
            self.inner.step(v)

    def result(self):
        return self.inner.result()


AGGREGATES = {
    "COUNT": Count,
    "SUM": Sum,
    "TOTAL": Total,
    "AVG": Avg,
    "MIN": Min,
    "MAX": Max,
    "GROUP_CONCAT": GroupConcat,
}


def is_aggregate(call):
    if call.name in ("MIN", "MAX"):
        return len(call.args) == 1
    return call.name in AGGREGATES
