"""Pure bounded evaluation of compiled Bases expressions over verified inputs.

The caller owns source verification and authorization. This evaluator cannot read
files, resolve links, execute code or acquire authority; it can only request the
static property identities present in its compiled program. Non-finite results
and unverified type coercions remain unavailable rather than becoming null.
"""

import math
import re

from app.services.base_formula_expression import FormulaProgram, UnsupportedFormula

_NUMERIC = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_PREFIX = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
EMPTY = {"type": "empty"}
PRIMITIVES = {"empty", "number", "text", "boolean"}
_SPACE = " \t\r\n\u00a0"


def number(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise UnsupportedFormula("Non-finite calculation")
    return {"type": "number", "value": 0.0 if value == 0 else float(value)}


def text(value):
    if not isinstance(value, str) or len(value.encode("utf-16-le")) // 2 > 8192:
        raise UnsupportedFormula("Calculated text exceeds limit")
    return {"type": "text", "value": value}


def boolean(value):
    return {"type": "boolean", "value": bool(value)}


def truth(value):
    if value["type"] not in PRIMITIVES:
        raise UnsupportedFormula("Unverified truth conversion")
    return value["type"] != "empty" and bool(value["value"])


def numeric(value):
    if value["type"] != "number":
        raise UnsupportedFormula("Expected a numeric calculation input")
    return value["value"]


def string(value):
    if value["type"] != "text":
        raise UnsupportedFormula("Expected a text calculation input")
    return value["value"]


def comparable(value):
    if value["type"] == "number":
        return value["value"]
    if value["type"] == "boolean":
        return float(value["value"])
    if value["type"] == "text":
        candidate = value["value"].strip(_SPACE)
        if not candidate:
            return 0.0
        if _NUMERIC.fullmatch(candidate):
            return numeric(number(float(candidate)))
    raise UnsupportedFormula("Unverified comparison conversion")


def concatenated(value):
    if value["type"] == "empty":
        return ""
    if value["type"] == "text":
        return value["value"]
    if (
        value["type"] == "number"
        and float(value["value"]).is_integer()
        and abs(value["value"]) <= 2**53 - 1
    ):
        return str(int(value["value"]))
    raise UnsupportedFormula("Unverified text conversion")


def binary(operator, left, right):
    lk, rk = left["type"], right["type"]
    if operator in {"==", "!="}:
        if lk == "empty" or rk == "empty":
            equal = lk == rk
        elif lk == rk and lk in PRIMITIVES:
            equal = left["value"] == right["value"]
        else:
            equal = comparable(left) == comparable(right)
        return boolean(equal if operator == "==" else not equal)
    if operator in {">", ">=", "<", "<="}:
        if lk == "empty" or rk == "empty":
            return EMPTY
        if lk == rk == "text":
            # JS compares UTF-16 units, not Python's Unicode scalar ordering.
            a, b = (v["value"].encode("utf-16-be") for v in (left, right))
        else:
            a, b = comparable(left), comparable(right)
        return boolean({">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[operator])
    if operator == "+" and "text" in {lk, rk}:
        return text(concatenated(left) + concatenated(right))
    if lk == "empty" or rk == "empty":
        return EMPTY
    a, b = numeric(left), numeric(right)
    if operator in {"/", "%"} and b == 0:
        raise UnsupportedFormula("Non-finite calculation")
    if operator == "+":
        return number(a + b)
    if operator == "-":
        return number(a - b)
    if operator == "*":
        return number(a * b)
    if operator == "/":
        return number(a / b)
    if operator == "%":
        return number(math.fmod(a, b))
    raise UnsupportedFormula("Unsupported binary calculation")


def evaluate(program: FormulaProgram, property_value):
    """Return a typed value; never discard references collected by the caller.

    Property callbacks may return rich typed values for direct pass-through.
    Operations on them are enabled only after native parity has been established.
    Errors from source/access verification deliberately propagate to the caller.
    """
    expressions, memo, inputs = dict(program.expressions), {}, {}
    remaining = 4096

    def formula(name, depth):
        if name not in memo:
            memo[name] = run(expressions[name], depth + 1)
        return memo[name]

    def run(node, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 256:
            raise UnsupportedFormula("Calculation exceeds work limit")
        if node.kind == "number":
            return number(node.value)
        if node.kind == "string":
            return text(node.value)
        if node.kind == "literal":
            return EMPTY if node.value is None else boolean(node.value)
        if node.kind == "property":
            source, name = node.value
            if source == "formula":
                return formula(name, depth)
            if node.value not in program.properties:
                raise UnsupportedFormula("Uncompiled property input")
            if node.value not in inputs:
                inputs[node.value] = property_value(source, name)
            return inputs[node.value]
        if node.kind == "unary":
            value = run(node.args[0], depth + 1)
            if node.value == "!":
                return boolean(not truth(value))
            if value["type"] == "empty":
                return EMPTY
            return number(numeric(value) * (-1 if node.value == "-" else 1))
        if node.kind == "binary":
            left = run(node.args[0], depth + 1)
            if node.value in {"&&", "||"}:
                first = truth(left)
                if node.value == "&&" and not first or node.value == "||" and first:
                    return boolean(first)
                return boolean(truth(run(node.args[1], depth + 1)))
            return binary(node.value, left, run(node.args[1], depth + 1))
        if node.kind == "call" and node.value == "if":
            branch = 1 if truth(run(node.args[0], depth + 1)) else 2
            return run(node.args[branch], depth + 1) if branch < len(node.args) else EMPTY
        args = [run(arg, depth + 1) for arg in node.args]
        if node.kind == "call":
            if node.value in {"min", "max"}:
                numbers = []
                for arg in args:
                    # Native validates left-to-right and returns on null.
                    if arg["type"] == "empty":
                        return EMPTY
                    numbers.append(numeric(arg))
                return number((min if node.value == "min" else max)(numbers))
            if node.value == "number":
                value = args[0]
                if value["type"] in {"empty", "number"}:
                    return value
                if value["type"] == "boolean":
                    return number(int(value["value"]))
                match = _PREFIX.match(string(value).lstrip(_SPACE))
                if not match:
                    raise UnsupportedFormula("Invalid numeric text")
                return number(float(match.group()))
        if node.kind == "method":
            value, method = args[0], node.value
            if method == "isTruthy":
                return boolean(truth(value))
            if method == "isEmpty" and value["type"] in {"empty", "text", "number"}:
                return boolean(
                    value["type"] == "empty" or value["type"] == "text" and not value["value"]
                )
            if method == "isType" and value["type"] in PRIMITIVES:
                native = {
                    "empty": "null",
                    "text": "string",
                    "number": "number",
                    "boolean": "boolean",
                }
                return boolean(native[value["type"]] == string(args[1]))
            if value["type"] == "empty":
                return EMPTY
            if method in {"abs", "ceil", "floor"}:
                return number(
                    {"abs": abs, "ceil": math.ceil, "floor": math.floor}[method](numeric(value))
                )
            if method == "lower":
                return text(string(value).lower())
            if method in {"contains", "containsAll", "containsAny", "startsWith", "endsWith"}:
                source = string(value)
                needles = [string(arg) for arg in args[1:]]
                if method == "startsWith":
                    return boolean(source.startswith(needles[0]))
                if method == "endsWith":
                    return boolean(source.endswith(needles[0]))
                if method == "contains":
                    return boolean(bool(needles[0]) and needles[0] in source)
                return boolean(
                    (any if method == "containsAny" else all)(
                        needle in source for needle in needles
                    )
                )
        raise UnsupportedFormula("Unverified calculation operation")

    return formula(program.root, 0)
