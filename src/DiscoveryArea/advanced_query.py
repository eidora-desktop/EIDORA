from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Mapping

import numpy as np


class AdvancedQueryError(ValueError):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.message = message
        self.position = max(0, int(position))


@dataclass(frozen=True)
class AdvancedQueryInfo:
    columns: frozenset[str]
    numeric_columns: frozenset[str]


@dataclass(frozen=True)
class _Token:
    kind: str
    value: object
    position: int


@dataclass(frozen=True)
class _Literal:
    kind: str
    value: object


@dataclass(frozen=True)
class _Predicate:
    field: str
    operator: str
    values: tuple[_Literal, ...] = ()


@dataclass(frozen=True)
class _Logical:
    operator: str
    left: object
    right: object


@dataclass(frozen=True)
class _Negation:
    operand: object


@dataclass(frozen=True)
class _ParsedQuery:
    root: object
    info: AdvancedQueryInfo


_NUMBER_PATTERN = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def inspect_advanced_query(expression: str) -> AdvancedQueryInfo:
    return _parse(expression).info


def validate_advanced_query(expression: str) -> None:
    _parse(expression)


def evaluate_advanced_query(
    expression: str,
    metadata_values: Mapping[str, np.ndarray],
    metadata_numeric_values: Mapping[str, np.ndarray],
    item_count: int,
) -> np.ndarray:
    parsed = _parse(expression)
    return np.asarray(
        _evaluate_node(
            parsed.root,
            metadata_values,
            metadata_numeric_values,
            max(0, int(item_count)),
        ),
        dtype=bool,
    )


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(expression):
        character = expression[position]
        if character.isspace():
            position += 1
            continue
        if character == "[":
            end = expression.find("]", position + 1)
            if end < 0:
                raise AdvancedQueryError("Missing closing ] for field name.", position)
            field = expression[position + 1 : end].strip()
            if not field:
                raise AdvancedQueryError("Field name cannot be empty.", position)
            tokens.append(_Token("FIELD", field, position))
            position = end + 1
            continue
        if character in {'"', "'"}:
            quote = character
            end = position + 1
            escaped = False
            while end < len(expression):
                current = expression[end]
                if current == quote and not escaped:
                    break
                if current == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False
                end += 1
            if end >= len(expression):
                raise AdvancedQueryError("Missing closing quote.", position)
            raw = expression[position : end + 1]
            try:
                value = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                raise AdvancedQueryError("Invalid text value.", position) from None
            tokens.append(_Token("STRING", str(value), position))
            position = end + 1
            continue
        two_char = expression[position : position + 2]
        if two_char in {"==", "!=", "<=", ">="}:
            tokens.append(_Token("OP", two_char, position))
            position += 2
            continue
        if character in {"<", ">"}:
            tokens.append(_Token("OP", character, position))
            position += 1
            continue
        if character == "(":
            tokens.append(_Token("LPAREN", character, position))
            position += 1
            continue
        if character == ")":
            tokens.append(_Token("RPAREN", character, position))
            position += 1
            continue
        if character == ",":
            tokens.append(_Token("COMMA", character, position))
            position += 1
            continue
        number = _NUMBER_PATTERN.match(expression, position)
        if number is not None:
            raw = number.group(0)
            tokens.append(_Token("NUMBER", float(raw), position))
            position = number.end()
            continue
        word = _WORD_PATTERN.match(expression, position)
        if word is not None:
            tokens.append(_Token("WORD", word.group(0).upper(), position))
            position = word.end()
            continue
        raise AdvancedQueryError(f"Unexpected character {character!r}.", position)
    tokens.append(_Token("EOF", "", len(expression)))
    return tokens


class _Parser:
    def __init__(self, expression: str) -> None:
        self._tokens = _tokenize(expression)
        self._index = 0
        self._columns: set[str] = set()
        self._numeric_columns: set[str] = set()

    def parse(self) -> _ParsedQuery:
        if self._peek().kind == "EOF":
            raise AdvancedQueryError("Enter a query before applying it.", 0)
        root = self._parse_or()
        token = self._peek()
        if token.kind != "EOF":
            raise AdvancedQueryError(f"Unexpected {self._describe(token)}.", token.position)
        return _ParsedQuery(
            root=root,
            info=AdvancedQueryInfo(
                columns=frozenset(self._columns),
                numeric_columns=frozenset(self._numeric_columns),
            ),
        )

    def _parse_or(self) -> object:
        node = self._parse_and()
        while self._accept_word("OR"):
            node = _Logical("OR", node, self._parse_and())
        return node

    def _parse_and(self) -> object:
        node = self._parse_not()
        while self._accept_word("AND"):
            node = _Logical("AND", node, self._parse_not())
        return node

    def _parse_not(self) -> object:
        if self._accept_word("NOT"):
            return _Negation(self._parse_not())
        return self._parse_primary()

    def _parse_primary(self) -> object:
        if self._accept("LPAREN") is not None:
            node = self._parse_or()
            self._require("RPAREN", "Missing closing parenthesis.")
            return node
        return self._parse_predicate()

    def _parse_predicate(self) -> _Predicate:
        field_token = self._require("FIELD", "Expected a metadata field such as [category].")
        field = str(field_token.value)
        self._columns.add(field)

        operator_token = self._peek()
        if operator_token.kind == "OP":
            self._advance()
            value = self._parse_value()
            if value.kind == "number":
                self._numeric_columns.add(field)
            return _Predicate(field, str(operator_token.value), (value,))
        if self._accept_word("CONTAINS"):
            return _Predicate(field, "CONTAINS", (self._parse_value(),))
        if self._accept_word("STARTS"):
            self._require_word("WITH", "Expected WITH after STARTS.")
            return _Predicate(field, "STARTS WITH", (self._parse_value(),))
        if self._accept_word("ENDS"):
            self._require_word("WITH", "Expected WITH after ENDS.")
            return _Predicate(field, "ENDS WITH", (self._parse_value(),))
        if self._accept_word("IN"):
            return _Predicate(field, "IN", self._parse_value_list())
        if self._accept_word("IS"):
            self._require_word("EMPTY", "Expected EMPTY after IS.")
            return _Predicate(field, "IS EMPTY")
        raise AdvancedQueryError("Expected a comparison operator after the field.", operator_token.position)

    def _parse_value_list(self) -> tuple[_Literal, ...]:
        self._require("LPAREN", "Expected ( before the value list.")
        values = [self._parse_value()]
        while self._accept("COMMA") is not None:
            values.append(self._parse_value())
        self._require("RPAREN", "Missing closing parenthesis for value list.")
        return tuple(values)

    def _parse_value(self) -> _Literal:
        token = self._peek()
        if token.kind == "STRING":
            self._advance()
            return _Literal("text", str(token.value))
        if token.kind == "NUMBER":
            self._advance()
            return _Literal("number", float(token.value))
        if token.kind == "WORD" and token.value in {"TRUE", "FALSE"}:
            self._advance()
            return _Literal("boolean", token.value == "TRUE")
        if token.kind == "WORD" and token.value == "NULL":
            self._advance()
            return _Literal("null", None)
        raise AdvancedQueryError("Expected a text, number, true, false, or null value.", token.position)

    def _peek(self) -> _Token:
        return self._tokens[self._index]

    def _advance(self) -> _Token:
        token = self._peek()
        self._index += 1
        return token

    def _accept(self, kind: str) -> _Token | None:
        if self._peek().kind != kind:
            return None
        return self._advance()

    def _accept_word(self, value: str) -> bool:
        token = self._peek()
        if token.kind != "WORD" or token.value != value:
            return False
        self._advance()
        return True

    def _require(self, kind: str, message: str) -> _Token:
        token = self._accept(kind)
        if token is None:
            current = self._peek()
            raise AdvancedQueryError(message, current.position)
        return token

    def _require_word(self, value: str, message: str) -> None:
        if not self._accept_word(value):
            raise AdvancedQueryError(message, self._peek().position)

    @staticmethod
    def _describe(token: _Token) -> str:
        if token.kind == "EOF":
            return "end of query"
        return repr(token.value)


def _parse(expression: str) -> _ParsedQuery:
    return _Parser(str(expression or "")).parse()


def _evaluate_node(
    node: object,
    text_values: Mapping[str, np.ndarray],
    numeric_values: Mapping[str, np.ndarray],
    item_count: int,
) -> np.ndarray:
    if isinstance(node, _Logical):
        left = _evaluate_node(node.left, text_values, numeric_values, item_count)
        right = _evaluate_node(node.right, text_values, numeric_values, item_count)
        return left & right if node.operator == "AND" else left | right
    if isinstance(node, _Negation):
        return ~_evaluate_node(node.operand, text_values, numeric_values, item_count)
    if isinstance(node, _Predicate):
        return _evaluate_predicate(node, text_values, numeric_values, item_count)
    return np.zeros(item_count, dtype=bool)


def _evaluate_predicate(
    predicate: _Predicate,
    text_values: Mapping[str, np.ndarray],
    numeric_values: Mapping[str, np.ndarray],
    item_count: int,
) -> np.ndarray:
    raw_values = text_values.get(predicate.field)
    if raw_values is None or np.asarray(raw_values).reshape(-1).size != item_count:
        return np.zeros(item_count, dtype=bool)
    values = np.asarray(raw_values, dtype=np.str_).reshape(-1)
    operator = predicate.operator
    if operator == "IS EMPTY":
        return np.char.strip(values) == ""
    if operator == "IN":
        result = np.zeros(item_count, dtype=bool)
        for literal in predicate.values:
            result |= _equals(values, numeric_values.get(predicate.field), literal)
        return result
    literal = predicate.values[0]
    if operator == "==":
        return _equals(values, numeric_values.get(predicate.field), literal)
    if operator == "!=":
        return ~_equals(values, numeric_values.get(predicate.field), literal)
    if operator in {"<", "<=", ">", ">="}:
        if literal.kind == "number":
            comparable = _numeric_array(values, numeric_values.get(predicate.field))
            expected: object = float(literal.value)
            valid = np.isfinite(comparable)
        else:
            comparable = values
            expected = _literal_text(literal)
            valid = np.ones(item_count, dtype=bool)
        result = np.zeros(item_count, dtype=bool)
        if operator == "<":
            result[valid] = comparable[valid] < expected
        elif operator == "<=":
            result[valid] = comparable[valid] <= expected
        elif operator == ">":
            result[valid] = comparable[valid] > expected
        else:
            result[valid] = comparable[valid] >= expected
        return result
    expected_text = _literal_text(literal)
    if operator == "CONTAINS":
        return np.char.find(values, expected_text) >= 0
    if operator == "STARTS WITH":
        return np.char.startswith(values, expected_text)
    if operator == "ENDS WITH":
        return np.char.endswith(values, expected_text)
    return np.zeros(item_count, dtype=bool)


def _equals(values: np.ndarray, numeric_values: np.ndarray | None, literal: _Literal) -> np.ndarray:
    if literal.kind == "number":
        numeric = _numeric_array(values, numeric_values)
        return np.isfinite(numeric) & (numeric == float(literal.value))
    if literal.kind == "boolean":
        expected = "true" if bool(literal.value) else "false"
        return np.char.lower(np.char.strip(values)) == expected
    if literal.kind == "null":
        return np.char.strip(values) == ""
    return values == str(literal.value)


def _numeric_array(values: np.ndarray, numeric_values: np.ndarray | None) -> np.ndarray:
    if numeric_values is not None:
        numeric = np.asarray(numeric_values, dtype=np.float64).reshape(-1)
        if numeric.shape == values.shape:
            return numeric
    numeric = np.full(values.shape[0], np.nan, dtype=np.float64)
    for index, value in enumerate(values):
        try:
            numeric[index] = float(value)
        except (TypeError, ValueError):
            pass
    return numeric


def _literal_text(literal: _Literal) -> str:
    if literal.kind == "boolean":
        return "true" if bool(literal.value) else "false"
    if literal.kind == "null":
        return ""
    return str(literal.value)
