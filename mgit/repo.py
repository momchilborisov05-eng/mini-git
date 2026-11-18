"""Repository plumbing: locating ``.mgit``, refs and HEAD, trees <-> flat maps, the working
tree scan, revision parsing and ``status``.

Paths inside the repository are always POSIX-style strings relative to the root (``src/a.py``),
whatever the host OS; they are converted to native paths only when touching the filesystem.
"""

from __future__ import annotations

import configparser
import getpass
import os
import re
import stat
import time
from dataclasses import dataclass, field

from .errors import MgitError, NotARepositoryError
from .ignore import IgnoreRules
from .index import Index, IndexEntry
from .objects import (
    MODE_EXEC,
    MODE_FILE,
    MODE_TREE,
    Commit,
    ObjectStore,
    Signature,
    Tag,
    TreeEntry,
    hash_object,
    parse_tree,
    serialize_tree,
)

MGIT_DIR = ".mgit"
DEFAULT_BRANCH = "main"
FlatTree = dict[str, tuple[str, str]]  # path -> (mode, sha)

_REF_NAME = re.compile(r"^(?!.*\.\.)(?!.*//)(?!/)(?!.*/$)(?!.*\.lock$)[A-Za-z0-9._/-]+$")


def valid_ref_name(name: str) -> bool:
    return bool(_REF_NAME.match(name)) and not name.startswith("-") and name != "HEAD"


@dataclass
class Status:
    branch: str | None
    head: str | None
    staged: dict[str, str] = field(default_factory=dict)  # path -> "added" | "modified" | "deleted"
    unstaged: dict[str, str] = field(default_factory=dict)  # path -> "modified" | "deleted"
    untracked: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    merging: str | None = None

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.conflicts)


class Repository:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.mgit_dir = os.path.join(self.root, MGIT_DIR)
        self.store = ObjectStore(os.path.join(self.mgit_dir, "objects"))

    # ------------------------------------------------------------------ creation / discovery

    @classmethod
    def init(cls, path: str = ".", branch: str = DEFAULT_BRANCH) -> tuple[Repository, bool]:
        """Create (or re-initialise) a repository. Returns ``(repo, already_existed)``."""
        repo = cls(path)
        existed = os.path.isdir(repo.mgit_dir)
        if not valid_ref_name(branch):
            raise MgitError(f"'{branch}' is not a valid branch name")
        for sub in ("objects", "refs/heads", "refs/tags"):
            os.makedirs(os.path.join(repo.mgit_dir, *sub.split("/")), exist_ok=True)
        if not os.path.exists(repo.git_path("HEAD")):
            repo._write(repo.git_path("HEAD"), f"ref: refs/heads/{branch}\n")
        if not os.path.exists(repo.git_path("config")):
            cfg = configparser.ConfigParser()
            cfg["core"] = {"repositoryformatversion": "0", "hash": "sha1"}
            with open(repo.git_path("config"), "w", encoding="utf-8") as fh:
                cfg.write(fh)
        return repo, existed

    @classmethod
    def discover(cls, start: str | None = None) -> Repository:
        path = os.path.abspath(start or os.getcwd())
        current = path
        while True:
            if os.path.isdir(os.path.join(current, MGIT_DIR)):
                return cls(current)
            parent = os.path.dirname(current)
            if parent == current:
                raise NotARepositoryError(path)
            current = parent

    # ------------------------------------------------------------------ small helpers

    def git_path(self, *parts: str) -> str:
        return os.path.join(self.mgit_dir, *parts)

    def abspath(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def to_rel(self, user_path: str, cwd: str | None = None) -> str:
        """Convert a user-supplied path (relative to ``cwd``) to a repo-relative POSIX path."""
        full = os.path.abspath(os.path.join(cwd or os.getcwd(), user_path))
        try:
            rel = os.path.relpath(full, self.root)
        except ValueError:  # different drive on Windows
            raise MgitError(f"'{user_path}' is outside the repository") from None
        if rel == os.curdir:
            return ""
        rel = rel.replace(os.sep, "/")
        if rel == ".." or rel.startswith("../"):
            raise MgitError(f"'{user_path}' is outside the repository")
        if rel == MGIT_DIR or rel.startswith(MGIT_DIR + "/"):
            raise MgitError(f"'{user_path}' is inside the repository's {MGIT_DIR} directory")
        return rel

    @staticmethod
    def _write(path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".lock"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)

    @staticmethod
    def _read(path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------------ config and identity

    def _config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read(self.git_path("config"), encoding="utf-8")
        return cfg

    def config_get(self, key: str) -> str | None:
        section, _, name = key.rpartition(".")
        cfg = self._config()
        if not section or not cfg.has_option(section, name):
            return None
        return cfg.get(section, name)

    def config_set(self, key: str, value: str) -> None:
        section, _, name = key.rpartition(".")
        if not section or not name:
            raise MgitError(f"invalid config key '{key}' (expected section.name)")
        cfg = self._config()
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, name, value)
        with open(self.git_path("config"), "w", encoding="utf-8") as fh:
            cfg.write(fh)

    def signature(self, kind: str = "AUTHOR") -> Signature:
        """Identity from MGIT_<kind>_NAME/EMAIL (committer falls back to author), repo config, OS user."""
        env = os.environ
        name = env.get(f"MGIT_{kind}_NAME") or env.get("MGIT_AUTHOR_NAME") or self.config_get("user.name")
        email = env.get(f"MGIT_{kind}_EMAIL") or env.get("MGIT_AUTHOR_EMAIL") or self.config_get("user.email")
        if not name:
            try:
                name = getpass.getuser()
            except Exception:
                name = "mgit user"
        email = email or f"{name.replace(' ', '.').lower()}@localhost"
        fixed = os.environ.get("MGIT_DATE")  # "1700000000 +0000" - reproducible commits
        if fixed:
            ts, tz = fixed.split()
            return Signature(name, email, int(ts), tz)
        now = time.time()
        offset = int(round((time.mktime(time.localtime(now)) - time.mktime(time.gmtime(now))) / 60))
        sign = "+" if offset >= 0 else "-"
        tz = f"{sign}{abs(offset) // 60:02d}{abs(offset) % 60:02d}"
        return Signature(name, email, int(now), tz)

    # ------------------------------------------------------------------ refs and HEAD

    def read_ref(self, name: str) -> str | None:
        """Resolve a ref (following ``ref:`` indirection) to an object id, or None."""
        for _ in range(10):
            content = self._read(self.git_path(*name.split("/")))
            if content is None:
                return None
            content = content.strip()
            if content.startswith("ref: "):
                name = content[5:]
                continue
            return content
        raise MgitError(f"ref '{name}' is a symbolic-ref loop")

    def update_ref(self, name: str, sha: str) -> None:
        self._write(self.git_path(*name.split("/")), sha + "\n")

    def delete_ref(self, name: str) -> None:
        path = self.git_path(*name.split("/"))
        if os.path.exists(path):
            os.unlink(path)

    def head_ref(self) -> str | None:
        """``refs/heads/<branch>`` when HEAD is attached, None when detached."""
        content = (self._read(self.git_path("HEAD")) or "").strip()
        return content[5:] if content.startswith("ref: ") else None

    def current_branch(self) -> str | None:
        ref = self.head_ref()
        return ref[len("refs/heads/") :] if ref else None

    def head_commit(self) -> str | None:
        return self.read_ref("HEAD")

    def set_head_branch(self, branch: str) -> None:
        self._write(self.git_path("HEAD"), f"ref: refs/heads/{branch}\n")

    def set_head_detached(self, sha: str) -> None:
        self._write(self.git_path("HEAD"), sha + "\n")

    def advance_head(self, sha: str) -> None:
        """Point the current branch (or the detached HEAD) at ``sha``."""
        ref = self.head_ref()
        if ref:
            self.update_ref(ref, sha)
        else:
            self.set_head_detached(sha)

    def list_refs(self, prefix: str) -> dict[str, str]:
        """``{short name: sha}`` for refs under e.g. ``refs/heads``."""
        base = self.git_path(*prefix.split("/"))
        refs: dict[str, str] = {}
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".lock"):
                    continue
                full = os.path.join(dirpath, f)
                short = os.path.relpath(full, base).replace(os.sep, "/")
                sha = self.read_ref(f"{prefix}/{short}")
                if sha:
                    refs[short] = sha
        return dict(sorted(refs.items()))

    def branches(self) -> dict[str, str]:
        return self.list_refs("refs/heads")

    def tags(self) -> dict[str, str]:
        return self.list_refs("refs/tags")

    # ------------------------------------------------------------------ objects

    def read_commit(self, sha: str) -> Commit:
        return Commit.parse(self.store.read_typed(sha, "commit"))

    def read_tree(self, sha: str) -> list[TreeEntry]:
        return parse_tree(self.store.read_typed(sha, "tree"))

    def read_blob(self, sha: str) -> bytes:
        return self.store.read_typed(sha, "blob")

    def peel(self, sha: str) -> str:
        """Follow annotated tags to the object they point at."""
        for _ in range(20):
            obj_type, payload = self.store.read(sha)
            if obj_type != "tag":
                return sha
            sha = Tag.parse(payload).object
        raise MgitError("tag chain too long")

    def flatten_tree(self, tree_sha: str, prefix: str = "") -> FlatTree:
        out: FlatTree = {}
        for e in self.read_tree(tree_sha):
            path = f"{prefix}{e.name}"
            if e.is_tree:
                out.update(self.flatten_tree(e.sha, path + "/"))
            else:
                out[path] = (e.mode, e.sha)
        return out

    def commit_tree_map(self, commit_sha: str | None) -> FlatTree:
        if not commit_sha:
            return {}
        return self.flatten_tree(self.read_commit(commit_sha).tree)

    def write_tree(self, flat: FlatTree) -> str:
        """Build nested tree objects from a flat ``{path: (mode, sha)}`` map; return the root id."""
        root: dict = {}
        for path, (mode, sha) in flat.items():
            node = root
            *dirs, name = path.split("/")
            for d in dirs:
                node = node.setdefault(d, {})
            node[name] = (mode, sha)

        def build(node: dict) -> str:
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    entries.append(TreeEntry(MODE_TREE, name, build(value)))
                else:
                    entries.append(TreeEntry(value[0], name, value[1]))
            return self.store.write("tree", serialize_tree(entries))

        return build(root)

    # ------------------------------------------------------------------ revisions

    def _resolve_name(self, name: str) -> str | None:
        if name in ("HEAD", "@"):
            sha = self.head_commit()
            if not sha:
                raise MgitError("HEAD does not point to a commit yet (no commits on this branch)")
            return sha
        if name in ("ORIG_HEAD", "MERGE_HEAD"):
            content = self._read(self.git_path(name))
            return content.strip() if content else None
        for ref in (name, f"refs/heads/{name}", f"refs/tags/{name}"):
            if ref.startswith("refs/") and valid_ref_name(ref):
                sha = self.read_ref(ref)
                if sha:
                    return sha
        if re.fullmatch(r"[0-9a-fA-F]{4,40}", name):
            matches = self.store.find_prefix(name)
            if len(matches) > 1:
                raise MgitError(f"short object id '{name}' is ambiguous ({len(matches)} matches)")
            if matches:
                return matches[0]
        return None

    def rev_parse(self, rev: str) -> str:
        """Resolve ``HEAD``, a branch, a tag, a (short) id, plus ``~N`` / ``^N`` suffixes."""
        m = re.fullmatch(r"(.+?)((?:[~^]\d*)*)", rev.strip())
        if not m:
            raise MgitError(f"invalid revision '{rev}'")
        base, suffix = m.groups()
        sha = self._resolve_name(base)
        if not sha:
            raise MgitError(f"unknown revision '{rev}'", hint="check 'mgit branch', 'mgit tag' or 'mgit log'")
        for op, num in re.findall(r"([~^])(\d*)", suffix):
            n = int(num) if num else 1
            sha = self.peel(sha)
            if op == "~":
                for _ in range(n):
                    parents = self.read_commit(sha).parents
                    if not parents:
                        raise MgitError(f"revision '{rev}' goes past the root commit")
                    sha = parents[0]
            elif n > 0:
                parents = self.read_commit(sha).parents
                if len(parents) < n:
                    raise MgitError(f"commit {sha[:7]} has no parent #{n}")
                sha = parents[n - 1]
        return sha

    def rev_parse_commit(self, rev: str) -> str:
        sha = self.peel(self.rev_parse(rev))
        obj_type, _ = self.store.read(sha)
        if obj_type != "commit":
            raise MgitError(f"'{rev}' is a {obj_type}, not a commit")
        return sha

    # ------------------------------------------------------------------ index and working tree

    def load_index(self) -> Index:
        return Index.load(self.git_path("index"))

    def ignore_rules(self) -> IgnoreRules:
        return IgnoreRules.load(self.root)

    @staticmethod
    def mode_of(st: os.stat_result) -> str:
        if os.name != "nt" and st.st_mode & stat.S_IXUSR:
            return MODE_EXEC
        return MODE_FILE

    def walk_files(self, rel_dir: str = "", rules: IgnoreRules | None = None) -> list[str]:
        """Non-ignored regular files under ``rel_dir`` (repo-relative POSIX paths, sorted).

        Uses ``os.scandir`` so file-type checks come from the directory listing itself
        (no extra ``stat`` call per file on Windows). Symlinks are skipped.
        """
        rules = rules or self.ignore_rules()
        found: list[str] = []
        stack = [rel_dir]
        while stack:
            rel_base = stack.pop()
            prefix = rel_base + "/" if rel_base else ""
            try:
                entries = list(os.scandir(self.abspath(rel_base)))
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                rel = prefix + entry.name
                if entry.is_dir():
                    if rel != MGIT_DIR and not rules.is_ignored(rel, is_dir=True):
                        stack.append(rel)
                elif entry.is_file() and not rules.is_ignored(rel):
                    found.append(rel)
        return sorted(found)

    def hash_file(self, rel: str, write: bool = False) -> tuple[str, str, os.stat_result]:
        full = self.abspath(rel)
        st = os.stat(full)
        with open(full, "rb") as fh:
            data = fh.read()
        sha = self.store.write("blob", data) if write else hash_object("blob", data)
        return sha, self.mode_of(st), st

    def worktree_entry(self, rel: str, entry: IndexEntry | None) -> tuple[str, str] | None:
        """``(mode, sha)`` of the file on disk, or None if it is missing.

        Uses the index's cached stat data to avoid re-hashing files that have not changed.
        """
        full = self.abspath(rel)
        try:
            st = os.stat(full)
        except (FileNotFoundError, NotADirectoryError):
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if entry is not None and entry.stat_matches(st):
            return (entry.mode, entry.sha)
        sha, mode, _ = self.hash_file(rel)
        return (mode, sha)

    def worktree_map(self, paths, index: Index) -> FlatTree:
        out: FlatTree = {}
        for p in paths:
            value = self.worktree_entry(p, index.entries.get(p))
            if value:
                out[p] = value
        return out

    # ------------------------------------------------------------------ merge state

    def merge_head(self) -> str | None:
        content = self._read(self.git_path("MERGE_HEAD"))
        return content.strip() if content else None

    def merge_conflicts(self) -> list[str]:
        content = self._read(self.git_path("MERGE_CONFLICTS"))
        return [line for line in (content or "").splitlines() if line]

    def set_merge_conflicts(self, paths: list[str]) -> None:
        self._write(self.git_path("MERGE_CONFLICTS"), "".join(p + "\n" for p in sorted(set(paths))))

    def clear_merge_state(self) -> None:
        for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_CONFLICTS"):
            path = self.git_path(name)
            if os.path.exists(path):
                os.unlink(path)

    # ------------------------------------------------------------------ status

    def status(self, include_untracked: bool = True) -> Status:
        head = self.head_commit()
        head_map = self.commit_tree_map(head)
        index = self.load_index()
        index_map = index.as_map()
        st = Status(self.current_branch(), head, merging=self.merge_head())
        st.conflicts = self.merge_conflicts()

        for path in sorted(set(head_map) | set(index_map)):
            if path not in index_map:
                st.staged[path] = "deleted"
            elif path not in head_map:
                st.staged[path] = "added"
            elif head_map[path] != index_map[path]:
                st.staged[path] = "modified"

        refreshed = False
        now = time.time_ns()
        for path, entry in sorted(index.entries.items()):
            if path in st.conflicts:
                continue
            on_disk = self.worktree_entry(path, entry)
            if on_disk is None:
                st.unstaged[path] = "deleted"
            elif on_disk != (entry.mode, entry.sha):
                st.unstaged[path] = "modified"
            elif entry.mtime_ns == 0:
                # Content verified equal by hashing: refresh the stat cache (as `git status` does).
                fst = os.stat(self.abspath(path))
                if fst.st_mtime_ns < now - 2_000_000_000:
                    entry.size, entry.mtime_ns = fst.st_size, fst.st_mtime_ns
                    refreshed = True
        if refreshed:
            index.save()

        if include_untracked:
            st.untracked = [p for p in self.walk_files() if p not in index.entries]
        return st
