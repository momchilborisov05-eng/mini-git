"""Three-way merge of text (diff3 style) built on the Myers diff.

Both sides are diffed against the common ancestor (``base``). Base lines that survived
unchanged in *both* sides are "sync points"; between two sync points each side either kept the
base chunk, changed it, or both changed it. Only the last case is a conflict - unless both
sides made the very same change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .diff import matching_pairs


@dataclass
class MergeResult:
    lines: list[str]
    conflicts: int

    @property
    def clean(self) -> bool:
        return self.conflicts == 0


def _ensure_nl(chunk: list[str]) -> list[str]:
    if chunk and not chunk[-1].endswith("\n"):
        return chunk[:-1] + [chunk[-1] + "\n"]
    return chunk


def merge3(
    base: Sequence[str],
    ours: Sequence[str],
    theirs: Sequence[str],
    ours_label: str = "ours",
    theirs_label: str = "theirs",
) -> MergeResult:
    in_ours = dict(matching_pairs(base, ours))
    in_theirs = dict(matching_pairs(base, theirs))
    sync = [(i, in_ours[i], in_theirs[i]) for i in range(len(base)) if i in in_ours and i in in_theirs]
    sync.append((len(base), len(ours), len(theirs)))  # sentinel: flush the tail

    out: list[str] = []
    conflicts = 0
    bi = oi = ti = 0
    for sb, so, st in sync:
        b_chunk, o_chunk, t_chunk = list(base[bi:sb]), list(ours[oi:so]), list(theirs[ti:st])
        if o_chunk == b_chunk:
            out += t_chunk  # only theirs changed (or nobody did)
        elif t_chunk == b_chunk or o_chunk == t_chunk:
            out += o_chunk  # only ours changed, or both made the same change
        else:
            conflicts += 1
            out.append(f"<<<<<<< {ours_label}\n")
            out += _ensure_nl(o_chunk)
            out.append("=======\n")
            out += _ensure_nl(t_chunk)
            out.append(f">>>>>>> {theirs_label}\n")
        if sb < len(base):
            out.append(base[sb])
        bi, oi, ti = sb + 1, so + 1, st + 1
    return MergeResult(out, conflicts)
