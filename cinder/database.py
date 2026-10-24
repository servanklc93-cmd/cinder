"""The public API: open a file, run SQL.

    db = cinder.connect("app.db")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("INSERT INTO users (name) VALUES (?)", ["ada"])
    for row in db.execute("SELECT * FROM users WHERE name = ?", ["ada"]):
        print(row)

Every statement outside BEGIN ... COMMIT commits on its own. Inside a
transaction, a statement that fails is undone on its own and the rest of the
transaction stays, like SQLite.
"""

import os
from collections import OrderedDict

from .btree import BTree, DuplicateKey, TreeError
from .catalog import Catalog
from .expr import compile_expr, constant
from .keys import decode_row, encode_row, encode_value, rowid_from_index_key, rowid_from_key, rowid_key
from .pager import Pager
from .query import plan_rows_for_write, plan_select
from .sql import ast
from .sql.parser import parse, parse_one
from .values import SQLError, coerce, truth


class Result:
    def __init__(self, columns=(), rows=(), rowcount=-1, lastrowid=None):
        self.columns = list(columns)
        self.rows = list(rows)
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def __repr__(self):
        return f"Result(columns={self.columns}, rows={len(self.rows)}, rowcount={self.rowcount})"


class Database:
    def __init__(self, path):
        self.path = path
        self.pager = Pager(path)
        self.catalog = Catalog(self.pager)
        self.in_transaction = False
        self.closed = False
        self._parsed = OrderedDict()   # sql text -> statement, for repeated DML

    # --- running SQL --------------------------------------------------------------

    def execute(self, sql, params=()):
        self._check_open()
        with _too_complex_guard():
            return self._run(self._parse(sql), sql, list(params))

    def _parse(self, sql):
        stmt = self._parsed.get(sql)
        if stmt is not None:
            self._parsed.move_to_end(sql)
            return stmt
        stmt = parse_one(sql)
        # DDL statements get modified while they're applied, so only cache
        # the statements that are run over and over with new parameters
        if isinstance(stmt, (ast.Select, ast.Insert, ast.Update, ast.Delete)):
            self._parsed[sql] = stmt
            if len(self._parsed) > 256:
                self._parsed.popitem(last=False)
        return stmt

    def executescript(self, sql):
        """Run several ;-separated statements; returns the last result."""
        self._check_open()
        result = Result()
        with _too_complex_guard():
            for stmt in parse(sql):
                result = self._run(stmt, sql, [])
        return result

    def _run(self, stmt, sql, params):
        if isinstance(stmt, ast.Begin):
            if self.in_transaction:
                raise SQLError("cannot start a transaction within a transaction")
            self.in_transaction = True
            return Result()
        if isinstance(stmt, ast.Commit):
            if not self.in_transaction:
                raise SQLError("cannot commit - no transaction is active")
            self.pager.commit()
            self.in_transaction = False
            return Result()
        if isinstance(stmt, ast.Rollback):
            if not self.in_transaction:
                raise SQLError("cannot rollback - no transaction is active")
            self._rollback()
            self.in_transaction = False
            return Result()
        if isinstance(stmt, ast.Vacuum):
            if self.in_transaction:
                raise SQLError("cannot VACUUM from within a transaction")
            self._vacuum()
            return Result()
        if isinstance(stmt, ast.Explain):
            return self._explain(stmt.statement, params)

        savepoint = self.pager.savepoint() if self.in_transaction else None
        try:
            result = self._dispatch(stmt, sql, params)
        except BaseException:
            if savepoint is not None:
                self.pager.rollback_to(savepoint)
                self.catalog.load()
            else:
                self._rollback()
            raise
        if savepoint is not None:
            self.pager.release()
        else:
            self.pager.commit()
        return result

    def _rollback(self):
        self.pager.rollback()
        self.catalog.load()

    def _dispatch(self, stmt, sql, params):
        if isinstance(stmt, ast.Select):
            plan = plan_select(stmt, self.catalog, self.pager, params)
            return Result(plan.columns, plan.run())
        if isinstance(stmt, ast.Insert):
            return self._insert(stmt, params)
        if isinstance(stmt, ast.Update):
            return self._update(stmt, params)
        if isinstance(stmt, ast.Delete):
            return self._delete(stmt, params)
        if isinstance(stmt, ast.CreateTable):
            self.catalog.create_table(stmt, _statement_text(sql, stmt))
            return Result()
        if isinstance(stmt, ast.DropTable):
            self.catalog.drop_table(stmt)
            return Result()
        if isinstance(stmt, ast.CreateIndex):
            table, ix = self.catalog.create_index(stmt, _statement_text(sql, stmt))
            if ix is not None:
                self._fill_index(table, ix)
            return Result()
        if isinstance(stmt, ast.DropIndex):
            self.catalog.drop_index(stmt)
            return Result()
        raise SQLError(f"can't run {type(stmt).__name__}")

    def _explain(self, stmt, params):
        if isinstance(stmt, ast.Select):
            plan = plan_select(stmt, self.catalog, self.pager, params)
        elif isinstance(stmt, (ast.Update, ast.Delete)):
            table = self.catalog.get(stmt.table)
            access, _, _ = plan_rows_for_write(table, stmt.where, self.catalog, self.pager, params)
            return Result(["plan"], [(access.describe(),)])
        else:
            raise SQLError("EXPLAIN works with SELECT, UPDATE and DELETE")
        return Result(["plan"], [("  " * depth + line,) for depth, line in plan.explain])

    # --- writes -------------------------------------------------------------------

    def _check_row(self, table, values, rowid, skip_rowid=None):
        """Coerce a row for storage and enforce NOT NULL / UNIQUE."""
        row = []
        for col, v in zip(table.columns, values):
            v = coerce(v, col.type, f"{table.name}.{col.name}")
            if v is None and col.not_null:
                raise SQLError(f"NOT NULL constraint failed: {table.name}.{col.name}")
            row.append(v)
        for ix in table.indexes:
            v = row[ix.col_idx]
            if ix.unique and v is not None:
                for k, _ in BTree(self.pager, ix.root).scan_prefix(encode_value(v)):
                    if rowid_from_index_key(k) != skip_rowid:
                        raise SQLError(f"UNIQUE constraint failed: {table.name}.{ix.column}")
        return row

    def _store(self, table, rowid, row):
        tree = BTree(self.pager, table.root)
        try:
            tree.insert(rowid_key(rowid), encode_row(row))
        except DuplicateKey:
            raise SQLError(f"UNIQUE constraint failed: {table.name}.{table.columns[table.rowid_col].name}") from None
        except TreeError as e:
            raise SQLError(str(e)) from None
        for ix in table.indexes:
            BTree(self.pager, ix.root).insert(encode_value(row[ix.col_idx]) + rowid_key(rowid), b"")

    def _unstore(self, table, rowid, row):
        BTree(self.pager, table.root).delete(rowid_key(rowid))
        for ix in table.indexes:
            BTree(self.pager, ix.root).delete(encode_value(row[ix.col_idx]) + rowid_key(rowid))

    def _next_rowid(self, table):
        last = BTree(self.pager, table.root).last_key()
        if last is None:
            return 1
        n = rowid_from_key(last) + 1
        if n > 2 ** 63 - 1:
            raise SQLError("database or disk is full (rowid space exhausted)")
        return n

    def _insert(self, stmt, params):
        table = self.catalog.get(stmt.table)
        if stmt.columns is None:
            targets = list(range(len(table.columns)))
        else:
            targets = [table.column(c) for c in stmt.columns]
            if len(set(targets)) != len(targets):
                raise SQLError("a column is named more than once in INSERT")
        defaults = [constant(c.default, params) if c.default is not None else None for c in table.columns]
        next_rowid = None
        last = None
        for exprs in stmt.rows:
            if len(exprs) != len(targets):
                raise SQLError(f"{len(exprs)} values for {len(targets)} columns")
            values = list(defaults)
            for i, e in zip(targets, exprs):
                values[i] = constant(e, params)

            if table.rowid_col is not None and values[table.rowid_col] is not None:
                values[table.rowid_col] = coerce(values[table.rowid_col], "INTEGER",
                                                 f"{table.name}.{table.columns[table.rowid_col].name}")
                rowid = values[table.rowid_col]
            else:
                if next_rowid is None:
                    next_rowid = self._next_rowid(table)
                rowid = next_rowid
                if table.rowid_col is not None:
                    values[table.rowid_col] = rowid
            row = self._check_row(table, values, rowid)
            self._store(table, rowid, row)
            if next_rowid is not None:
                next_rowid = max(next_rowid, rowid + 1)
            last = rowid
        return Result(rowcount=len(stmt.rows), lastrowid=last)

    def _matching(self, table, where, params):
        access, pred, ctx = plan_rows_for_write(table, where, self.catalog, self.pager, params)
        # collect first: the tree can't change under a running scan
        return [(rowid, values) for rowid, values in access.rows(())
                if pred is None or truth(pred(values)) == 1], ctx

    def _update(self, stmt, params):
        table = self.catalog.get(stmt.table)
        rows, ctx = self._matching(table, stmt.where, params)
        assigns = []
        for name, e in stmt.assignments:
            assigns.append((table.column(name), compile_expr(e, ctx)))
        if len({i for i, _ in assigns}) != len(assigns):
            raise SQLError("a column is assigned more than once in UPDATE")

        for rowid, old in rows:
            new = list(old)
            for i, f in assigns:
                new[i] = f(old)        # every SET sees the old row, as in SQL
            new_rowid = rowid
            if table.rowid_col is not None:
                if new[table.rowid_col] is None:
                    raise SQLError(f"NOT NULL constraint failed: {table.name}.{table.columns[table.rowid_col].name}")
                new_rowid = coerce(new[table.rowid_col], "INTEGER", f"{table.name}.{table.columns[table.rowid_col].name}")
                new[table.rowid_col] = new_rowid
            self._unstore(table, rowid, list(old))
            self._store(table, new_rowid, self._check_row(table, new, new_rowid))
        return Result(rowcount=len(rows))

    def _delete(self, stmt, params):
        table = self.catalog.get(stmt.table)
        rows, _ = self._matching(table, stmt.where, params)
        for rowid, values in rows:
            self._unstore(table, rowid, list(values))
        return Result(rowcount=len(rows))

    def _fill_index(self, table, ix):
        tree = BTree(self.pager, ix.root)
        last = None
        # walk the table in index order by sorting the keys first
        keys = sorted(encode_value(decode_row(raw)[ix.col_idx]) + k
                      for k, raw in BTree(self.pager, table.root).scan())
        for key in keys:
            value_part = key[:-8]
            if ix.unique and last == value_part and not value_part.startswith(b"\x01"):
                raise SQLError(f"UNIQUE constraint failed: {table.name}.{ix.column}")
            last = value_part
            tree.insert(key, b"")

    # --- maintenance ----------------------------------------------------------------

    def _vacuum(self):
        """Rebuild the file without free pages, in a fresh file swapped in atomically."""
        tmp = self.path + ".vacuum"
        if os.path.exists(tmp):
            os.remove(tmp)
        fresh = Database(tmp)
        try:
            for table in self.catalog.tables.values():
                fresh.catalog.create_table(parse_one(table.sql), table.sql)
                new_table = fresh.catalog.get(table.name)
                dest = BTree(fresh.pager, new_table.root)
                for k, raw in BTree(self.pager, table.root).scan():
                    dest.insert(k, raw)
                for ix in new_table.indexes:
                    fresh._fill_index(new_table, ix)
                for ix in table.indexes:
                    if not ix.automatic:
                        stmt = parse_one(ix.sql)
                        _, new_ix = fresh.catalog.create_index(stmt, ix.sql)
                        fresh._fill_index(new_table, new_ix)
            fresh.pager.commit()
        finally:
            fresh.pager.file.close()
        self.pager.file.close()
        os.replace(tmp, self.path)
        self.pager = Pager(self.path)
        self.catalog = Catalog(self.pager)

    # --- lifecycle --------------------------------------------------------------------

    def _check_open(self):
        if self.closed:
            raise SQLError("database is closed")

    def close(self):
        if not self.closed:
            if self.in_transaction:
                self._rollback()
            self.pager.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def tables(self):
        return sorted(t.name for t in self.catalog.tables.values())


class _too_complex_guard:
    """Turn a blown recursion limit (say, a 100k-term arithmetic chain) into a
    normal SQL error. By then _run has already undone the statement."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is RecursionError:
            raise SQLError("expression is too complex") from None
        return False


def _statement_text(sql, stmt):
    # the schema stores the CREATE statement; for a script, regenerate it
    # rather than storing the whole script
    statements = parse(sql)
    if len(statements) == 1:
        return sql.strip().rstrip(";").strip()
    return _render_create(stmt)


def _render_create(stmt):
    from .expr import unparse
    if isinstance(stmt, ast.CreateIndex):
        return f"CREATE {'UNIQUE ' if stmt.unique else ''}INDEX {stmt.name} ON {stmt.table} ({stmt.column})"
    cols = []
    for c in stmt.columns:
        parts = [c.name, c.type]
        if c.primary_key:
            parts.append("PRIMARY KEY")
        if c.not_null and not c.primary_key:
            parts.append("NOT NULL")
        if c.unique and not c.primary_key:
            parts.append("UNIQUE")
        if c.default is not None:
            parts.append("DEFAULT " + unparse(c.default))
        cols.append(" ".join(parts))
    return f"CREATE TABLE {stmt.name} ({', '.join(cols)})"


def connect(path):
    return Database(path)
