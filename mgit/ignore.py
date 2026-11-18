"""``.mgitignore`` rules: a practical subset of gitignore syntax.

Supported: blank lines and ``#`` comments, ``*``, ``?``, ``[abc]``, ``**`` (any number of
directories), a trailing ``/`` (directories only), a leading or inner ``/`` (anchored to the
repository root) and ``!`` negation. The last matching rule wins. ``.mgit/`` is always ignored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

IGNORE_FILE = ".mgitignore"


def _translate(glob: str) -> str:
    """Translate a gitignore-style glob into a regular expression (no anchors)."""
    out, i, n = [], 0, len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("/**", i) and i + 3 == n:
            out.append("/.*")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = glob[i + 1 : j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class Rule:
    pattern: str
    regex: re.Pattern
    negate: bool
    dir_only: bool
    anchored: bool


def parse_rule(line: str) -> Rule | None:
    raw = line.rstrip("\n").rstrip("\r")
    if not raw.strip() or raw.lstrip().startswith("#"):
        return None
    pattern = raw.strip()
    negate = pattern.startswith("!")
    if negate:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    if not pattern:
        return None
    return Rule(raw, re.compile(_translate(pattern)), negate, dir_only, anchored)


class IgnoreRules:
    def __init__(self, lines: list[str] | None = None):
        self.rules = [r for r in (parse_rule(line) for line in (lines or [])) if r]

    @classmethod
    def load(cls, root: str) -> IgnoreRules:
        path = os.path.join(root, IGNORE_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                return cls(fh.readlines())
        except FileNotFoundError:
            return cls()

    def _match(self, rel: str, is_dir: bool) -> bool:
        ignored = False
        base = rel.rsplit("/", 1)[-1]
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            target = rel if rule.anchored else base
            if rule.regex.fullmatch(target):
                ignored = not rule.negate
        return ignored

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        """True if ``rel`` (posix path relative to the root) or any parent directory is ignored."""
        parts = rel.split("/")
        if parts[0] == ".mgit":
            return True
        for i in range(1, len(parts)):
            if self._match("/".join(parts[:i]), True):
                return True
        return self._match(rel, is_dir)
