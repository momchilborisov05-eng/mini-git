r"""A compact lane-based ASCII commit graph for ``mgit log --graph``.

Each column ("lane") holds the id of the commit expected next in that lane. A commit takes the
leftmost lane waiting for it; lanes that were also waiting for it collapse into it (``|/``);
extra parents of a merge open new lanes to the right (``|\``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


def _lanes(cols: list[str], mark: int | None = None) -> str:
    return " ".join("*" if i == mark else "|" for i in range(len(cols)))


def graph_lines(rows: Iterable[tuple[str, list[str], list[str]]]) -> Iterator[str]:
    """``rows`` are ``(sha, parents, text_lines)`` in display order; yields output lines."""
    cols: list[str] = []
    for sha, parents, text in rows:
        if sha not in cols:
            cols.append(sha)
        dups = [i for i, c in enumerate(cols) if c == sha]
        if len(dups) > 1:  # several lanes converge on this commit
            first = dups[0]
            chars = [" "] * (2 * len(cols) - 1)
            shift = False
            for i in range(len(cols)):
                if i in dups[1:]:
                    shift = True
                if shift:
                    chars[2 * i - 1] = "/"
                else:
                    chars[2 * i] = "|"
            yield "".join(chars).rstrip()
            cols = [c for i, c in enumerate(cols) if i == first or c != sha]
        idx = cols.index(sha)
        prefix = _lanes(cols, idx)
        yield f"{prefix} {text[0]}" if text else prefix

        # update lanes for the next row
        new_parents = [p for p in parents[1:] if p not in cols]
        if parents:
            cols[idx] = parents[0]
        else:
            cols.pop(idx)
        for k, p in enumerate(new_parents):
            cols.insert(idx + 1 + k, p)
        if new_parents:
            left = " ".join("|" for _ in range(idx + 1))
            shifted = len(cols) - (idx + 1) - len(new_parents)  # old lanes pushed to the right
            yield left + "\\" + " \\" * shifted
        elif not parents and idx < len(cols):
            chars = [" "] * (2 * (len(cols) + 1) - 1)
            for i in range(len(cols) + 1):
                if i < idx:
                    chars[2 * i] = "|"
                elif i > idx:
                    chars[2 * i - 1] = "/"
            yield "".join(chars).rstrip()
        for extra in text[1:]:
            yield f"{_lanes(cols)} {extra}" if cols else f"  {extra}"
