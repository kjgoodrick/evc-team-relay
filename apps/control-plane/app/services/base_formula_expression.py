"""Bounded syntax for source-verifiable Bases calculations.

This is an allowlisted expression tree, not Python/JavaScript execution. Parsing
is deliberately separate from evaluation and authorization. Every branch and
transitive formula is checked before a caller may use a computed result. Unknown
functions, dynamic property names and ambient file/context access fail closed.
"""

import math
import re
from dataclasses import dataclass

MAX_SOURCE = 8192
MAX_NODES = 256
MAX_DEPTH = 24
MAX_FORMULAS = 64
MAX_TOTAL_NODES = 2048


class UnsupportedFormula(ValueError):
    pass


@dataclass(frozen=True)
class Expression:
    kind: str
    value: object = None
    args: tuple["Expression", ...] = ()


# Only functions with bounded, reproducible primitive semantics belong here.
# File/Link access, regex, clock, random, HTML and plugin functions are excluded.
GLOBALS = {"if": (2, 3), "min": (1, 32), "max": (1, 32), "number": (1, 1)}
METHODS = {
    "isTruthy": (0, 0),
    "isEmpty": (0, 0),
    "isType": (1, 1),
    "abs": (0, 0),
    "ceil": (0, 0),
    "floor": (0, 0),
    "contains": (1, 1),
    "containsAll": (1, 32),
    "containsAny": (1, 32),
    "startsWith": (1, 1),
    "endsWith": (1, 1),
    "lower": (0, 0),
}
# path/folder depend on a recipient's local vault mount. General expressions
# cannot safely translate those strings back to a shared canonical identity.
FILE_PROPERTIES = frozenset({"name", "basename", "fullname", "ext"})
PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "==": 3,
    "!=": 3,
    ">": 4,
    "<": 4,
    ">=": 4,
    "<=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
}
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


class Parser:
    def __init__(self, source):
        if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE:
            raise UnsupportedFormula("Formula exceeds source limits")
        if any(0xD800 <= ord(c) <= 0xDFFF or ord(c) < 32 and c not in "\t\n\r" for c in source):
            raise UnsupportedFormula("Invalid formula text")
        self.source, self.pos, self.nodes = source, 0, 0
        self.token = self.next_token()

    def next_token(self):
        s = self.source
        while self.pos < len(s) and s[self.pos] in " \t\n\r":
            self.pos += 1
        if self.pos == len(s):
            return ("end", "")
        start, ch = self.pos, s[self.pos]
        if ch in "\"'":
            self.pos += 1
            result = []
            while self.pos < len(s):
                c = s[self.pos]
                self.pos += 1
                if c == ch:
                    return ("string", "".join(result))
                if c in "\n\r":
                    raise UnsupportedFormula("Unescaped string newline")
                if c == "\\":
                    if self.pos >= len(s):
                        break
                    c = s[self.pos]
                    self.pos += 1
                    escapes = {
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "b": "\b",
                        "f": "\f",
                        "\\": "\\",
                        '"': '"',
                        "'": "'",
                        "/": "/",
                    }
                    if c not in escapes:
                        raise UnsupportedFormula("Unsupported string escape")
                    c = escapes[c]
                result.append(c)
            raise UnsupportedFormula("Unterminated formula string")
        number = _NUMBER.match(s, start)
        if number:
            self.pos = number.end()
            value = float(number.group())
            if not math.isfinite(value):
                raise UnsupportedFormula("Non-finite formula number")
            return ("number", 0.0 if value == 0 else value)
        name = _NAME.match(s, start)
        if name:
            self.pos = name.end()
            return ("name", name.group())
        for operator in ("&&", "||", "==", "!=", ">=", "<="):
            if s.startswith(operator, start):
                self.pos += len(operator)
                return (operator, operator)
        if ch in "+-*/%!><().,[]":
            self.pos += 1
            return (ch, ch)
        raise UnsupportedFormula("Unsupported formula token")

    def take(self, kind):
        if self.token[0] != kind:
            raise UnsupportedFormula("Unexpected formula token")
        value = self.token[1]
        self.token = self.next_token()
        return value

    def node(self, kind, value=None, args=()):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise UnsupportedFormula("Formula exceeds node limit")
        return Expression(kind, value, tuple(args))

    def arguments(self, name, catalog, depth):
        if name not in catalog:
            raise UnsupportedFormula("Unsupported formula function")
        self.take("(")
        args = []
        if self.token[0] != ")":
            while True:
                if len(args) >= catalog[name][1]:
                    raise UnsupportedFormula("Too many formula arguments")
                args.append(self.expression(1, depth + 1))
                if self.token[0] != ",":
                    break
                self.take(",")
        self.take(")")
        if not catalog[name][0] <= len(args) <= catalog[name][1]:
            raise UnsupportedFormula("Invalid formula argument count")
        return args

    def expression(self, minimum=1, depth=0):
        if depth > MAX_DEPTH:
            raise UnsupportedFormula("Formula exceeds nesting limit")
        kind, value = self.token
        if kind in {"!", "-"}:
            self.take(kind)
            result = self.node("unary", kind, [self.expression(7, depth + 1)])
        elif kind == "(":
            self.take("(")
            result = self.expression(1, depth + 1)
            self.take(")")
        elif kind in {"number", "string"}:
            self.take(kind)
            result = self.node(kind, value)
        elif kind == "name":
            self.take("name")
            if value in {"true", "false", "null"}:
                result = self.node("literal", {"true": True, "false": False, "null": None}[value])
            elif self.token[0] == "(":
                result = self.node("call", value, self.arguments(value, GLOBALS, depth))
            else:
                source, name = "note", value
                if value in {"note", "formula", "file"}:
                    source = value
                    if self.token[0] == ".":
                        self.take(".")
                        name = self.take("name")
                    else:
                        self.take("[")
                        name = self.take("string")
                        self.take("]")
                elif value in {"this", "value", "index", "acc"}:
                    raise UnsupportedFormula("Ambient formula context is unavailable")
                if not name or len(name) > 256 or any(ord(c) < 32 for c in name):
                    raise UnsupportedFormula("Invalid property name")
                if source == "file" and name not in FILE_PROPERTIES:
                    raise UnsupportedFormula("Unsupported file property")
                result = self.node("property", (source, name))
        else:
            raise UnsupportedFormula("Expected formula value")
        while self.token[0] == ".":
            self.take(".")
            method = self.take("name")
            result = self.node("method", method, [result, *self.arguments(method, METHODS, depth)])
        while self.token[0] in PRECEDENCE and PRECEDENCE[self.token[0]] >= minimum:
            operator = self.token[0]
            self.take(operator)
            right = self.expression(PRECEDENCE[operator] + 1, depth + 1)
            result = self.node("binary", operator, [result, right])
        return result


def parse(source):
    parser = Parser(source)
    expression = parser.expression()
    parser.take("end")
    return expression


@dataclass(frozen=True)
class FormulaProgram:
    root: str
    expressions: tuple[tuple[str, Expression], ...]
    properties: frozenset[tuple[str, str]]


def compile_formula(formulas, name):
    """Prove the whole reachable syntax, including unselected branches.

    This does not claim the properties are readable. Callers must derive each
    used value from a verified source and retain all of its access references.
    """
    if not isinstance(formulas, dict) or not isinstance(name, str):
        raise UnsupportedFormula("Unavailable formula configuration")
    completed, visiting, properties = {}, set(), set()
    total = 0

    def formula(key, depth):
        nonlocal total
        if key in visiting or depth > MAX_DEPTH:
            raise UnsupportedFormula("Cyclic or deeply nested formula references")
        if key in completed:
            return
        if key not in formulas or len(completed) + len(visiting) >= MAX_FORMULAS:
            raise UnsupportedFormula("Unavailable or excessive formula references")
        visiting.add(key)
        tree = parse(formulas[key])
        pending = [tree]
        while pending:
            node = pending.pop()
            total += 1
            if total > MAX_TOTAL_NODES:
                raise UnsupportedFormula("Formula program exceeds work limit")
            if node.kind == "property":
                source, prop = node.value
                if source == "formula":
                    formula(prop, depth + 1)
                else:
                    properties.add((source, prop))
            pending.extend(node.args)
        visiting.remove(key)
        completed[key] = tree

    formula(name, 0)
    return FormulaProgram(name, tuple(completed.items()), frozenset(properties))


def supported(formulas, name):
    """Syntax eligibility only; no promise that a record's inputs are usable."""
    try:
        compile_formula(formulas, name)
        return True
    except UnsupportedFormula:
        return False
