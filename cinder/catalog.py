"""Tables, indexes and the schema table that stores them.

The schema lives in its own B+tree (its root is in the file header), one
row per object: [kind, name, table, root page, sql]. Like SQLite, it keeps
the original CREATE statement and parses it again when the file is opened.
"""

from dataclasses import dataclass, field
from typing import Optional

from .btree import BTree
from .keys import decode_row, encode_key, encode_row
from .sql import ast
from .sql.parser import parse_one
from .values import SQLError


@dataclass
class Index:
    name: str
    table: str
    column: str
    col_idx: int
    root: int
    unique: bool
    sql: Optional[str]          # None for indexes made by a UNIQUE constraint

    @property
    def automatic(self):
        return self.sql is None


@dataclass
class Table:
    name: str
    columns: list               # [ast.ColumnDef]
    root: int
    sql: str
    indexes: list = field(default_factory=list)

    def __post_init__(self):
        for col in self.columns:
            # a non-integer primary key is a NOT NULL + UNIQUE constraint. done
            # here so it also holds for tables loaded back from the schema
            if col.primary_key and col.type != "INTEGER":
                col.not_null = col.unique = True
        self.col_index = {c.name.lower(): i for i, c in enumerate(self.columns)}
        # an INTEGER PRIMARY KEY column is the rowid itself, as in SQLite
        self.rowid_col = next(
            (i for i, c in enumerate(self.columns) if c.primary_key and c.type == "INTEGER"), None)

    def column(self, name):
        i = self.col_index.get(name.lower())
        if i is None:
            raise SQLError(f"no such column: {name}")
        return i

    def index_on(self, col_idx):
        """The best index on a column: unique first."""
        found = [ix for ix in self.indexes if ix.col_idx == col_idx]
        found.sort(key=lambda ix: not ix.unique)
        return found[0] if found else None


class Catalog:
    def __init__(self, pager):
        self.pager = pager
        if pager.schema_root == 0:
            pager.schema_root = BTree.create(pager).root
            pager.commit()
        self.load()

    @property
    def schema(self):
        return BTree(self.pager, self.pager.schema_root)

    def load(self):
        self.tables = {}
        indexes = []
        for _, raw in self.schema.scan():
            kind, name, table, root, sql = decode_row(raw)
            if kind == "table":
                stmt = parse_one(sql)
                self.tables[name.lower()] = Table(stmt.name, stmt.columns, root, sql)
            else:
                indexes.append((kind, name, table, root, sql))
        for kind, name, table_name, root, sql in indexes:
            table = self.tables[table_name.lower()]
            if kind == "autoindex":
                # made by a UNIQUE constraint: the sql field holds the column name
                column, unique, sql = sql, True, None
            else:
                stmt = parse_one(sql)
                column, unique = stmt.column, stmt.unique
            table.indexes.append(Index(name, table.name, column, table.column(column), root, unique, sql))

    def get(self, name):
        table = self.tables.get(name.lower())
        if table is None:
            raise SQLError(f"no such table: {name}")
        return table

    def find_index(self, name):
        for table in self.tables.values():
            for ix in table.indexes:
                if ix.name.lower() == name.lower():
                    return table, ix
        return None, None

    def _put(self, kind, name, table, root, sql):
        self.schema.insert(encode_key(name.lower()), encode_row([kind, name, table, root, sql]))

    def _remove(self, name):
        self.schema.delete(encode_key(name.lower()))

    # --- DDL --------------------------------------------------------------------

    def create_table(self, stmt, sql):
        if stmt.name.lower() in self.tables:
            if stmt.if_not_exists:
                return None
            raise SQLError(f"table {stmt.name} already exists")
        if self.find_index(stmt.name)[1]:
            raise SQLError(f"there is already an index named {stmt.name}")
        seen = set()
        for col in stmt.columns:
            if col.name.lower() in seen:
                raise SQLError(f"duplicate column name: {col.name}")
            seen.add(col.name.lower())
        if sum(c.primary_key for c in stmt.columns) > 1:
            raise SQLError(f'table "{stmt.name}" has more than one primary key')
        for col in stmt.columns:
            if col.default is not None and not isinstance(col.default, ast.Literal):
                raise SQLError(f"default value of column {col.name} must be a constant")

        tree = BTree.create(self.pager)
        table = Table(stmt.name, stmt.columns, tree.root, sql)
        self._put("table", stmt.name, stmt.name, tree.root, sql)
        self.tables[stmt.name.lower()] = table

        n = 0
        for i, col in enumerate(stmt.columns):
            if col.unique and i != table.rowid_col:
                n += 1
                ix_tree = BTree.create(self.pager)
                name = f"sqlite_autoindex_{stmt.name}_{n}"
                table.indexes.append(Index(name, stmt.name, col.name, i, ix_tree.root, True, None))
                self._put("autoindex", name, stmt.name, ix_tree.root, col.name)
        return table

    def drop_table(self, stmt):
        table = self.tables.get(stmt.name.lower())
        if table is None:
            if stmt.if_exists:
                return
            raise SQLError(f"no such table: {stmt.name}")
        for ix in table.indexes:
            BTree(self.pager, ix.root).destroy()
            self._remove(ix.name)
        BTree(self.pager, table.root).destroy()
        self._remove(table.name)
        del self.tables[table.name.lower()]

    def create_index(self, stmt, sql):
        """Registers the index; the caller fills it from the table."""
        table = self.get(stmt.table)
        if self.find_index(stmt.name)[1] or stmt.name.lower() in self.tables:
            if stmt.if_not_exists:
                return None, None
            raise SQLError(f"index {stmt.name} already exists")
        col_idx = table.column(stmt.column)
        tree = BTree.create(self.pager)
        ix = Index(stmt.name, table.name, table.columns[col_idx].name, col_idx, tree.root, stmt.unique, sql)
        table.indexes.append(ix)
        self._put("index", stmt.name, table.name, tree.root, sql)
        return table, ix

    def drop_index(self, stmt):
        table, ix = self.find_index(stmt.name)
        if ix is None:
            if stmt.if_exists:
                return
            raise SQLError(f"no such index: {stmt.name}")
        if ix.automatic:
            raise SQLError("index associated with UNIQUE or PRIMARY KEY constraint cannot be dropped")
        BTree(self.pager, ix.root).destroy()
        self._remove(ix.name)
        table.indexes.remove(ix)
