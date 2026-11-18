"""The staging area ("index"): the snapshot that the next commit will record.

Stored as JSON in ``.mgit/index`` (git uses a binary format; JSON keeps it inspectable).
Each entry caches the file's size and mtime so ``status`` can skip re-hashing unchanged files.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass

INDEX_VERSION = 1
# Entries whose mtime is this close to the moment the index is written are "racily clean":
# a same-size edit in the same timestamp tick would be invisible to the stat check, so their
# cached mtime is zeroed to force a re-hash next time (the same trick git uses).
RACY_WINDOW_NS = 2_000_000_000


@dataclass
class IndexEntry:
    sha: str
    mode: str
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, sha: str, mode: str, st: os.stat_result) -> IndexEntry:
        return cls(sha, mode, st.st_size, st.st_mtime_ns)

    def stat_matches(self, st: os.stat_result) -> bool:
        return self.mtime_ns != 0 and self.size == st.st_size and self.mtime_ns == st.st_mtime_ns


class Index:
    def __init__(self, path: str):
        self.path = path
        self.entries: dict[str, IndexEntry] = {}

    @classmethod
    def load(cls, path: str) -> Index:
        idx = cls(path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return idx
        for rel, e in data.get("entries", {}).items():
            idx.entries[rel] = IndexEntry(**e)
        return idx

    def save(self) -> None:
        now = time.time_ns()
        for e in self.entries.values():
            if e.mtime_ns >= now - RACY_WINDOW_NS:
                e.mtime_ns = 0
        data = {
            "version": INDEX_VERSION,
            "entries": {k: asdict(self.entries[k]) for k in sorted(self.entries)},
        }
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".index-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.path)

    def as_map(self) -> dict[str, tuple[str, str]]:
        """``{path: (mode, sha)}`` - the same shape as a flattened tree."""
        return {p: (e.mode, e.sha) for p, e in self.entries.items()}
