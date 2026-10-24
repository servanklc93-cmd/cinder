"""Recursive descent parser for the SQL subset cinder understands.

Operator precedence follows SQLite, lowest first:

    OR
    AND
    NOT
    =  !=  IS  IN  LIKE  BETWEEN
    <  <=  >  >=
    +  -
    *  /  %
    ||
    unary -  +
"""

from . import ast
from .lexer import SQLSyntaxError, tokenize

TYPE_NAMES = {
    "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "BOOLEAN": "INTEGER", "BOOL": "INTEGER",
    "REAL": "REAL", "FLOAT": "REAL", "DOUBLE": "REAL", "NUMERIC": "REAL",
    "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT", "STRING": "TEXT",
}


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        self.pos = 0
        self.param_count = sum(1 for t in self.tokens if t.kind == "param")

    # --- helpers --------------------------------------------------------------

    @property
    def tok(self):
        return self.tokens[self.pos]

    def advance(self):
        t = self.tokens[self.pos]
        if t.kind != "eof":
            self.pos += 1
        return t

    def at(self, kind, value=None):
        t = self.tok
        return t.kind == kind and (value is None or t.value == value)

    def at_kw(self, *words):
        return self.tok.kind == "kw" and self.tok.value in words

    def accept(self, kind, value=None):
        if self.at(kind, value):
            return self.advance()
        return None

    def accept_kw(self, *words):
        if self.at_kw(*words):
            return self.advance().value
        return None

    def expect(self, kind, value=None, what=None):
        if self.at(kind, value):
            return self.advance()
        raise self.error(f"expected {what or value or kind}")

    def expect_kw(self, word):
        return self.expect("kw", word, word)

    def error(self, message):
        t = self.tok
        found = "end of input" if t.kind == "eof" else repr(str(t.value))
        return SQLSyntaxError(f"{message}, found {found}", t.pos, self.sql)

    def ident(self, what="a name"):
        t = self.tok
        if t.kind == "ident":
            return self.advance().value
        raise self.error(f"expected {what}")

    # --- statements -----------------------------------------------------------

    def parse_script(self):
        statements = []
        while not self.at("eof"):
            if self.accept("op", ";"):
                continue
            statements.append(self.statement())
            if not self.at("eof"):
                self.expect("op", ";", "';' between statements")
        return statements

    def statement(self):
        if self.accept_kw("EXPLAIN"):
            return ast.Explain(self.statement())
        if self.at_kw("SELECT"):
            return self.select()
        if self.accept_kw("INSERT"):
            return self.insert()
        if self.accept_kw("UPDATE"):
            return self.update()
        if self.accept_kw("DELETE"):
            return self.delete()
        if self.accept_kw("CREATE"):
            return self.create()
        if self.accept_kw("DROP"):
            return self.drop()
        if self.accept_kw("BEGIN"):
            self.accept_kw("TRANSACTION")
            return ast.Begin()
        if self.accept_kw("COMMIT", "END"):
            self.accept_kw("TRANSACTION")
            return ast.Commit()
        if self.accept_kw("ROLLBACK"):
            self.accept_kw("TRANSACTION")
            return ast.Rollback()
        if self.accept_kw("VACUUM"):
            return ast.Vacuum()
        raise self.error("expected a statement")

    def if_not_exists(self):
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            return True
        return False

    def create(self):
        unique = bool(self.accept_kw("UNIQUE"))
        if self.accept_kw("INDEX"):
            ine = self.if_not_exists()
            name = self.ident("an index name")
            self.expect_kw("ON")
            table = self.ident("a table name")
            self.expect("op", "(")
            column = self.ident("a column name")
            if self.at("op", ","):
                raise self.error("multi-column indexes are not supported")
            self.accept_kw("ASC", "DESC")
            self.expect("op", ")")
            return ast.CreateIndex(name, table, column, unique, ine)
        if unique:
            raise self.error("expected INDEX after UNIQUE")
        self.expect_kw("TABLE")
        ine = self.if_not_exists()
        name = self.ident("a table name")
        self.expect("op", "(")
        columns = [self.column_def()]
        while self.accept("op", ","):
            columns.append(self.column_def())
        self.expect("op", ")")
        return ast.CreateTable(name, columns, ine)

    def column_def(self):
        name = self.ident("a column name")
        t = self.tok
        type_word = t.value.upper() if t.kind in ("ident", "kw") else None
        if type_word not in TYPE_NAMES:
            raise self.error("expected a column type (INTEGER, REAL or TEXT)")
        self.advance()
        if self.accept("op", "("):  # VARCHAR(255): the size is accepted and ignored
            self.expect("num")
            self.expect("op", ")")
        col = ast.ColumnDef(name, TYPE_NAMES[type_word])
        while True:
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                col.primary_key = True
            elif self.accept_kw("NOT"):
                self.expect_kw("NULL")
                col.not_null = True
            elif self.accept_kw("NULL"):
                pass
            elif self.accept_kw("UNIQUE"):
                col.unique = True
            elif self.accept_kw("DEFAULT"):
                col.default = self.primary()
            else:
                return col

    def drop(self):
        kind = self.accept_kw("TABLE", "INDEX")
        if not kind:
            raise self.error("expected TABLE or INDEX")
        if_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            if_exists = True
        name = self.ident()
        return ast.DropTable(name, if_exists) if kind == "TABLE" else ast.DropIndex(name, if_exists)

    def insert(self):
        self.expect_kw("INTO")
        table = self.ident("a table name")
        columns = None
        if self.accept("op", "("):
            columns = [self.ident("a column name")]
            while self.accept("op", ","):
                columns.append(self.ident("a column name"))
            self.expect("op", ")")
        self.expect_kw("VALUES")
        rows = [self.paren_list()]
        while self.accept("op", ","):
            rows.append(self.paren_list())
        return ast.Insert(table, columns, rows)

    def paren_list(self):
        self.expect("op", "(")
        items = [self.expr()]
        while self.accept("op", ","):
            items.append(self.expr())
        self.expect("op", ")")
        return items

    def update(self):
        table = self.ident("a table name")
        self.expect_kw("SET")
        assignments = []
        while True:
            col = self.ident("a column name")
            self.expect("op", "=")
            assignments.append((col, self.expr()))
            if not self.accept("op", ","):
                break
        where = self.expr() if self.accept_kw("WHERE") else None
        return ast.Update(table, assignments, where)

    def delete(self):
        self.expect_kw("FROM")
        table = self.ident("a table name")
        where = self.expr() if self.accept_kw("WHERE") else None
        return ast.Delete(table, where)

    def select(self):
        self.expect_kw("SELECT")
        distinct = bool(self.accept_kw("DISTINCT"))
        items = [self.select_item()]
        while self.accept("op", ","):
            items.append(self.select_item())
        sel = ast.Select(items, distinct=distinct)

        if self.accept_kw("FROM"):
            sel.table = self.table_ref()
            while True:
                if self.accept("op", ","):
                    sel.joins.append(ast.Join(self.table_ref(), None))
                    continue
                left = False
                if self.accept_kw("LEFT"):
                    self.accept_kw("OUTER")
                    left = True
                elif self.accept_kw("INNER", "CROSS"):
                    pass
                elif not self.at_kw("JOIN"):
                    break
                self.expect_kw("JOIN")
                table = self.table_ref()
                on = self.expr() if self.accept_kw("ON") else None
                if left and on is None:
                    raise self.error("LEFT JOIN needs an ON condition")
                sel.joins.append(ast.Join(table, on, left))

        if self.accept_kw("WHERE"):
            sel.where = self.expr()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            sel.group_by = [self.expr()]
            while self.accept("op", ","):
                sel.group_by.append(self.expr())
        if self.accept_kw("HAVING"):
            sel.having = self.expr()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            sel.order_by = [self.order_item()]
            while self.accept("op", ","):
                sel.order_by.append(self.order_item())
        if self.accept_kw("LIMIT"):
            sel.limit = self.expr()
            if self.accept_kw("OFFSET"):
                sel.offset = self.expr()
            elif self.accept("op", ","):  # LIMIT offset, count
                sel.offset, sel.limit = sel.limit, self.expr()
        return sel

    def select_item(self):
        if self.accept("op", "*"):
            return ast.SelectItem(None)
        # table.*
        if self.at("ident") and self.tokens[self.pos + 1].value == "." and self.tokens[self.pos + 2].value == "*":
            table = self.advance().value
            self.advance()
            self.advance()
            return ast.SelectItem(None, star_table=table)
        expr = self.expr()
        alias = None
        if self.accept_kw("AS"):
            alias = self.ident("an alias")
        elif self.at("ident"):
            alias = self.advance().value
        return ast.SelectItem(expr, alias)

    def table_ref(self):
        name = self.ident("a table name")
        alias = None
        if self.accept_kw("AS"):
            alias = self.ident("an alias")
        elif self.at("ident"):
            alias = self.advance().value
        return ast.TableRef(name, alias)

    def order_item(self):
        expr = self.expr()
        desc = self.accept_kw("ASC", "DESC") == "DESC"
        return ast.OrderItem(expr, desc)

    # --- expressions ----------------------------------------------------------

    def expr(self):
        return self.or_expr()

    def or_expr(self):
        left = self.and_expr()
        while self.accept_kw("OR"):
            left = ast.Binary("OR", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept_kw("AND"):
            left = ast.Binary("AND", left, self.not_expr())
        return left

    def not_expr(self):
        if self.accept_kw("NOT"):
            return ast.Unary("NOT", self.not_expr())
        return self.equality()

    def equality(self):
        left = self.comparison()
        while True:
            if self.at("op") and self.tok.value in ("=", "!="):
                op = self.advance().value
                left = ast.Binary(op, left, self.comparison())
            elif self.accept_kw("IS"):
                negated = bool(self.accept_kw("NOT"))
                self.expect_kw("NULL")
                left = ast.IsNull(left, negated)
            else:
                negated = False
                save = self.pos
                if self.accept_kw("NOT"):
                    negated = True
                    if not self.at_kw("IN", "LIKE", "BETWEEN"):
                        self.pos = save
                        return left
                if self.accept_kw("IN"):
                    self.expect("op", "(")
                    items = [self.expr()]
                    while self.accept("op", ","):
                        items.append(self.expr())
                    self.expect("op", ")")
                    left = ast.InList(left, tuple(items), negated)
                elif self.accept_kw("LIKE"):
                    like = ast.Binary("LIKE", left, self.comparison())
                    left = ast.Unary("NOT", like) if negated else like
                elif self.accept_kw("BETWEEN"):
                    low = self.comparison()
                    self.expect_kw("AND")
                    high = self.comparison()
                    left = ast.Between(left, low, high, negated)
                else:
                    return left

    def comparison(self):
        left = self.additive()
        while self.at("op") and self.tok.value in ("<", "<=", ">", ">="):
            op = self.advance().value
            left = ast.Binary(op, left, self.additive())
        return left

    def additive(self):
        left = self.multiplicative()
        while self.at("op") and self.tok.value in ("+", "-"):
            op = self.advance().value
            left = ast.Binary(op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.concat()
        while self.at("op") and self.tok.value in ("*", "/", "%"):
            op = self.advance().value
            left = ast.Binary(op, left, self.concat())
        return left

    def concat(self):
        left = self.unary()
        while self.accept("op", "||"):
            left = ast.Binary("||", left, self.unary())
        return left

    def unary(self):
        if self.at("op") and self.tok.value in ("-", "+"):
            op = self.advance().value
            operand = self.unary()
            # fold negative literals so -5 is a plain literal (matters for LIMIT etc.)
            if op == "-" and isinstance(operand, ast.Literal) and isinstance(operand.value, (int, float)) \
                    and not isinstance(operand.value, bool):
                return ast.Literal(-operand.value)
            return ast.Unary(op, operand)
        return self.primary()

    def primary(self):
        t = self.tok
        if t.kind == "num" or t.kind == "str":
            self.advance()
            return ast.Literal(t.value)
        if t.kind == "param":
            self.advance()
            return ast.Param(t.value)
        if t.kind == "kw":
            if t.value == "NULL":
                self.advance()
                return ast.Literal(None)
            if t.value in ("TRUE", "FALSE"):
                self.advance()
                return ast.Literal(1 if t.value == "TRUE" else 0)
            if t.value == "CASE":
                self.advance()
                return self.case()
        if self.accept("op", "("):
            e = self.expr()
            self.expect("op", ")")
            return e
        if t.kind == "ident":
            name = self.advance().value
            if self.accept("op", "("):
                return self.call(name)
            if self.accept("op", "."):
                return ast.Column(self.ident("a column name"), name)
            return ast.Column(name)
        raise self.error("expected an expression")

    def call(self, name):
        name = name.upper()
        if self.accept("op", "*"):
            self.expect("op", ")")
            return ast.Call(name, (), star=True)
        if self.accept("op", ")"):
            return ast.Call(name, ())
        distinct = bool(self.accept_kw("DISTINCT"))
        args = [self.expr()]
        while self.accept("op", ","):
            args.append(self.expr())
        self.expect("op", ")")
        return ast.Call(name, tuple(args), distinct=distinct)

    def case(self):
        operand = None if self.at_kw("WHEN") else self.expr()
        whens = []
        while self.accept_kw("WHEN"):
            cond = self.expr()
            self.expect_kw("THEN")
            whens.append((cond, self.expr()))
        if not whens:
            raise self.error("expected WHEN")
        default = self.expr() if self.accept_kw("ELSE") else None
        self.expect_kw("END")
        return ast.Case(operand, tuple(whens), default)


def parse(sql):
    """Parse one or more ;-separated statements."""
    return Parser(sql).parse_script()


def parse_one(sql):
    statements = parse(sql)
    if len(statements) != 1:
        raise SQLSyntaxError(f"expected one statement, got {len(statements)}")
    return statements[0]
