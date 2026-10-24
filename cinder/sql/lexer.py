import re
from dataclasses import dataclass


class SQLSyntaxError(Exception):
    def __init__(self, message, pos=None, sql=None):
        if pos is not None and sql is not None:
            line = sql.count("\n", 0, pos) + 1
            col = pos - (sql.rfind("\n", 0, pos) + 1) + 1
            message = f"{message} (line {line}, column {col})"
        super().__init__(message)


KEYWORDS = {
    "SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
    "CREATE", "DROP", "TABLE", "INDEX", "ON", "UNIQUE", "PRIMARY", "KEY", "NOT", "NULL",
    "DEFAULT", "IF", "EXISTS", "AND", "OR", "IS", "IN", "LIKE", "BETWEEN", "AS", "ORDER",
    "BY", "ASC", "DESC", "LIMIT", "OFFSET", "GROUP", "HAVING", "DISTINCT", "JOIN", "INNER",
    "LEFT", "OUTER", "CROSS", "BEGIN", "COMMIT", "ROLLBACK", "TRANSACTION", "EXPLAIN",
    "CASE", "WHEN", "THEN", "ELSE", "END", "TRUE", "FALSE", "VACUUM",
}


@dataclass
class Token:
    kind: str    # 'kw', 'ident', 'num', 'str', 'op', 'param', 'eof'
    value: object
    pos: int


_TOKEN = re.compile(r"""
    (?P<ws>\s+|--[^\n]*|/\*.*?\*/)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")*")
  | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op><=|>=|<>|!=|==|\|\||[-+*/%=<>(),.;])
  | (?P<param>\?)
""", re.VERBOSE | re.DOTALL)


def tokenize(sql):
    tokens = []
    pos = 0
    params = 0
    while pos < len(sql):
        m = _TOKEN.match(sql, pos)
        if not m:
            if sql[pos] == "'":
                raise SQLSyntaxError("unterminated string", pos, sql)
            raise SQLSyntaxError(f"unexpected character {sql[pos]!r}", pos, sql)
        kind = m.lastgroup
        text = m.group()
        if kind == "num":
            value = float(text) if any(c in text for c in ".eE") else int(text)
            tokens.append(Token("num", value, pos))
        elif kind == "str":
            tokens.append(Token("str", text[1:-1].replace("''", "'"), pos))
        elif kind == "qident":
            tokens.append(Token("ident", text[1:-1].replace('""', '"'), pos))
        elif kind == "ident":
            upper = text.upper()
            if upper in KEYWORDS:
                tokens.append(Token("kw", upper, pos))
            else:
                tokens.append(Token("ident", text, pos))
        elif kind == "op":
            tokens.append(Token("op", {"==": "=", "<>": "!="}.get(text, text), pos))
        elif kind == "param":
            tokens.append(Token("param", params, pos))
            params += 1
        pos = m.end()
    tokens.append(Token("eof", None, len(sql)))
    return tokens
