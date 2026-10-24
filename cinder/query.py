"""Query planning and execution.

A SELECT becomes a pipeline of generators (the Volcano model):

    access path per table -> nested loop joins -> filter -> aggregate
        -> having -> project -> distinct -> sort -> offset / limit

Access paths, best first:

    rowid lookup      WHERE id = ?            (id is INTEGER PRIMARY KEY)
    index lookup      WHERE email = ?         (unique indexes preferred)
    rowid range       WHERE id BETWEEN ? AND ?
    index range       WHERE age > ?
    full scan

The conditions used to pick an access path are still applied as a filter
afterwards. A seek only has to return a superset of the matching rows,
which keeps type edge cases (an INTEGER column compared with 2.5, say)
from ever producing wrong results.
"""

import math
from dataclasses import dataclass, field

from .btree import BTree
from .expr import (
    Context, Scope, aggregates_in, as_int, columns_in, compile_expr, constant, split_and, unparse,
)
from .functions import AGGREGATES, CountStar, Distinct
from .keys import (
    INT64_MAX, INT64_MIN, decode_row, encode_value, rowid_from_index_key, rowid_from_key, rowid_key,
)
from .sql import ast
from .values import SQLError, sort_key, truth

_AFTER_ANY_ROWID = b"\xff" * 9
_FIRST_NON_NULL = b"\x02"
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


# --- access paths -----------------------------------------------------------------


class Access:
    """Produces (rowid, values) for one table, possibly depending on the outer row."""

    def __init__(self, table, pager):
        self.table = table
        self.pager = pager
        self.tree = BTree(pager, table.root)

    def fetch(self, rowid):
        raw = self.tree.get(rowid_key(rowid))
        return None if raw is None else (rowid, tuple(decode_row(raw)))


class FullScan(Access):
    ordered_by_rowid = True

    def rows(self, outer):
        for k, raw in self.tree.scan():
            yield rowid_from_key(k), tuple(decode_row(raw))

    def describe(self):
        return f"SCAN {self.table.name}"


def _rowid_value(v):
    """A seek value as a rowid, or None if no rowid can equal it."""
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return v if INT64_MIN <= v <= INT64_MAX else None
    if isinstance(v, float) and v.is_integer() and INT64_MIN <= v <= INT64_MAX:
        return int(v)
    return None


class RowidLookup(Access):
    ordered_by_rowid = True

    def __init__(self, table, pager, value, label):
        super().__init__(table, pager)
        self.value = value
        self.label = label

    def rows(self, outer):
        rowid = _rowid_value(self.value(outer))
        if rowid is not None:
            hit = self.fetch(rowid)
            if hit:
                yield hit

    def describe(self):
        return f"SEARCH {self.table.name} USING ROWID ({self.label})"


def _numeric_bound(v, lower):
    # conservative integer bound for a rowid range; None means unbounded
    if isinstance(v, bool):
        v = int(v)
    if not isinstance(v, (int, float)) or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, float):
        if math.isinf(v):
            return None if (v < 0) == lower else (INT64_MAX if lower else INT64_MIN)
        v = math.floor(v) if lower else math.ceil(v)
    return max(INT64_MIN, min(INT64_MAX, v))


class RowidRange(Access):
    ordered_by_rowid = True

    def __init__(self, table, pager, lo, hi, label):
        super().__init__(table, pager)
        self.lo, self.hi, self.label = lo, hi, label

    def rows(self, outer):
        start = end = None
        if self.lo:
            v = self.lo(outer)
            if v is None:
                return
            b = _numeric_bound(v, lower=True)
            start = rowid_key(b) if b is not None else None
        if self.hi:
            v = self.hi(outer)
            if v is None:
                return
            b = _numeric_bound(v, lower=False)
            end = rowid_key(b) if b is not None else None
        for k, raw in self.tree.scan(start, end, end_inclusive=True):
            yield rowid_from_key(k), tuple(decode_row(raw))

    def describe(self):
        return f"SEARCH {self.table.name} USING ROWID RANGE ({self.label})"


class IndexAccess(Access):
    ordered_by_rowid = False

    def __init__(self, table, pager, index):
        super().__init__(table, pager)
        self.index = index
        self.itree = BTree(pager, index.root)

    def _rows_for(self, keys):
        for k, _ in keys:
            yield self.fetch(rowid_from_index_key(k))


class IndexLookup(IndexAccess):
    def __init__(self, table, pager, index, value, label):
        super().__init__(table, pager, index)
        self.value, self.label = value, label

    def rows(self, outer):
        v = self.value(outer)
        if v is None:
            return
        yield from self._rows_for(self.itree.scan_prefix(encode_value(v)))

    def describe(self):
        return f"SEARCH {self.table.name} USING INDEX {self.index.name} ({self.label})"


class IndexRange(IndexAccess):
    def __init__(self, table, pager, index, lo, lo_incl, hi, hi_incl, label):
        super().__init__(table, pager, index)
        self.lo, self.lo_incl, self.hi, self.hi_incl, self.label = lo, lo_incl, hi, hi_incl, label

    def rows(self, outer):
        start, end = _FIRST_NON_NULL, None
        if self.lo:
            v = self.lo(outer)
            if v is None:
                return
            start = encode_value(v) + (b"" if self.lo_incl else _AFTER_ANY_ROWID)
        if self.hi:
            v = self.hi(outer)
            if v is None:
                return
            end = encode_value(v) + (_AFTER_ANY_ROWID if self.hi_incl else b"")
        yield from self._rows_for(self.itree.scan(start, end, end_inclusive=False))

    def describe(self):
        return f"SEARCH {self.table.name} USING INDEX {self.index.name} ({self.label})"


class IndexOrderScan(IndexAccess):
    """Every row, in index order: lets ORDER BY <indexed column> skip the sort."""

    def rows(self, outer):
        yield from self._rows_for(self.itree.scan())

    def describe(self):
        return f"SCAN {self.table.name} USING INDEX {self.index.name}"


# --- choosing an access path ------------------------------------------------------------


@dataclass
class Sarg:
    """A search argument: <column of this table> <op> <expression we can evaluate first>."""
    col_idx: int
    op: str
    expr: ast.Expr


def _sargs(conjuncts, scope, pos):
    _, table, offset = scope.sources[pos]
    found = []

    def bound(e):
        # computable before this table's row exists: only earlier tables' columns
        return all(scope.resolve(c) < offset for c in columns_in(e)) and not aggregates_in(e)

    def local_col(e):
        if isinstance(e, ast.Column):
            i = scope.resolve(e)
            if offset <= i < offset + len(table.columns):
                return i - offset
        return None

    for c in conjuncts:
        if isinstance(c, ast.Binary) and c.op in _FLIP:
            left, right = local_col(c.left), local_col(c.right)
            if left is not None and bound(c.right):
                found.append(Sarg(left, c.op, c.right))
            elif right is not None and bound(c.left):
                found.append(Sarg(right, _FLIP[c.op], c.left))
        elif isinstance(c, ast.Between) and not c.negated:
            col = local_col(c.operand)
            if col is not None and bound(c.low) and bound(c.high):
                found += [Sarg(col, ">=", c.low), Sarg(col, "<=", c.high)]
    return found


def choose_access(table, pager, conjuncts, scope, pos, ctx):
    sargs = _sargs(conjuncts, scope, pos)
    fn = lambda e: compile_expr(e, ctx)
    name = lambda s: f"{table.columns[s.col_idx].name}{s.op}{unparse(s.expr)}"

    eq = [s for s in sargs if s.op == "="]
    for s in eq:
        if s.col_idx == table.rowid_col:
            return RowidLookup(table, pager, fn(s.expr), name(s))
    for unique in (True, False):
        for s in eq:
            ix = table.index_on(s.col_idx)
            if ix and ix.unique == unique:
                return IndexLookup(table, pager, ix, fn(s.expr), name(s))

    def range_on(col_idx):
        lo = hi = None
        for s in sargs:
            if s.col_idx == col_idx and s.op in (">", ">=") and lo is None:
                lo = s
            if s.col_idx == col_idx and s.op in ("<", "<=") and hi is None:
                hi = s
        return lo, hi

    if table.rowid_col is not None:
        lo, hi = range_on(table.rowid_col)
        if lo or hi:
            label = " AND ".join(name(s) for s in (lo, hi) if s)
            return RowidRange(table, pager, lo and fn(lo.expr), hi and fn(hi.expr), label)

    for s in sargs:
        ix = table.index_on(s.col_idx)
        if ix and s.op != "=":
            lo, hi = range_on(s.col_idx)
            label = " AND ".join(name(x) for x in (lo, hi) if x)
            return IndexRange(table, pager, ix,
                              lo and fn(lo.expr), lo is not None and lo.op == ">=",
                              hi and fn(hi.expr), hi is not None and hi.op == "<=", label)
    return FullScan(table, pager)


# --- SELECT -------------------------------------------------------------------------------


@dataclass
class Step:
    access: Access
    filter: object               # compiled predicate or None
    filter_text: list
    left: bool = False
    width: int = 0


@dataclass
class SelectPlan:
    columns: list
    run: object                  # () -> iterator of tuples
    explain: list = field(default_factory=list)


def _passes(pred, row):
    return pred is None or truth(pred(row)) == 1


def _and_all(preds):
    preds = [p for p in preds if p is not None]
    if not preds:
        return None
    if len(preds) == 1:
        return preds[0]
    return lambda row: all(truth(p(row)) == 1 for p in preds)


def plan_select(sel, catalog, pager, params):

    # --- sources
    sources = []
    if sel.table is not None:
        sources.append((sel.table.ref, catalog.get(sel.table.name)))
        for j in sel.joins:
            sources.append((j.table.ref, catalog.get(j.table.name)))
    refs = [r.lower() for r, _ in sources]
    if len(set(refs)) != len(refs):
        raise SQLError("table names in FROM must be unique; use an alias")
    scope = Scope(sources)
    ctx = Context(scope, params)

    # --- expand the select list
    items = []                   # (expr, name)
    for item in sel.items:
        if item.expr is not None:
            name = item.alias or (item.expr.name if isinstance(item.expr, ast.Column) else unparse(item.expr))
            items.append((item.expr, name))
            continue
        if not sources:
            raise SQLError("no tables specified")
        matched = False
        for ref, table, _ in scope.sources:
            if item.star_table is None or item.star_table.lower() == ref:
                matched = True
                for c in table.columns:
                    items.append((ast.Column(c.name, ref), c.name))
        if not matched:
            raise SQLError(f"no such table: {item.star_table}")

    def by_position(e, what):
        # ORDER BY 2 / GROUP BY 1 refer to select items
        if isinstance(e, ast.Literal) and isinstance(e.value, int) and not isinstance(e.value, bool):
            if not 1 <= e.value <= len(items):
                raise SQLError(f"{what} term out of range - should be between 1 and {len(items)}")
            return items[e.value - 1][0]
        return e

    aliases = {name.lower(): expr for (expr, name), item in zip(items, sel.items) if item.alias} \
        if all(i.expr is not None for i in sel.items) else {}

    def by_alias(e):
        if isinstance(e, ast.Column) and e.table is None and e.name.lower() in aliases:
            try:
                scope.resolve(e)
            except SQLError:
                return aliases[e.name.lower()]
        return e

    group_by = [by_alias(by_position(g, "GROUP BY")) for g in sel.group_by]
    order_by = [(by_alias(by_position(o.expr, "ORDER BY")), o.desc) for o in sel.order_by]

    for e in [sel.where] + [j.on for j in sel.joins]:
        if e is not None and aggregates_in(e):
            raise SQLError("aggregate functions are not allowed in WHERE or ON")

    # --- distribute WHERE / ON conditions over the joins
    left_positions = {i + 1 for i, j in enumerate(sel.joins) if j.left}
    per_pos = [[] for _ in sources] or [[]]
    top = []

    def max_source(e):
        cols = columns_in(e)
        return max((scope.source_of(scope.resolve(c)) for c in cols), default=0)

    for c in split_and(sel.where):
        p = max_source(c)
        if not sources or p in left_positions:
            top.append(c)          # can't move a WHERE into the inner side of a LEFT JOIN
        else:
            per_pos[p].append(c)
    for i, j in enumerate(sel.joins):
        pos = i + 1
        for c in split_and(j.on):
            p = max_source(c)
            if j.left or p == pos:
                per_pos[pos].append(c)
            else:
                per_pos[p].append(c)  # an inner join's ON is just more WHERE

    steps = []
    for pos, (ref, table) in enumerate(sources):
        conj = per_pos[pos]
        access = choose_access(table, pager, conj, scope, pos, ctx)
        pred = _and_all([compile_expr(c, ctx) for c in conj])
        steps.append(Step(access, pred, [unparse(c) for c in conj],
                          left=pos in left_positions, width=len(table.columns)))
    top_pred = _and_all([compile_expr(c, ctx) for c in top])

    # --- ORDER BY a rowid / indexed column can come straight from the access path
    is_agg = bool(group_by) or any(aggregates_in(e) for e, _ in items) or \
        (sel.having is not None) or any(aggregates_in(e) for e, _ in order_by)
    ordered = False
    if len(steps) == 1 and not is_agg and not sel.distinct and len(order_by) == 1 and not order_by[0][1]:
        e = order_by[0][0]
        if isinstance(e, ast.Column):
            col = scope.resolve(e)
            step = steps[0]
            acc = step.access
            if col == acc.table.rowid_col and getattr(acc, "ordered_by_rowid", False):
                ordered = True
            elif isinstance(acc, (IndexLookup, IndexRange, IndexOrderScan)) and acc.index.col_idx == col:
                ordered = True
            elif isinstance(acc, FullScan) and acc.table.index_on(col):
                step.access = IndexOrderScan(acc.table, pager, acc.table.index_on(col))
                ordered = True

    def source_rows():
        if not steps:
            yield ()
            return
        yield from _join(steps, 0, ())

    def filtered():
        for row in source_rows():
            if _passes(top_pred, row):
                yield row

    scans = []
    for depth, st in enumerate(steps):
        line = ("LEFT JOIN " if st.left else "") + st.access.describe()
        if depth:
            line += "  -- for each row above"
        scans.append((depth, line + (f"  [filter: {' AND '.join(st.filter_text)}]" if st.filter_text else "")))
    ops = []                     # operators in execution order, printed top-down reversed
    if top:
        ops.append("FILTER " + " AND ".join(unparse(c) for c in top))

    # --- aggregation
    if is_agg:
        rows_fn, out_ctx = _aggregate(filtered, scope, ctx, items, group_by, sel.having, order_by, params)
        ops.append("AGGREGATE" + (" GROUP BY " + ", ".join(map(unparse, group_by)) if group_by else ""))
        if sel.having is not None:
            ops.append("HAVING " + unparse(sel.having))
    else:
        rows_fn, out_ctx = filtered, ctx

    outputs = [compile_expr(e, out_ctx) for e, _ in items]
    sort_keys = []
    for e, desc in order_by:
        hit = next((i for i, (ie, _) in enumerate(items) if ie == e), None)
        if hit is not None:
            sort_keys.append((("out", hit), desc))
        else:
            sort_keys.append((("expr", compile_expr(e, out_ctx)), desc))

    limit = offset = None
    if sel.limit is not None:
        limit = as_int(constant(sel.limit, params), "LIMIT")
        if limit < 0:
            limit = None
    if sel.offset is not None:
        offset = max(as_int(constant(sel.offset, params), "OFFSET"), 0)

    def run():
        produced = ((tuple(f(r) for f in outputs), r) for r in rows_fn())
        if sel.distinct:
            produced = _distinct(produced)
        if order_by and not ordered:
            produced = _sort(produced, sort_keys)
        out = (o for o, _ in produced)
        if offset:
            out = _skip(out, offset)
        if limit is not None:
            out = _take(out, limit)
        return out

    if sel.distinct:
        ops.append("DISTINCT")
    if order_by and not ordered:
        ops.append("SORT " + ", ".join(unparse(e) + (" DESC" if d else "") for e, d in order_by))
    elif order_by:
        ops.append("ORDER BY uses the scan order (no sort)")
    if limit is not None or offset:
        ops.append(f"LIMIT {limit if limit is not None else 'ALL'}" + (f" OFFSET {offset}" if offset else ""))
    explain = [(i, op) for i, op in enumerate(reversed(ops))]
    explain += [(len(ops) + d, line) for d, line in scans] or [(len(ops), "CONSTANT ROW")]

    return SelectPlan([name for _, name in items], run, explain)


def _join(steps, i, outer):
    step = steps[i]
    last = i == len(steps) - 1
    matched = False
    for _, values in step.access.rows(outer):
        row = outer + values
        if not _passes(step.filter, row):
            continue
        matched = True
        if last:
            yield row
        else:
            yield from _join(steps, i + 1, row)
    if step.left and not matched:
        row = outer + (None,) * step.width
        if last:
            yield row
        else:
            yield from _join(steps, i + 1, row)


def _aggregate(source, scope, ctx, items, group_by, having, order_by, params):
    calls = []
    for e in [e for e, _ in items] + ([having] if having is not None else []) + [e for e, _ in order_by]:
        for call in aggregates_in(e):
            if call not in calls:
                calls.append(call)

    key_fns = [compile_expr(g, ctx) for g in group_by]
    arg_fns = []
    for call in calls:
        if call.star:
            if call.name != "COUNT":
                raise SQLError(f"wrong number of arguments to function {call.name}()")
            arg_fns.append(None)
            continue
        if call.name == "GROUP_CONCAT":
            if not 1 <= len(call.args) <= 2:
                raise SQLError("wrong number of arguments to function GROUP_CONCAT()")
        elif len(call.args) != 1:
            raise SQLError(f"wrong number of arguments to function {call.name}()")
        arg_fns.append(compile_expr(call.args[0], ctx))

    def new_states():
        states = []
        for call in calls:
            if call.star:
                agg = CountStar()
            elif call.name == "GROUP_CONCAT" and len(call.args) == 2:
                agg = AGGREGATES[call.name](constant(call.args[1], params))
            else:
                agg = AGGREGATES[call.name]()
            states.append(Distinct(agg) if call.distinct else agg)
        return states

    n_keys, n_aggs = len(group_by), len(calls)

    def lookup(e):
        # group keys and aggregate results live at the front of the row,
        # the last source row of the group (for bare columns) after them
        for i, g in enumerate(group_by):
            if e == g:
                return i
        if isinstance(e, ast.Call) and e in calls:
            return n_keys + calls.index(e)
        return None

    class Shifted(Scope):
        def __init__(self):
            self.sources, self.width = scope.sources, scope.width

        def resolve(self, col):
            return n_keys + n_aggs + scope.resolve(col)

    out_ctx = Context(Shifted(), params, lookup)
    having_fn = compile_expr(having, out_ctx) if having is not None else None
    empty_row = (None,) * scope.width

    def rows():
        groups = {}
        for row in source():
            key = tuple(f(row) for f in key_fns)
            entry = groups.get(key)
            if entry is None:
                entry = groups[key] = [new_states(), row]
            entry[1] = row
            for state, f in zip(entry[0], arg_fns):
                state.step(None if f is None else f(row))
        if not groups and not group_by:
            groups[()] = [new_states(), empty_row]
        # SQLite hands back groups in key order, so do the same
        for key in sorted(groups, key=lambda k: tuple(sort_key(v) for v in k)):
            states, rep = groups[key]
            out = key + tuple(s.result() for s in states) + rep
            if having_fn is None or truth(having_fn(out)) == 1:
                yield out

    return rows, out_ctx


def _distinct(produced):
    seen = set()
    for out, row in produced:
        key = tuple((type(v) is str, v) for v in out)
        if key not in seen:
            seen.add(key)
            yield out, row


def _sort(produced, sort_keys):
    rows = list(produced)

    def value(entry, how):
        kind, x = how
        return entry[0][x] if kind == "out" else x(entry[1])

    # stable sorts from the last key to the first give a multi-key order
    # where each key can be ascending or descending
    for how, desc in reversed(sort_keys):
        rows.sort(key=lambda entry: sort_key(value(entry, how)), reverse=desc)
    return iter(rows)


def _skip(it, n):
    for i, x in enumerate(it):
        if i >= n:
            yield x


def _take(it, n):
    if n <= 0:
        return
    for i, x in enumerate(it):
        yield x
        if i + 1 >= n:
            return


def plan_rows_for_write(table, where, catalog, pager, params):
    """(rowid, values) for every row of one table matching WHERE (UPDATE/DELETE)."""
    scope = Scope([(table.name, table)])
    ctx = Context(scope, params)
    conjuncts = split_and(where)
    for c in conjuncts:
        if aggregates_in(c):
            raise SQLError("aggregate functions are not allowed in WHERE")
    access = choose_access(table, pager, conjuncts, scope, 0, ctx)
    pred = _and_all([compile_expr(c, ctx) for c in conjuncts])
    return access, pred, ctx
