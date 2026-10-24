from dataclasses import dataclass, field
from typing import Optional, Union

# --- expressions -------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: object


@dataclass(frozen=True)
class Param:
    index: int  # position of the ? in the statement


@dataclass(frozen=True)
class Column:
    name: str
    table: Optional[str] = None

    def __str__(self):
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Unary:
    op: str  # '-', '+', 'NOT'
    operand: "Expr"


@dataclass(frozen=True)
class Binary:
    op: str  # '+', '-', '*', '/', '%', '||', '=', '!=', '<', '<=', '>', '>=', 'AND', 'OR', 'LIKE'
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class IsNull:
    operand: "Expr"
    negated: bool = False


@dataclass(frozen=True)
class Between:
    operand: "Expr"
    low: "Expr"
    high: "Expr"
    negated: bool = False


@dataclass(frozen=True)
class InList:
    operand: "Expr"
    items: tuple
    negated: bool = False


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple
    star: bool = False       # COUNT(*)
    distinct: bool = False   # COUNT(DISTINCT x)


@dataclass(frozen=True)
class Case:
    operand: Optional["Expr"]   # CASE x WHEN ... (None for searched CASE)
    whens: tuple                # ((condition, result), ...)
    default: Optional["Expr"]


Expr = Union[Literal, Param, Column, Unary, Binary, IsNull, Between, InList, Call, Case]

# --- statements --------------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    type: str                # INTEGER, REAL or TEXT
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False
    default: Optional[Expr] = None


@dataclass
class CreateTable:
    name: str
    columns: list
    if_not_exists: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    column: str
    unique: bool = False
    if_not_exists: bool = False


@dataclass
class DropIndex:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: Optional[list]
    rows: list               # list of lists of Expr


@dataclass
class SelectItem:
    expr: Optional[Expr]     # None means * (or table.* when star_table is set)
    alias: Optional[str] = None
    star_table: Optional[str] = None


@dataclass
class TableRef:
    name: str
    alias: Optional[str] = None

    @property
    def ref(self):
        return self.alias or self.name


@dataclass
class Join:
    table: TableRef
    on: Optional[Expr]
    left: bool = False       # LEFT JOIN


@dataclass
class OrderItem:
    expr: Expr
    desc: bool = False


@dataclass
class Select:
    items: list
    table: Optional[TableRef] = None
    joins: list = field(default_factory=list)
    where: Optional[Expr] = None
    group_by: list = field(default_factory=list)
    having: Optional[Expr] = None
    order_by: list = field(default_factory=list)
    limit: Optional[Expr] = None
    offset: Optional[Expr] = None
    distinct: bool = False


@dataclass
class Update:
    table: str
    assignments: list        # [(column, Expr)]
    where: Optional[Expr] = None


@dataclass
class Delete:
    table: str
    where: Optional[Expr] = None


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


@dataclass
class Explain:
    statement: object


@dataclass
class Vacuum:
    pass
