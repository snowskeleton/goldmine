"""Parser for WoW SavedVariables files (a restricted subset of Lua table syntax).

Handles what the WoW client writes: top-level `Name = value` assignments, nested
tables with `["key"] =`, `[123] =`, `key =` and positional entries, strings with Lua
escapes, numbers, booleans, nil, and `--` / `--[[ ]]` comments.

Tables whose keys are exactly 1..n become lists; everything else becomes a dict.
"""

from __future__ import annotations

import re

__all__ = ["parse", "parse_file", "ParseError"]


class ParseError(ValueError):
    pass


_NUMBER = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
            "\\": "\\", '"': '"', "'": "'", "\n": "\n"}


def _decode(raw: str) -> str:
    # The parser works on latin-1 text so each char is one byte; WoW strings are UTF-8.
    return raw.encode("latin-1").decode("utf-8", errors="replace")


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def error(self, msg: str) -> ParseError:
        line = self.s.count("\n", 0, self.i) + 1
        return ParseError(f"{msg} at line {line}")

    def skip(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n;":
                self.i += 1
            elif s.startswith("--", self.i):
                m = re.compile(r"--\[(=*)\[").match(s, self.i)
                if m:
                    end = s.find("]" + m.group(1) + "]", m.end())
                    self.i = n if end < 0 else end + len(m.group(1)) + 2
                else:
                    end = s.find("\n", self.i)
                    self.i = n if end < 0 else end + 1
            else:
                break

    def peek(self) -> str:
        self.skip()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise self.error(f"expected {ch!r}, got {self.peek()!r}")
        self.i += 1

    def value(self):
        c = self.peek()
        if c == "{":
            return self.table()
        if c in "\"'":
            return self.string()
        if c == "[" and re.compile(r"\[=*\[").match(self.s, self.i):
            return self.long_string()
        m = _NUMBER.match(self.s, self.i)
        if m:
            self.i = m.end()
            text = m.group(0)
            if "x" in text.lower():
                return int(text, 16)
            if re.fullmatch(r"-?\d+", text):
                return int(text)
            return float(text)
        m = _NAME.match(self.s, self.i)
        if m:
            word = m.group(0)
            self.i = m.end()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "nil":
                return None
            if word in ("inf", "nan"):
                return float(word)
            raise self.error(f"unexpected identifier {word!r}")
        if self.s.startswith("-inf", self.i):
            self.i += 4
            return float("-inf")
        raise self.error(f"unexpected character {c!r}")

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out = []
        s = self.s
        while True:
            if self.i >= len(s):
                raise self.error("unterminated string")
            c = s[self.i]
            if c == quote:
                self.i += 1
                return _decode("".join(out))
            if c == "\\":
                self.i += 1
                e = s[self.i]
                if e in _ESCAPES:
                    out.append(_ESCAPES[e])
                    self.i += 1
                elif e.isdigit():
                    m = re.compile(r"\d{1,3}").match(s, self.i)
                    out.append(chr(int(m.group(0))))
                    self.i = m.end()
                else:
                    raise self.error(f"bad escape \\{e}")
            else:
                out.append(c)
                self.i += 1

    def long_string(self) -> str:
        m = re.compile(r"\[(=*)\[\n?").match(self.s, self.i)
        close = "]" + m.group(1) + "]"
        end = self.s.find(close, m.end())
        if end < 0:
            raise self.error("unterminated long string")
        self.i = end + len(close)
        return _decode(self.s[m.end():end])

    def table(self):
        self.expect("{")
        result: dict = {}
        pos = 1
        while True:
            c = self.peek()
            if c == "}":
                self.i += 1
                break
            if c == "[" and not re.compile(r"\[=*\[").match(self.s, self.i):
                self.i += 1
                key = self.value()
                self.expect("]")
                self.expect("=")
                result[key] = self.value()
            else:
                m = _NAME.match(self.s, self.i)
                after = m and re.compile(r"\s*=(?!=)").match(self.s, m.end())
                if m and after and m.group(0) not in ("true", "false", "nil"):
                    self.i = after.end()
                    result[m.group(0)] = self.value()
                else:
                    result[pos] = self.value()
                    pos += 1
            if self.peek() in ",;":
                self.i += 1
        n = len(result)
        if n and all(isinstance(k, int) and not isinstance(k, bool) for k in result) \
                and set(result) == set(range(1, n + 1)):
            return [result[k] for k in range(1, n + 1)]
        return result

    def chunk(self) -> dict:
        out = {}
        while self.peek():
            m = _NAME.match(self.s, self.i)
            if not m:
                raise self.error("expected variable name")
            self.i = m.end()
            self.expect("=")
            out[m.group(0)] = self.value()
        return out


def parse(data: bytes | str) -> dict:
    """Parse SavedVariables content into {variable_name: python_value}."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _Parser(data.decode("latin-1")).chunk()


def parse_file(path) -> dict:
    with open(path, "rb") as f:
        return parse(f.read())
