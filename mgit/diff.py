"""Line diff (Myers' O(ND) algorithm) and unified-format output, written from scratch.

``myers_diff`` finds a shortest edit script between two sequences: the fewest insertions and
deletions that turn ``a`` into ``b``. It explores diagonals ``k = x - y`` in rounds ``d = 0, 1,
2...``, keeping only the furthest-reaching path on each diagonal, and records each round so the
path can be walked back afterwards. Common prefixes and suffixes are trimmed first, which makes
the typical "small edit in a big file" case cost almost nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

EQUAL, DELETE, INSERT = " ", "-", "+"
NO_NEWLINE = "\\ No newline at end of file"


@dataclass(frozen=True)
class Op:
    tag: str  # " ", "-" or "+"
    line: str
    a_index: int  # position in a (or -1 for insertions)
    b_index: int  # position in b (or -1 for deletions)


def _myers_core(a: Sequence[int], b: Sequence[int]) -> list[tuple[str, int, int]]:
    n, m = len(a), len(b)
    if n == 0:
        return [(INSERT, -1, j) for j in range(m)]
    if m == 0:
        return [(DELETE, i, -1) for i in range(n)]
    max_d = n + m
    off = max_d + 1  # v[off + k] = furthest x reached on diagonal k
    v = [0] * (2 * max_d + 3)
    trace: list[list[int]] = []  # trace[d] = v[k] for k in [-d-1, d+1] before round d
    for d in range(max_d + 1):
        trace.append(v[off - d - 1 : off + d + 2])
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v[off + k - 1] < v[off + k + 1]):
                x = v[off + k + 1]  # step down: insertion
            else:
                x = v[off + k - 1] + 1  # step right: deletion
            y = x - k
            while x < n and y < m and a[x] == b[y]:  # follow the "snake" of equal lines
                x += 1
                y += 1
            v[off + k] = x
            if x >= n and y >= m:
                return _backtrack(trace, n, m)
    raise AssertionError("unreachable")  # pragma: no cover


def _backtrack(trace: list[list[int]], n: int, m: int) -> list[tuple[str, int, int]]:
    ops: list[tuple[str, int, int]] = []
    x, y = n, m
    for d in range(len(trace) - 1, -1, -1):
        snap = trace[d]
        k = x - y
        base = d + 1  # snap[k + base] is v[k]
        if k == -d or (k != d and snap[k - 1 + base] < snap[k + 1 + base]):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = snap[prev_k + base]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            ops.append((EQUAL, x - 1, y - 1))
            x, y = x - 1, y - 1
        if d > 0:
            if x == prev_x:
                ops.append((INSERT, -1, y - 1))
            else:
                ops.append((DELETE, x - 1, -1))
        x, y = prev_x, prev_y
    ops.reverse()
    return ops


def myers_diff(a: Sequence[str], b: Sequence[str]) -> list[Op]:
    """Shortest edit script from ``a`` to ``b`` as a list of :class:`Op`."""
    # Intern lines as small ints so comparisons inside the hot loop are cheap.
    ids: dict[str, int] = {}
    ai = [ids.setdefault(line, len(ids)) for line in a]
    bi = [ids.setdefault(line, len(ids)) for line in b]
    n, m = len(a), len(b)
    pre = 0
    while pre < n and pre < m and ai[pre] == bi[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and ai[n - 1 - suf] == bi[m - 1 - suf]:
        suf += 1
    core = _myers_core(ai[pre : n - suf], bi[pre : m - suf])
    ops = [Op(EQUAL, a[i], i, i) for i in range(pre)]
    for tag, i, j in core:
        ia = i + pre if i >= 0 else -1
        jb = j + pre if j >= 0 else -1
        ops.append(Op(tag, a[ia] if tag != INSERT else b[jb], ia, jb))
    ops += [Op(EQUAL, a[n - suf + s], n - suf + s, m - suf + s) for s in range(suf)]
    return ops


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    return sum(1 for op in myers_diff(a, b) if op.tag != EQUAL)


def matching_pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int]]:
    """``(i, j)`` pairs with ``a[i] == b[j]`` on the shortest edit path (strictly increasing)."""
    return [(op.a_index, op.b_index) for op in myers_diff(a, b) if op.tag == EQUAL]


def split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _range(start: int, length: int) -> str:
    # unified format: a zero-length range points at the line *before* the change
    if length == 0:
        return f"{start - 1 if start > 0 else 0},0"
    return str(start) if length == 1 else f"{start},{length}"


def unified_hunks(a: Sequence[str], b: Sequence[str], context: int = 3) -> list[str]:
    """Hunks (``@@`` headers and body lines, without trailing newlines) in unified format."""
    ops = myers_diff(a, b)
    changes = [i for i, op in enumerate(ops) if op.tag != EQUAL]
    if not changes:
        return []
    # Group changes whose distance is small enough that their contexts touch.
    groups: list[tuple[int, int]] = []
    start = end = changes[0]
    for idx in changes[1:]:
        if idx - end > 2 * context:
            groups.append((start, end))
            start = idx
        end = idx
    groups.append((start, end))

    # Line numbers (1-based) before each op.
    a_no, b_no, numbers = 1, 1, []
    for op in ops:
        numbers.append((a_no, b_no))
        if op.tag != INSERT:
            a_no += 1
        if op.tag != DELETE:
            b_no += 1

    out: list[str] = []
    for g_start, g_end in groups:
        lo = max(0, g_start - context)
        hi = min(len(ops), g_end + context + 1)
        chunk = ops[lo:hi]
        a_len = sum(1 for op in chunk if op.tag != INSERT)
        b_len = sum(1 for op in chunk if op.tag != DELETE)
        a_start, b_start = numbers[lo]
        out.append(f"@@ -{_range(a_start, a_len)} +{_range(b_start, b_len)} @@")
        for op in chunk:
            line = op.line
            out.append(op.tag + line.rstrip("\r\n") if line.endswith("\n") else op.tag + line)
            if not line.endswith("\n"):
                out.append(NO_NEWLINE)
    return out


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")
