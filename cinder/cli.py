"""An interactive shell in the spirit of sqlite3.

    $ cinder app.db
    cinder> SELECT * FROM users WHERE age > 30;
"""

import argparse
import sys
import time

from . import __version__
from .database import connect
from .sql.lexer import SQLSyntaxError
from .sql.parser import parse
from .values import SQLError, to_text

HELP = """\
.tables              list tables
.schema [table]      show CREATE statements
.indexes [table]     list indexes
.timer on|off        print how long each statement took
.read FILE           run the SQL in a file
.quit                exit
Statements end with ';'. EXPLAIN SELECT ... shows the query plan."""


def render(columns, rows, limit=200):
    """A plain text table, like sqlite3's .mode box without the box."""
    def cell(v):
        if v is None:
            return "NULL"
        return to_text(v)

    shown = [[cell(v) for v in row] for row in rows[:limit]]
    widths = [len(c) for c in columns]
    for row in shown:
        for i, v in enumerate(row):
            widths[i] = min(max(widths[i], len(v)), 60)
    line = lambda vals: "  ".join(v[:60].ljust(w) for v, w in zip(vals, widths)).rstrip()
    out = [line(columns), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in shown]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more rows")
    return "\n".join(out)


class Shell:
    def __init__(self, db, out=None):
        self.db = db
        self.out = out          # None: whatever sys.stdout is at the time
        self.timer = False

    def print(self, *args):
        print(*args, file=self.out or sys.stdout)

    def run_sql(self, sql):
        start = time.perf_counter()
        try:
            statements = parse(sql)
        except SQLSyntaxError as e:
            self.print(f"Error: {e}")
            return False
        ok = True
        for stmt_text in _split_statements(sql):
            try:
                result = self.db.execute(stmt_text)
            except (SQLError, SQLSyntaxError) as e:
                self.print(f"Error: {e}")
                ok = False
                break
            if result.columns == ["plan"]:
                self.print("\n".join(r[0] for r in result.rows))
            elif result.columns:
                self.print(render(result.columns, result.rows))
            elif result.rowcount >= 0:
                self.print(f"{result.rowcount} row{'s' if result.rowcount != 1 else ''} affected")
        if self.timer and statements:
            self.print(f"Run time: {(time.perf_counter() - start) * 1000:.2f} ms")
        return ok

    def dot(self, line):
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        cat = self.db.catalog
        if cmd in (".quit", ".exit"):
            return False
        if cmd == ".help":
            self.print(HELP)
        elif cmd == ".tables":
            self.print("  ".join(self.db.tables()))
        elif cmd == ".schema":
            for t in sorted(cat.tables.values(), key=lambda t: t.name):
                if args and t.name.lower() != args[0].lower():
                    continue
                self.print(t.sql + ";")
                for ix in t.indexes:
                    if not ix.automatic:
                        self.print(ix.sql + ";")
        elif cmd == ".indexes":
            for t in sorted(cat.tables.values(), key=lambda t: t.name):
                if args and t.name.lower() != args[0].lower():
                    continue
                for ix in t.indexes:
                    self.print(f"{ix.name}  ({t.name}.{ix.column}{', unique' if ix.unique else ''})")
        elif cmd == ".timer":
            self.timer = bool(args) and args[0].lower() == "on"
        elif cmd == ".read" and args:
            try:
                with open(args[0]) as f:
                    self.run_sql(f.read())
            except OSError as e:
                self.print(f"Error: {e}")
        else:
            self.print(f"Error: unknown command {cmd}. Try .help")
        return True

    def repl(self):
        self.print(f"cinder {__version__}. Enter .help for usage.")
        buf = ""
        while True:
            try:
                line = input("cinder> " if not buf else "   ...> ")
            except EOFError:
                self.print()
                return
            except KeyboardInterrupt:
                self.print()
                buf = ""
                continue
            if not buf and line.strip().startswith("."):
                if not self.dot(line.strip()):
                    return
                continue
            buf += line + "\n"
            if buf.strip().endswith(";"):
                self.run_sql(buf)
                buf = ""


def _split_statements(sql):
    """Statement texts of a script, so each keeps its own source for the schema."""
    from .sql.lexer import tokenize
    texts = []
    start = 0
    for tok in tokenize(sql):
        if tok.kind == "op" and tok.value == ";":
            texts.append(sql[start:tok.pos])
            start = tok.pos + 1
    texts.append(sql[start:])
    return [t for t in texts if t.strip()]


def main(argv=None):
    p = argparse.ArgumentParser(prog="cinder", description="A small SQL database in pure Python")
    p.add_argument("database", help="database file (created if it doesn't exist)")
    p.add_argument("sql", nargs="?", help="run this SQL and exit")
    args = p.parse_args(argv)

    db = connect(args.database)
    shell = Shell(db)
    try:
        if args.sql is not None:
            return 0 if shell.run_sql(args.sql) else 1
        if not sys.stdin.isatty():
            return 0 if shell.run_sql(sys.stdin.read()) else 1
        shell.repl()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
