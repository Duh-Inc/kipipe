"""Minimal KiCad S-expression parser/serializer.

Atoms are kept as strings so numbers round-trip exactly. Quoted strings are
returned as `Q` (a str subclass) so the serializer can re-quote them.
"""
from __future__ import annotations

import re


class Q(str):
    """A quoted string atom."""


_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)


def _unescape(s: str) -> str:
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def parse(text: str):
    stack: list[list] = [[]]
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip() == "":
                break
            raise ValueError(f"sexpr parse error at {pos}: {text[pos:pos+40]!r}")
        pos = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            if len(stack) < 2:
                raise ValueError("unbalanced ')'")
            lst = stack.pop()
            stack[-1].append(lst)
        elif m.group(3) is not None:
            stack[-1].append(Q(_unescape(m.group(3))))
        elif m.group(4) is not None:
            stack[-1].append(m.group(4))
    if len(stack) != 1:
        raise ValueError("unbalanced '('")
    top = stack[0]
    return top[0] if len(top) == 1 else top


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def dumps(node, indent: int = 0) -> str:
    if isinstance(node, Q):
        return _quote(node)
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float)):
        return fmt(node)
    # list
    simple = all(not isinstance(x, list) for x in node)
    if simple:
        return "(" + " ".join(dumps(x) for x in node) + ")"
    pad = "  " * (indent + 1)
    parts = []
    head = []
    for x in node:
        if isinstance(x, list):
            break
        head.append(dumps(x))
    out = "(" + " ".join(head)
    for x in node[len(head):]:
        if isinstance(x, list):
            parts.append("\n" + pad + dumps(x, indent + 1))
        else:
            parts.append(" " + dumps(x))
    return out + "".join(parts) + "\n" + "  " * indent + ")"


def fmt(v: float) -> str:
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# --- helpers -------------------------------------------------------------

def head(node):
    return node[0] if isinstance(node, list) and node else None


def find(node, name):
    for x in node:
        if isinstance(x, list) and x and x[0] == name:
            return x
    return None


def find_all(node, name):
    return [x for x in node if isinstance(x, list) and x and x[0] == name]


def walk(node):
    if isinstance(node, list):
        yield node
        for x in node:
            yield from walk(x)
