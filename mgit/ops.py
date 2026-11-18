"""Porcelain operations: add, rm, commit, branch, tag, checkout, reset, merge, history walks.

Every function takes a :class:`Repository` and raises :class:`MgitError` on user errors, so the
same code serves the CLI and the tests.
"""

from __future__ import annotations

import heapq
import os
from collections.abc import Iterator
from dataclasses import dataclass, field

from .diff import decode, encode, is_binary, split_lines
from .errors import MgitError
from .index import Index, IndexEntry
from .merge import merge3
from .objects import Commit, Tag
from .repo import FlatTree, Repository, valid_ref_name

# ---------------------------------------------------------------------------- staging


def _stage(repo: Repository, index: Index, rel: str) -> bool:
    sha, mode, st = repo.hash_file(rel, write=True)
    old = index.entries.get(rel)
    index.entries[rel] = IndexEntry.from_stat(sha, mode, st)
    return old is None or (old.sha, old.mode) != (sha, mode)


def add(repo: Repository, paths: list[str], force: bool = False, cwd: str | None = None) -> list[str]:
    """Stage files and directories. Deleted tracked files under a given path are unstaged too.

    Returns the paths whose staged content changed.
    """
    if not paths:
        raise MgitError("nothing specified, nothing added", hint="maybe you wanted 'mgit add .'")
    index = repo.load_index()
    rules = repo.ignore_rules()
    changed: list[str] = []
    touched: set[str] = set()
    for user_path in paths:
        rel = repo.to_rel(user_path, cwd)
        full = repo.abspath(rel)
        if os.path.isdir(full):
            prefix = rel + "/" if rel else ""
            for f in repo.walk_files(rel, rules):
                touched.add(f)
                if _stage(repo, index, f):
                    changed.append(f)
            for tracked in list(index.entries):
                if tracked.startswith(prefix) and not os.path.isfile(repo.abspath(tracked)):
                    del index.entries[tracked]
                    changed.append(tracked)
        elif os.path.isfile(full):
            if rules.is_ignored(rel) and rel not in index.entries and not force:
                raise MgitError(
                    f"'{rel}' is ignored by .mgitignore",
                    hint="use 'mgit add -f' if you really want to add it",
                )
            touched.add(rel)
            if _stage(repo, index, rel):
                changed.append(rel)
        elif rel in index.entries:
            del index.entries[rel]
            changed.append(rel)
            touched.add(rel)
        else:
            raise MgitError(f"pathspec '{user_path}' did not match any files")
    index.save()
    conflicts = repo.merge_conflicts()
    if conflicts:  # staging a conflicted path marks it resolved
        repo.set_merge_conflicts([c for c in conflicts if c not in touched and c not in changed])
    return sorted(set(changed))


def rm(
    repo: Repository,
    paths: list[str],
    cached: bool = False,
    force: bool = False,
    recursive: bool = False,
    cwd: str | None = None,
) -> list[str]:
    index = repo.load_index()
    head_map = repo.commit_tree_map(repo.head_commit())
    targets: list[str] = []
    for user_path in paths:
        rel = repo.to_rel(user_path, cwd)
        if rel in index.entries:
            targets.append(rel)
            continue
        prefix = rel + "/" if rel else ""
        under = [p for p in index.entries if p.startswith(prefix)]
        if not under:
            raise MgitError(f"pathspec '{user_path}' did not match any tracked files")
        if not recursive:
            raise MgitError(f"not removing '{user_path}' recursively without -r")
        targets += under
    if not force and not cached:
        for rel in targets:
            entry = index.entries[rel]
            on_disk = repo.worktree_entry(rel, entry)
            if on_disk is not None and on_disk != (entry.mode, entry.sha):
                raise MgitError(f"'{rel}' has local modifications", hint="use --cached to keep the file, or -f to force removal")
            if head_map.get(rel) != (entry.mode, entry.sha):
                raise MgitError(f"'{rel}' has changes staged in the index", hint="use --cached to keep the file, or -f to force removal")
    for rel in targets:
        del index.entries[rel]
        if not cached:
            full = repo.abspath(rel)
            if os.path.exists(full):
                os.unlink(full)
                _prune_empty_dirs(repo, rel)
    index.save()
    return sorted(targets)


# ---------------------------------------------------------------------------- commits


def commit(repo: Repository, message: str | None, allow_empty: bool = False) -> str:
    merge_head = repo.merge_head()
    conflicts = repo.merge_conflicts()
    if conflicts:
        raise MgitError(
            "cannot commit: you have unmerged paths: " + ", ".join(conflicts),
            hint="fix the conflicts, then 'mgit add <file>' to mark them resolved",
        )
    if message is None and merge_head:
        message = repo._read(repo.git_path("MERGE_MSG")) or f"Merge commit {merge_head[:7]}"
    if not message or not message.strip():
        raise MgitError("aborting commit due to empty commit message", hint='use -m "message"')
    index = repo.load_index()
    tree = repo.write_tree(index.as_map())
    head = repo.head_commit()
    if head and not merge_head and not allow_empty and repo.read_commit(head).tree == tree:
        raise MgitError("nothing to commit, working tree matches HEAD", hint="use 'mgit add' to stage changes, or --allow-empty")
    if not head and not index.entries and not allow_empty:
        raise MgitError("nothing to commit (create/copy files and use 'mgit add' to track them)")
    parents = [head] if head else []
    if merge_head:
        parents.append(merge_head)
    author = repo.signature("AUTHOR")
    committer = repo.signature("COMMITTER")
    sha = repo.store.write("commit", Commit(tree, parents, author, committer, message.strip() + "\n").serialize())
    repo.advance_head(sha)
    repo.clear_merge_state()
    return sha


def iter_history(repo: Repository, starts: list[str], first_parent: bool = False) -> Iterator[tuple[str, Commit]]:
    """Commits reachable from ``starts``, newest first, never showing a parent before a child."""
    commits: dict[str, Commit] = {}
    children: dict[str, int] = {}
    stack = list(dict.fromkeys(starts))
    while stack:
        sha = stack.pop()
        if sha in commits:
            continue
        c = repo.read_commit(sha)
        commits[sha] = c
        parents = c.parents[:1] if first_parent else c.parents
        for p in parents:
            children[p] = children.get(p, 0) + 1
            stack.append(p)
    order = 0
    heap: list[tuple[int, int, str]] = []
    for sha in dict.fromkeys(starts):
        if children.get(sha, 0) == 0:
            heapq.heappush(heap, (-commits[sha].committer.timestamp, order, sha))
            order += 1
    seen: set[str] = set()
    while heap:
        _, _, sha = heapq.heappop(heap)
        if sha in seen:
            continue
        seen.add(sha)
        c = commits[sha]
        yield sha, c
        for p in c.parents[:1] if first_parent else c.parents:
            children[p] -= 1
            if children[p] == 0:
                heapq.heappush(heap, (-commits[p].committer.timestamp, order, p))
                order += 1


def ancestors(repo: Repository, sha: str) -> set[str]:
    seen: set[str] = set()
    stack = [sha]
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        stack += repo.read_commit(s).parents
    return seen


def is_ancestor(repo: Repository, maybe_ancestor: str, sha: str) -> bool:
    return maybe_ancestor in ancestors(repo, sha)


def merge_base(repo: Repository, a: str, b: str) -> str | None:
    """Best common ancestor: a common ancestor that is not an ancestor of another one."""
    common = ancestors(repo, a) & ancestors(repo, b)
    if not common:
        return None
    best = set(common)
    for c in common:
        if c in best:
            best -= ancestors(repo, c) - {c}
    # deterministic choice if history is criss-crossed: newest first
    return max(best, key=lambda s: (repo.read_commit(s).committer.timestamp, s))


# ---------------------------------------------------------------------------- branches and tags


def create_branch(repo: Repository, name: str, start: str = "HEAD", force: bool = False) -> str:
    if not valid_ref_name(name):
        raise MgitError(f"'{name}' is not a valid branch name")
    if repo.read_ref(f"refs/heads/{name}") and not force:
        raise MgitError(f"a branch named '{name}' already exists")
    sha = repo.rev_parse_commit(start)
    repo.update_ref(f"refs/heads/{name}", sha)
    return sha


def delete_branch(repo: Repository, name: str, force: bool = False) -> str:
    sha = repo.read_ref(f"refs/heads/{name}")
    if not sha:
        raise MgitError(f"branch '{name}' not found")
    if repo.current_branch() == name:
        raise MgitError(f"cannot delete branch '{name}': it is checked out")
    head = repo.head_commit()
    if not force and head and not is_ancestor(repo, sha, head):
        raise MgitError(f"branch '{name}' is not fully merged", hint=f"use 'mgit branch -D {name}' to delete it anyway")
    repo.delete_ref(f"refs/heads/{name}")
    return sha


def create_tag(repo: Repository, name: str, rev: str = "HEAD", message: str | None = None, force: bool = False) -> str:
    if not valid_ref_name(name):
        raise MgitError(f"'{name}' is not a valid tag name")
    if repo.read_ref(f"refs/tags/{name}") and not force:
        raise MgitError(f"tag '{name}' already exists")
    target = repo.rev_parse(rev)
    if message is not None:
        obj_type, _ = repo.store.read(target)
        tag = Tag(target, obj_type, name, repo.signature("COMMITTER"), message.strip() + "\n")
        target = repo.store.write("tag", tag.serialize())
    repo.update_ref(f"refs/tags/{name}", target)
    return target


def delete_tag(repo: Repository, name: str) -> str:
    sha = repo.read_ref(f"refs/tags/{name}")
    if not sha:
        raise MgitError(f"tag '{name}' not found")
    repo.delete_ref(f"refs/tags/{name}")
    return sha


# ---------------------------------------------------------------------------- working tree updates


def _prune_empty_dirs(repo: Repository, rel: str) -> None:
    parts = rel.split("/")[:-1]
    while parts:
        full = repo.abspath("/".join(parts))
        try:
            if os.listdir(full):
                return
            os.rmdir(full)
        except OSError:
            return
        parts.pop()


def _write_blob(repo: Repository, rel: str, mode: str, sha: str) -> None:
    full = repo.abspath(rel)
    if os.path.isdir(full):
        raise MgitError(f"cannot write '{rel}': a directory is in the way")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(repo.read_blob(sha))
    if os.name != "nt":
        os.chmod(full, 0o755 if mode == "100755" else 0o644)


def switch_tree(repo: Repository, old: FlatTree, new: FlatTree, index: Index | None = None) -> Index:
    """Make the working tree and index match ``new``, given that they currently match ``old``."""
    for rel in sorted(set(old) - set(new)):
        full = repo.abspath(rel)
        if os.path.isfile(full):
            os.unlink(full)
            _prune_empty_dirs(repo, rel)
    for rel, (mode, sha) in sorted(new.items()):
        if old.get(rel) != (mode, sha) or not os.path.isfile(repo.abspath(rel)):
            _write_blob(repo, rel, mode, sha)
    index = index or repo.load_index()
    index.entries = {}
    for rel, (mode, sha) in new.items():
        index.entries[rel] = IndexEntry.from_stat(sha, mode, os.stat(repo.abspath(rel)))
    index.save()
    return index


def _check_untracked_overwrite(repo: Repository, index: Index, new: FlatTree) -> None:
    clobbered = []
    for rel, (_mode, sha) in new.items():
        if rel in index.entries:
            continue
        full = repo.abspath(rel)
        if os.path.isfile(full):
            if repo.hash_file(rel)[0] != sha:
                clobbered.append(rel)
    if clobbered:
        raise MgitError(
            "untracked working tree files would be overwritten: " + ", ".join(sorted(clobbered)),
            hint="move or remove them first",
        )


def _require_clean(repo: Repository, action: str) -> None:
    st = repo.status(include_untracked=False)
    if st.merging:
        raise MgitError(f"cannot {action}: a merge is in progress", hint="commit it or run 'mgit merge --abort'")
    if not st.clean:
        files = sorted(set(st.staged) | set(st.unstaged))
        shown = ", ".join(files[:5]) + (" ..." if len(files) > 5 else "")
        raise MgitError(
            f"cannot {action}: you have uncommitted changes ({shown})",
            hint="commit them, or discard them with 'mgit reset --hard'",
        )


@dataclass
class CheckoutResult:
    sha: str
    branch: str | None
    created: bool = False


def checkout(repo: Repository, target: str, new_branch: str | None = None, force: bool = False) -> CheckoutResult:
    head = repo.head_commit()
    if new_branch:
        sha = repo.rev_parse_commit(target) if target else head
        if sha is None:
            raise MgitError("cannot create a branch before the first commit")
        if repo.read_ref(f"refs/heads/{new_branch}"):
            raise MgitError(f"a branch named '{new_branch}' already exists")
        if sha != head and not force:
            _require_clean(repo, "switch branches")
        if not valid_ref_name(new_branch):
            raise MgitError(f"'{new_branch}' is not a valid branch name")
        if sha != head:
            index = repo.load_index()
            _check_untracked_overwrite(repo, index, repo.commit_tree_map(sha))
            switch_tree(repo, {**repo.commit_tree_map(head), **index.as_map()}, repo.commit_tree_map(sha), index)
        repo.update_ref(f"refs/heads/{new_branch}", sha)
        repo.set_head_branch(new_branch)
        return CheckoutResult(sha, new_branch, created=True)

    branch = target if repo.read_ref(f"refs/heads/{target}") else None
    sha = repo.read_ref(f"refs/heads/{target}") if branch else repo.rev_parse_commit(target)
    if branch and branch == repo.current_branch():
        return CheckoutResult(sha, branch)
    if sha == head and not repo.merge_head():  # same snapshot: just move HEAD, keep local changes
        if branch:
            repo.set_head_branch(branch)
        else:
            repo.set_head_detached(sha)
        return CheckoutResult(sha, branch)
    if not force:
        _require_clean(repo, "switch branches")
    index = repo.load_index()
    new_map = repo.commit_tree_map(sha)
    if not force:
        _check_untracked_overwrite(repo, index, new_map)
    old_map = {**repo.commit_tree_map(head), **index.as_map()}
    switch_tree(repo, old_map, new_map, index)
    repo.clear_merge_state()
    if branch:
        repo.set_head_branch(branch)
    else:
        repo.set_head_detached(sha)
    return CheckoutResult(sha, branch)


def restore_paths(repo: Repository, paths: list[str], source: str | None = None, cwd: str | None = None) -> list[str]:
    """``checkout [rev] -- paths``: overwrite working files from the index (or a commit)."""
    index = repo.load_index()
    src = repo.commit_tree_map(repo.rev_parse_commit(source)) if source else index.as_map()
    restored = []
    for user_path in paths:
        rel = repo.to_rel(user_path, cwd)
        prefix = rel + "/" if rel else ""
        matches = [p for p in src if p == rel or p.startswith(prefix)]
        if not matches:
            raise MgitError(f"pathspec '{user_path}' did not match any file known to mgit")
        for p in matches:
            mode, sha = src[p]
            _write_blob(repo, p, mode, sha)
            if source:
                index.entries[p] = IndexEntry.from_stat(sha, mode, os.stat(repo.abspath(p)))
            restored.append(p)
    index.save()
    return sorted(restored)


def reset(repo: Repository, rev: str = "HEAD", mode: str = "mixed") -> str:
    if mode not in ("soft", "mixed", "hard"):
        raise ValueError(mode)
    target = repo.rev_parse_commit(rev)
    head = repo.head_commit()
    if head:
        repo._write(repo.git_path("ORIG_HEAD"), head + "\n")
    index = repo.load_index()
    target_map = repo.commit_tree_map(target)
    if mode == "hard":
        old = {**repo.commit_tree_map(head), **index.as_map()}
        old.update({p: ("", "") for p in repo.merge_conflicts()})
        # anything conflicted or modified must be rewritten: pretend the old state is unknown
        switch_tree(repo, {p: ("", "") for p in old}, target_map, index)
    elif mode == "mixed":
        index.entries = {}
        for rel, (fmode, sha) in target_map.items():
            index.entries[rel] = IndexEntry(sha, fmode, -1, 0)
        index.save()
    repo.advance_head(target)
    repo.clear_merge_state()
    return target


def reset_paths(repo: Repository, paths: list[str], rev: str = "HEAD", cwd: str | None = None) -> list[str]:
    """Unstage: make the index entries for ``paths`` match ``rev`` (working files untouched)."""
    head = repo.head_commit()
    source = repo.commit_tree_map(repo.rev_parse_commit(rev)) if head else {}
    index = repo.load_index()
    touched = []
    for user_path in paths:
        rel = repo.to_rel(user_path, cwd)
        prefix = rel + "/" if rel else ""
        keys = {p for p in set(source) | set(index.entries) if p == rel or p.startswith(prefix)}
        for p in keys:
            if p in source:
                mode, sha = source[p]
                index.entries[p] = IndexEntry(sha, mode, -1, 0)
            else:
                index.entries.pop(p, None)
            touched.append(p)
    index.save()
    return sorted(touched)


# ---------------------------------------------------------------------------- merge


@dataclass
class MergeOutcome:
    kind: str  # "up-to-date" | "fast-forward" | "merge" | "conflict"
    sha: str | None
    conflicts: list[str] = field(default_factory=list)
    merged_files: list[str] = field(default_factory=list)


def _merge_blob(repo: Repository, path: str, base, ours, theirs, ours_label: str, theirs_label: str):
    """Return ``(mode, sha, conflicted)`` for a path changed on both sides."""
    mode = ours[0] if ours[0] != (base or ours)[0] else theirs[0]
    base_bytes = repo.read_blob(base[1]) if base else b""
    ours_bytes, theirs_bytes = repo.read_blob(ours[1]), repo.read_blob(theirs[1])
    if any(is_binary(b) for b in (base_bytes, ours_bytes, theirs_bytes)):
        return ours[0], ours[1], True
    result = merge3(
        split_lines(decode(base_bytes)),
        split_lines(decode(ours_bytes)),
        split_lines(decode(theirs_bytes)),
        ours_label,
        theirs_label,
    )
    sha = repo.store.write("blob", encode("".join(result.lines)))
    return mode, sha, not result.clean


def merge(repo: Repository, rev: str, message: str | None = None, ff_only: bool = False, no_ff: bool = False) -> MergeOutcome:
    head = repo.head_commit()
    if head is None:
        raise MgitError("cannot merge into an empty branch (make a first commit)")
    _require_clean(repo, "merge")
    theirs = repo.rev_parse_commit(rev)
    if is_ancestor(repo, theirs, head):
        return MergeOutcome("up-to-date", head)
    index = repo.load_index()
    head_map = repo.commit_tree_map(head)
    their_map = repo.commit_tree_map(theirs)
    _check_untracked_overwrite(repo, index, their_map)

    if is_ancestor(repo, head, theirs) and not no_ff:
        switch_tree(repo, head_map, their_map, index)
        repo._write(repo.git_path("ORIG_HEAD"), head + "\n")
        repo.advance_head(theirs)
        return MergeOutcome("fast-forward", theirs)
    if ff_only:
        raise MgitError("not possible to fast-forward, aborting", hint="drop --ff-only to create a merge commit")

    base = merge_base(repo, head, theirs)
    base_map = repo.commit_tree_map(base)
    ours_label = f"HEAD ({repo.current_branch() or head[:7]})"
    theirs_label = rev
    result: FlatTree = {}
    conflicts: list[str] = []
    merged_files: list[str] = []
    conflict_contents: dict[str, bytes] = {}
    for path in sorted(set(base_map) | set(head_map) | set(their_map)):
        b, o, t = base_map.get(path), head_map.get(path), their_map.get(path)
        if o == t:
            chosen = o
        elif o == b:
            chosen = t
        elif t == b:
            chosen = o
        elif o is None or t is None:
            # modify/delete conflict: keep the surviving version in the working tree
            survivor = o or t
            conflicts.append(path)
            conflict_contents[path] = repo.read_blob(survivor[1])
            chosen = o  # index keeps "ours" until the user resolves it
        else:
            mode, sha, conflicted = _merge_blob(repo, path, b, o, t, ours_label, theirs_label)
            if conflicted:
                conflicts.append(path)
                conflict_contents[path] = repo.read_blob(sha)
                chosen = o
            else:
                merged_files.append(path)
                chosen = (mode, sha)
        if chosen is not None:
            result[path] = chosen

    switch_tree(repo, head_map, result, index)
    repo._write(repo.git_path("ORIG_HEAD"), head + "\n")
    msg = message or f"Merge {'branch' if repo.read_ref('refs/heads/' + rev) else 'revision'} '{rev}'"
    if repo.current_branch():
        msg += f" into {repo.current_branch()}"
    if conflicts:
        for path, data in conflict_contents.items():
            full = repo.abspath(path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(data)
        repo._write(repo.git_path("MERGE_HEAD"), theirs + "\n")
        repo._write(repo.git_path("MERGE_MSG"), msg + "\n")
        repo.set_merge_conflicts(conflicts)
        return MergeOutcome("conflict", None, conflicts, merged_files)

    tree = repo.write_tree(result)
    c = Commit(tree, [head, theirs], repo.signature("AUTHOR"), repo.signature("COMMITTER"), msg + "\n")
    sha = repo.store.write("commit", c.serialize())
    repo.advance_head(sha)
    return MergeOutcome("merge", sha, [], merged_files)


def merge_abort(repo: Repository) -> str:
    if not repo.merge_head():
        raise MgitError("there is no merge to abort")
    return reset(repo, "HEAD", "hard")
