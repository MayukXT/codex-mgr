"""Replace selected JSON values while retaining every other source character."""

from __future__ import annotations

import json
from decimal import Decimal


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are ambiguous.")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("Nonfinite JSON numbers are invalid.")


class JsonEdits:
    """Use the standard decoder for validation and locating scalar value spans."""

    def __init__(self, text: str):
        self.text = text
        self.decoder = json.JSONDecoder(
            object_pairs_hook=_unique_object, parse_float=Decimal, parse_constant=_reject_constant
        )
        self.start = self._space(1 if text.startswith("\ufeff") else 0)
        self.value, end = self.decoder.raw_decode(text, self.start)
        if self._space(end) != len(text):
            raise ValueError("Unexpected characters after JSON record.")
        self.edits: dict[tuple, tuple[int, int, str]] = {}

    def _space(self, position):
        while position < len(self.text) and self.text[position] in " \t\r\n":
            position += 1
        return position

    def _child(self, position, key):
        position = self._space(position)
        opening = self.text[position]
        if opening not in "{[":
            raise ValueError("JSON path does not identify a container.")
        position = self._space(position + 1)
        closing = "}" if opening == "{" else "]"
        index = 0
        while self.text[position] != closing:
            if opening == "{":
                current, position = self.decoder.raw_decode(self.text, position)
                position = self._space(position)
                if self.text[position] != ":":
                    raise ValueError("Invalid JSON member.")
                position = self._space(position + 1)
            else:
                current = index
            start = position
            _, end = self.decoder.raw_decode(self.text, start)
            if current == key:
                return start, end, None
            position = self._space(end)
            if self.text[position] == ",":
                position = self._space(position + 1)
            index += 1
        return None, None, position

    def set(self, path: tuple, value: str | None):
        if not path:
            raise ValueError("A JSON field path is required.")
        position = self.start
        current = self.value
        for index, key in enumerate(path):
            start, end, closing = self._child(position, key)
            if start is None:
                if (
                    index != len(path) - 1
                    or not isinstance(current, dict)
                    or not isinstance(key, str)
                ):
                    raise ValueError("JSON field path is missing.")
                prefix = (
                    ","
                    if current or any(edit[0] == closing for edit in self.edits.values())
                    else ""
                )
                replacement = prefix + json.dumps(key) + ":" + json.dumps(value, ensure_ascii=False)
                self.edits[path] = (closing, closing, replacement)
                return
            current = current[key]
            if index == len(path) - 1:
                if current != value:
                    self.edits[path] = (start, end, json.dumps(value, ensure_ascii=False))
                return
            position = start

    def render(self) -> str:
        text = self.text
        for start, end, replacement in sorted(self.edits.values(), reverse=True):
            text = text[:start] + replacement + text[end:]
        return text
