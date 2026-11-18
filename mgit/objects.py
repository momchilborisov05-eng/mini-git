"""Content-addressed object store and the four object types (blob, tree, commit, tag).

The on-disk format matches git's loose objects:

    zlib( b"<type> <size>\\0" + payload )   stored at objects/<sha[:2]>/<sha[2:]>

where ``sha`` is the SHA-1 of the *uncompressed* framed bytes. Identical content therefore
always maps to the same id and is stored exactly once.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from .errors import MgitError

OBJECT_TYPES = ("blob", "tree", "commit", "tag")
MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_TREE = "40000"
SHA_HEX_LEN = 40
COMPRESSION_LEVEL = 6


def frame(obj_type: str, payload: bytes) -> bytes:
    """Prefix ``payload`` with git's ``"<type> <size>\\0"`` header."""
    return f"{obj_type} {len(payload)}".encode() + b"\0" + payload


def hash_object(obj_type: str, payload: bytes) -> str:
    """Return the object id ``payload`` would get, without storing it."""
    return hashlib.sha1(frame(obj_type, payload)).hexdigest()


class ObjectStore:
    """Loose-object database under ``.mgit/objects``."""

    def __init__(self, root: str):
        self.root = root

    def path_for(self, sha: str) -> str:
        return os.path.join(self.root, sha[:2], sha[2:])

    def exists(self, sha: str) -> bool:
        return len(sha) == SHA_HEX_LEN and os.path.isfile(self.path_for(sha))

    def write(self, obj_type: str, payload: bytes) -> str:
        if obj_type not in OBJECT_TYPES:
            raise ValueError(f"unknown object type {obj_type!r}")
        raw = frame(obj_type, payload)
        sha = hashlib.sha1(raw).hexdigest()
        path = self.path_for(sha)
        if os.path.exists(path):  # deduplication: same content, same id, nothing to do
            return sha
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(zlib.compress(raw, COMPRESSION_LEVEL))
            os.replace(tmp, path)  # atomic: readers never see a half-written object
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return sha

    def read(self, sha: str) -> tuple[str, bytes]:
        """Return ``(type, payload)``; verifies size and hash so corruption is detected."""
        try:
            with open(self.path_for(sha), "rb") as fh:
                raw = zlib.decompress(fh.read())
        except FileNotFoundError:
            raise MgitError(f"object {sha} not found") from None
        except zlib.error as exc:
            raise MgitError(f"object {sha} is corrupt: {exc}") from None
        header, sep, payload = raw.partition(b"\0")
        try:
            obj_type, size = header.decode("ascii").split(" ")
            size_ok = int(size) == len(payload)
        except ValueError:
            size_ok = False
        if not sep or not size_ok or hashlib.sha1(raw).hexdigest() != sha:
            raise MgitError(f"object {sha} is corrupt (header or hash mismatch)")
        return obj_type, payload

    def read_typed(self, sha: str, expected: str) -> bytes:
        obj_type, payload = self.read(sha)
        if obj_type != expected:
            raise MgitError(f"object {sha[:7]} is a {obj_type}, not a {expected}")
        return payload

    def find_prefix(self, prefix: str) -> list[str]:
        """All object ids starting with ``prefix`` (at least 4 hex chars)."""
        prefix = prefix.lower()
        directory = os.path.join(self.root, prefix[:2])
        if len(prefix) < 4 or not os.path.isdir(directory):
            return []
        rest = prefix[2:]
        return sorted(prefix[:2] + name for name in os.listdir(directory) if name.startswith(rest) and not name.startswith(".tmp"))

    def iter_ids(self) -> Iterator[str]:
        if not os.path.isdir(self.root):
            return
        for d in sorted(os.listdir(self.root)):
            full = os.path.join(self.root, d)
            if len(d) == 2 and os.path.isdir(full):
                for name in sorted(os.listdir(full)):
                    if not name.startswith(".tmp"):
                        yield d + name


# --------------------------------------------------------------------------- trees


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    sha: str

    @property
    def is_tree(self) -> bool:
        return self.mode == MODE_TREE


def _tree_sort_key(entry: TreeEntry) -> bytes:
    # git sorts directory names as if they ended in "/" so "a.txt" < "a/" < "a0"
    return entry.name.encode() + (b"/" if entry.is_tree else b"")


def serialize_tree(entries: list[TreeEntry]) -> bytes:
    out = bytearray()
    for e in sorted(entries, key=_tree_sort_key):
        if "/" in e.name or e.name in ("", ".", ".."):
            raise ValueError(f"invalid tree entry name {e.name!r}")
        out += f"{e.mode} {e.name}".encode() + b"\0" + bytes.fromhex(e.sha)
    return bytes(out)


def parse_tree(payload: bytes) -> list[TreeEntry]:
    entries, i = [], 0
    while i < len(payload):
        space = payload.index(b" ", i)
        nul = payload.index(b"\0", space)
        mode = payload[i:space].decode()
        name = payload[space + 1 : nul].decode()
        sha = payload[nul + 1 : nul + 21].hex()
        entries.append(TreeEntry(mode, name, sha))
        i = nul + 21
    return entries


# --------------------------------------------------------------------------- commits and tags


@dataclass
class Signature:
    name: str
    email: str
    timestamp: int
    tz: str  # "+0200"

    def __str__(self) -> str:
        return f"{self.name} <{self.email}> {self.timestamp} {self.tz}"

    @classmethod
    def parse(cls, text: str) -> Signature:
        lt, gt = text.index("<"), text.index(">")
        ts, tz = text[gt + 1 :].split()
        return cls(text[:lt].strip(), text[lt + 1 : gt], int(ts), tz)


def _parse_headers(payload: bytes) -> tuple[list[tuple[str, str]], str]:
    text = payload.decode("utf-8", errors="replace")
    head, _, message = text.partition("\n\n")
    headers = []
    for line in head.split("\n"):
        key, _, value = line.partition(" ")
        headers.append((key, value))
    return headers, message


@dataclass
class Commit:
    tree: str
    parents: list[str]
    author: Signature
    committer: Signature
    message: str

    def serialize(self) -> bytes:
        lines = [f"tree {self.tree}"]
        lines += [f"parent {p}" for p in self.parents]
        lines += [f"author {self.author}", f"committer {self.committer}"]
        message = self.message if self.message.endswith("\n") else self.message + "\n"
        return ("\n".join(lines) + "\n\n" + message).encode("utf-8")

    @classmethod
    def parse(cls, payload: bytes) -> Commit:
        headers, message = _parse_headers(payload)
        values: dict[str, list[str]] = {}
        for key, value in headers:
            values.setdefault(key, []).append(value)
        return cls(
            tree=values["tree"][0],
            parents=values.get("parent", []),
            author=Signature.parse(values["author"][0]),
            committer=Signature.parse(values["committer"][0]),
            message=message,
        )

    @property
    def summary(self) -> str:
        return self.message.strip().split("\n", 1)[0]


@dataclass
class Tag:
    object: str
    type: str
    tag: str
    tagger: Signature
    message: str = field(default="")

    def serialize(self) -> bytes:
        message = self.message if self.message.endswith("\n") else self.message + "\n"
        head = f"object {self.object}\ntype {self.type}\ntag {self.tag}\ntagger {self.tagger}\n"
        return (head + "\n" + message).encode("utf-8")

    @classmethod
    def parse(cls, payload: bytes) -> Tag:
        headers, message = _parse_headers(payload)
        h = dict(headers)
        return cls(h["object"], h["type"], h["tag"], Signature.parse(h["tagger"]), message)
