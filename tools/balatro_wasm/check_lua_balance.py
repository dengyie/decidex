"""Rough Lua block-balance checker used to sanity-check bridge edits.

Not a full Lua parser: it counts function/if/for/while openers against `end`
after stripping comments and string literals. It is good enough to catch the
class of mistake that an unbalanced `end` would introduce in a patch file
that has no Lua interpreter available in CI.
"""

from __future__ import annotations

import re
import sys


def strip_noise(text: str) -> str:
    """Remove comments and string literals so keyword counting is reliable."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("--[[", i):
            j = text.find("]]", i + 4)
            i = (j + 2) if j != -1 else n
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = j if j != -1 else n
            continue
        if text.startswith("[[", i):
            j = text.find("]]", i + 2)
            i = (j + 2) if j != -1 else n
            continue
        ch = text[i]
        if ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def check(path: str) -> int:
    src = open(path, encoding="utf-8").read()
    clean = strip_noise(src)

    # `function` opens a block, `if`/`for`/`while` open a block that their
    # matching `end` closes. `do` inside `for ... do` / `while ... do` is the
    # same block, so it must not be counted separately.
    openers = len(re.findall(r"\b(?:function|if|for|while)\b", clean))
    ends = len(re.findall(r"\bend\b", clean))

    # `elseif ... then` / `else` do not open new blocks, but `elseif` contains
    # no `end` either, so they are already neutral.
    reopens = len(re.findall(r"\brepeat\b", clean))
    untils = len(re.findall(r"\buntil\b", clean))
    if reopens != untils:
        print(f"repeat/until mismatch: {reopens} repeat vs {untils} until")

    delta = openers - ends
    print(f"{path}: {openers} block openers, {ends} ends, delta={delta}")
    if delta != 0:
        print("  !! unbalanced blocks")
    return delta


if __name__ == "__main__":
    total = 0
    for p in sys.argv[1:]:
        total += check(p)
    sys.exit(0 if total == 0 else 1)
