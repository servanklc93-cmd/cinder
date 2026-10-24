"""Name resolution and expression compilation.

Expressions are compiled once into Python closures that take a row (a flat
tuple of every column of every table in the FROM clause) and return a
value, instead of walking the AST again for every row.
"""

from .functions import SCALARS, is_aggregate
from .sql import ast
from .values import (
    SQLError, arith, cmp_op, concat, in_list, like, negate, sql_and, sql_not, sql_or, to_number, truth,
)


class Scope:
    """The tables visible to an expression and where their columns sit in a row."""

    def __init__(self, sources):
        self.sources = []       # (ref, table, offset)
        offset = 0
        for ref, table in sources:
            self.sources.append((ref.lower(), table, offset))
            offset += len(table.columns)
        self.width = offset

    def resolve(self, col):
        """Flat row index of a column reference."""
        name = col.name.lower()
        if col.table:
            for ref, table, offset in self.sources:
                if ref == col.table.lower():
                    i = table.col_index.get(name)
                    if i is None:
                        raise SQLError(f"no such column: {col}")
                    return offset + i
            raise SQLError(f"no such column: {col}")
        hits = [offset + table.col_index[name] for _, table, offset in self.sources if name in table.col_index]
        if not hits:
            raise SQLError(f"no such column: {col.name}")
        if len(hits) > 1:
            raise SQLError(f"ambiguous column name: {col.name}")
        return hits[0]

    def source_of(self, flat_index):
        for i, (_, table, offset) in enumerate(self.sources):
            if offset <= flat_index < offset + len(table.columns):
                return i
        raise IndexError(flat_index)


class Context:
    def __init__(self, scope, params, lookup=None):
        self.scope = scope
        self.params = params
        # post-aggregation: maps whole sub-expressions (group keys, aggregate
        # calls) to slots in the aggregated row
        self.lookup = lookup


def walk(e):
    """Every node of an expression tree. Iterative, so a WHERE with a few
    thousand ANDs doesn't hit Python's recursion limit."""
    stack = [e]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(_children(n)))


def columns_in(e):
    return [n for n in walk(e) if isinstance(n, ast.Column)]


def aggregates_in(e):
    """Aggregate calls in an expression (not looking inside them)."""
    found = []
    stack = [e]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call) and is_aggregate(n):
            for a in n.args:
                if any(isinstance(x, ast.Call) and is_aggregate(x) for x in walk(a)):
                    raise SQLError(f"misuse of aggregate function {n.name}()")
            found.append(n)
            continue
        stack.extend(reversed(_children(n)))
    return found


def _children(n):
    if isinstance(n, ast.Unary):
        return [n.operand]
    if isinstance(n, ast.Binary):
        return [n.left, n.right]
    if isinstance(n, ast.IsNull):
        return [n.operand]
    if isinstance(n, ast.Between):
        return [n.operand, n.low, n.high]
    if isinstance(n, ast.InList):
        return [n.operand, *n.items]
    if isinstance(n, ast.Call):
        return list(n.args)
    if isinstance(n, ast.Case):
        out = [] if n.operand is None else [n.operand]
        for c, r in n.whens:
            out += [c, r]
        if n.default is not None:
            out.append(n.default)
        return out
    return []


def split_and(e):
    return _flatten(e, "AND") if e is not None else []


def _flatten(e, op):
    """a AND b AND c -> [a, b, c], without recursion (the tree is left-deep)."""
    out = []
    stack = [e]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Binary) and n.op == op:
            stack.append(n.right)
            stack.append(n.left)
        else:
            out.append(n)
    return out


_ARITH = {"+", "-", "*", "/", "%"}
_CMP = {"=", "!=", "<", "<=", ">", ">="}


def compile_expr(e, ctx):
    if ctx.lookup is not None:
        slot = ctx.lookup(e)
        if slot is not None:
            return lambda row: row[slot]

    if isinstance(e, ast.Literal):
        v = e.value
        return lambda row: v

    if isinstance(e, ast.Param):
        if e.index >= len(ctx.params):
            raise SQLError(f"missing value for parameter {e.index + 1}")
        v = ctx.params[e.index]
        if isinstance(v, bool):
            v = int(v)
        if v is not None and not isinstance(v, (int, float, str)):
            raise SQLError(f"unsupported parameter type {type(v).__name__}")
        return lambda row: v

    if isinstance(e, ast.Column):
        i = ctx.scope.resolve(e)
        return lambda row: row[i]

    if isinstance(e, ast.Unary):
        f = compile_expr(e.operand, ctx)
        if e.op == "-":
            return lambda row: negate(f(row))
        if e.op == "NOT":
            return lambda row: sql_not(f(row))
        return f  # unary + does nothing in SQLite

    if isinstance(e, ast.Binary) and e.op in ("AND", "OR") and ctx.lookup is None:
        # compile a whole chain at once: one closure looping over the terms
        # instead of one closure per AND nested a thousand deep
        terms = [compile_expr(t, ctx) for t in _flatten(e, e.op)]
        if e.op == "AND":
            def f_and(row):
                result = 1
                for t in terms:
                    v = truth(t(row))
                    if v == 0:
                        return 0
                    if v is None:
                        result = None
                return result
            return f_and

        def f_or(row):
            result = 0
            for t in terms:
                v = truth(t(row))
                if v == 1:
                    return 1
                if v is None:
                    result = None
            return result
        return f_or

    if isinstance(e, ast.Binary):
        fa = compile_expr(e.left, ctx)
        fb = compile_expr(e.right, ctx)
        op = e.op
        if op == "AND":
            return lambda row: sql_and(fa(row), fb(row))
        if op == "OR":
            return lambda row: sql_or(fa(row), fb(row))
        if op in _ARITH:
            return lambda row: arith(op, fa(row), fb(row))
        if op in _CMP:
            return lambda row: cmp_op(op, fa(row), fb(row))
        if op == "||":
            return lambda row: concat(fa(row), fb(row))
        if op == "LIKE":
            return lambda row: like(fa(row), fb(row))
        raise SQLError(f"unknown operator {op}")

    if isinstance(e, ast.IsNull):
        f = compile_expr(e.operand, ctx)
        if e.negated:
            return lambda row: int(f(row) is not None)
        return lambda row: int(f(row) is None)

    if isinstance(e, ast.Between):
        fv, flo, fhi = (compile_expr(x, ctx) for x in (e.operand, e.low, e.high))

        def between(row):
            v = fv(row)
            r = sql_and(cmp_op(">=", v, flo(row)), cmp_op("<=", v, fhi(row)))
            return sql_not(r) if e.negated else r
        return between

    if isinstance(e, ast.InList):
        fv = compile_expr(e.operand, ctx)
        items = [compile_expr(x, ctx) for x in e.items]

        def inlist(row):
            r = in_list(fv(row), [f(row) for f in items])
            return sql_not(r) if e.negated else r
        return inlist

    if isinstance(e, ast.Case):
        fop = compile_expr(e.operand, ctx) if e.operand is not None else None
        whens = [(compile_expr(c, ctx), compile_expr(r, ctx)) for c, r in e.whens]
        fdef = compile_expr(e.default, ctx) if e.default is not None else (lambda row: None)

        def case(row):
            base = fop(row) if fop else None
            for cond, result in whens:
                hit = cmp_op("=", base, cond(row)) if fop else cond(row)
                if truth(hit) == 1:
                    return result(row)
            return fdef(row)
        return case

    if isinstance(e, ast.Call):
        if is_aggregate(e):
            raise SQLError(f"misuse of aggregate function {e.name}()")
        entry = SCALARS.get(e.name)
        if entry is None:
            raise SQLError(f"no such function: {e.name}")
        fn, lo, hi = entry
        if e.star or not lo <= len(e.args) <= hi:
            raise SQLError(f"wrong number of arguments to function {e.name}()")
        args = [compile_expr(a, ctx) for a in e.args]
        if len(args) == 1:
            a0 = args[0]
            return lambda row: fn(a0(row))
        return lambda row: fn(*[a(row) for a in args])

    raise SQLError(f"can't evaluate {type(e).__name__}")


def constant(e, params):
    """Evaluate an expression that may not reference any columns."""
    if columns_in(e):
        raise SQLError(f"no such column: {columns_in(e)[0]}")
    return compile_expr(e, Context(Scope([]), params))(())


def as_int(v, what):
    v = to_number(v)
    if v is None:
        raise SQLError(f"{what} must be an integer")
    return int(v)


def unparse(e):
    """SQL text for an expression, used for result column names and EXPLAIN."""
    if isinstance(e, ast.Literal):
        v = e.value
        if v is None:
            return "NULL"
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        return repr(v)
    if isinstance(e, ast.Param):
        return "?"
    if isinstance(e, ast.Column):
        return str(e)
    if isinstance(e, ast.Unary):
        return f"NOT {unparse(e.operand)}" if e.op == "NOT" else f"{e.op}{unparse(e.operand)}"
    if isinstance(e, ast.Binary) and e.op in ("AND", "OR"):
        return f" {e.op} ".join(unparse(t) for t in _flatten(e, e.op))
    if isinstance(e, ast.Binary):
        return f"{unparse(e.left)} {e.op} {unparse(e.right)}"
    if isinstance(e, ast.IsNull):
        return f"{unparse(e.operand)} IS {'NOT ' if e.negated else ''}NULL"
    if isinstance(e, ast.Between):
        return f"{unparse(e.operand)} {'NOT ' if e.negated else ''}BETWEEN {unparse(e.low)} AND {unparse(e.high)}"
    if isinstance(e, ast.InList):
        return f"{unparse(e.operand)} {'NOT ' if e.negated else ''}IN ({', '.join(map(unparse, e.items))})"
    if isinstance(e, ast.Call):
        inner = "*" if e.star else ("DISTINCT " if e.distinct else "") + ", ".join(map(unparse, e.args))
        return f"{e.name.lower()}({inner})"
    if isinstance(e, ast.Case):
        parts = ["CASE"] + ([unparse(e.operand)] if e.operand is not None else [])
        for c, r in e.whens:
            parts += ["WHEN", unparse(c), "THEN", unparse(r)]
        if e.default is not None:
            parts += ["ELSE", unparse(e.default)]
        return " ".join(parts + ["END"])
    return "?"
