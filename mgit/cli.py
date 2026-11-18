"""Command-line interface: argument parsing and human-readable output."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import __version__, ops
from .color import enabled as color_enabled
from .color import paint
from .diff import decode, is_binary, split_lines, unified_hunks
from .errors import EXIT_ERROR, EXIT_OK, EXIT_USAGE, MgitError
from .graph import graph_lines
from .objects import MODE_TREE, Commit, Signature, Tag, hash_object
from .repo import FlatTree, Repository


class Out:
    """Writes to stdout with optional colour (decided per call so tests can redirect stdout)."""

    def __init__(self) -> None:
        self.color = color_enabled(sys.stdout)

    def __call__(self, text: str = "", *styles: str) -> None:
        sys.stdout.write(paint(text, *styles, on=self.color) + "\n")

    def c(self, text: str, *styles: str) -> str:
        return paint(text, *styles, on=self.color)


def _short(sha: str | None) -> str:
    return sha[:7] if sha else "0000000"


def format_date(sig: Signature) -> str:
    sign = 1 if sig.tz.startswith("+") else -1
    offset = timedelta(hours=int(sig.tz[1:3]), minutes=int(sig.tz[3:5])) * sign
    dt = datetime.fromtimestamp(sig.timestamp, timezone(offset))
    return dt.strftime("%a %b %d %H:%M:%S %Y ") + sig.tz


def _decorations(repo: Repository) -> dict[str, list[str]]:
    deco: dict[str, list[str]] = {}
    head = repo.head_commit()
    branch = repo.current_branch()
    for name, sha in repo.branches().items():
        label = f"HEAD -> {name}" if name == branch else name
        deco.setdefault(sha, []).append(label)
    if head and not branch:
        deco.setdefault(head, []).insert(0, "HEAD")
    for name, sha in repo.tags().items():
        deco.setdefault(repo.peel(sha), []).append(f"tag: {name}")
    for labels in deco.values():
        labels.sort(key=lambda s: (not s.startswith("HEAD"), s.startswith("tag:"), s))
    return deco


# ------------------------------------------------------------------------------ diff rendering


def render_diff(
    repo: Repository,
    old: FlatTree,
    new: FlatTree,
    out: Out,
    new_reader: Callable[[str, str], bytes] | None = None,
    paths: list[str] | None = None,
    stat_only: bool = False,
) -> int:
    """Print a unified diff between two flat trees. Returns the number of changed files."""
    read_old = lambda _p, sha: repo.read_blob(sha)  # noqa: E731
    read_new = new_reader or read_old
    changed = 0
    for path in sorted(set(old) | set(new)):
        if paths and not any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths):
            continue
        a, b = old.get(path), new.get(path)
        if a == b:
            continue
        changed += 1
        a_data = read_old(path, a[1]) if a else b""
        b_data = read_new(path, b[1]) if b else b""
        if stat_only:
            hunks = [] if is_binary(a_data + b_data) else unified_hunks(split_lines(decode(a_data)), split_lines(decode(b_data)))
            plus = sum(1 for h in hunks if h.startswith("+"))
            minus = sum(1 for h in hunks if h.startswith("-"))
            out(f" {path} | {plus + minus} " + out.c("+" * min(plus, 40), "green") + out.c("-" * min(minus, 40), "red"))
            continue
        out(f"diff --mgit a/{path} b/{path}", "bold")
        if a is None:
            out(f"new file mode {b[0]}", "bold")
        elif b is None:
            out(f"deleted file mode {a[0]}", "bold")
        elif a[0] != b[0]:
            out(f"old mode {a[0]}", "bold")
            out(f"new mode {b[0]}", "bold")
        out(f"index {_short(a[1] if a else None)}..{_short(b[1] if b else None)}", "bold")
        if is_binary(a_data) or is_binary(b_data):
            out(f"Binary files a/{path} and b/{path} differ")
            continue
        hunks = unified_hunks(split_lines(decode(a_data)), split_lines(decode(b_data)))
        if not hunks:
            continue
        out(f"--- {'a/' + path if a else '/dev/null'}", "bold")
        out(f"+++ {'b/' + path if b else '/dev/null'}", "bold")
        for line in hunks:
            if line.startswith("@@"):
                out(line, "cyan")
            elif line.startswith("+"):
                out(line, "green")
            elif line.startswith("-"):
                out(line, "red")
            else:
                out(line)
    return changed


def _worktree_reader(repo: Repository) -> Callable[[str, str], bytes]:
    def read(path: str, sha: str) -> bytes:
        with open(repo.abspath(path), "rb") as fh:
            return fh.read()

    return read


# ------------------------------------------------------------------------------ commands


def cmd_init(args, out: Out) -> int:
    repo, existed = Repository.init(args.directory, args.initial_branch)
    verb = "Reinitialized existing" if existed else "Initialized empty"
    out(f"{verb} mgit repository in {repo.mgit_dir}")
    return EXIT_OK


def cmd_add(args, out: Out) -> int:
    repo = Repository.discover()
    changed = ops.add(repo, args.paths, force=args.force)
    if args.verbose:
        for p in changed:
            out(f"add '{p}'")
    return EXIT_OK


def cmd_rm(args, out: Out) -> int:
    repo = Repository.discover()
    for p in ops.rm(repo, args.paths, cached=args.cached, force=args.force, recursive=args.recursive):
        out(f"rm '{p}'")
    return EXIT_OK


def cmd_status(args, out: Out) -> int:
    repo = Repository.discover()
    st = repo.status()
    if args.short:
        rows: dict[str, list[str]] = {}
        letter = {"added": "A", "modified": "M", "deleted": "D"}
        for p, kind in st.staged.items():
            rows.setdefault(p, [" ", " "])[0] = letter[kind]
        for p, kind in st.unstaged.items():
            rows.setdefault(p, [" ", " "])[1] = letter[kind]
        for p in st.conflicts:
            rows[p] = ["U", "U"]
        for p in sorted(rows):
            x, y = rows[p]
            out(out.c(x, "red" if x == "U" else "green") + out.c(y, "red") + " " + p)
        for p in st.untracked:
            out(out.c("??", "red") + " " + p)
        return EXIT_OK

    if st.branch:
        out(f"On branch {st.branch}")
    else:
        out(out.c(f"HEAD detached at {_short(st.head)}", "red"))
    if not st.head:
        out("\nNo commits yet")
    if st.merging:
        out(f"\nYou are in the middle of a merge ({_short(st.merging)}).")
        out('  (fix conflicts and run "mgit commit", or "mgit merge --abort")')
    if st.conflicts:
        out("\nUnmerged paths:")
        out('  (use "mgit add <file>..." to mark resolution)')
        for p in st.conflicts:
            out(out.c(f"\tboth modified:   {p}", "red"))
    if st.staged:
        out("\nChanges to be committed:")
        out('  (use "mgit reset -- <file>..." to unstage)')
        for p, kind in st.staged.items():
            label = "new file" if kind == "added" else kind
            out(out.c(f"\t{label + ':':<12}{p}", "green"))
    if st.unstaged:
        out("\nChanges not staged for commit:")
        out('  (use "mgit add <file>..." to stage, "mgit checkout -- <file>..." to discard)')
        for p, kind in st.unstaged.items():
            out(out.c(f"\t{kind + ':':<12}{p}", "red"))
    if st.untracked:
        out("\nUntracked files:")
        out('  (use "mgit add <file>..." to include in what will be committed)')
        for p in st.untracked:
            out(out.c(f"\t{p}", "red"))
    if st.clean and not st.untracked:
        out("\nnothing to commit, working tree clean" if st.head else '\nnothing to commit (create files and use "mgit add" to track)')
    elif not st.staged and not st.conflicts:
        if st.unstaged:
            out('\nno changes added to commit (use "mgit add")')
        else:
            out('\nnothing added to commit but untracked files present (use "mgit add" to track)')
    return EXIT_OK


def cmd_commit(args, out: Out) -> int:
    repo = Repository.discover()
    had_head = repo.head_commit() is not None
    sha = ops.commit(repo, args.message, allow_empty=args.allow_empty)
    c = repo.read_commit(sha)
    where = repo.current_branch() or "detached HEAD"
    root = "" if had_head else " (root-commit)"
    out(f"[{where}{root} {_short(sha)}] {c.summary}")
    old = repo.commit_tree_map(c.parents[0]) if c.parents else {}
    new = repo.flatten_tree(c.tree)
    changed = sum(1 for p in set(old) | set(new) if old.get(p) != new.get(p))
    out(f" {changed} file{'s' if changed != 1 else ''} changed")
    return EXIT_OK


def _print_commit(repo: Repository, sha: str, c: Commit, out: Out, deco: dict[str, list[str]]) -> list[str]:
    labels = deco.get(sha)
    head = out.c(f"commit {sha}", "yellow")
    if labels:
        head += out.c(" (", "yellow") + out.c(", ".join(labels), "cyan", "bold") + out.c(")", "yellow")
    lines = [head]
    if len(c.parents) > 1:
        lines.append("Merge: " + " ".join(_short(p) for p in c.parents))
    lines.append(f"Author: {c.author.name} <{c.author.email}>")
    lines.append(f"Date:   {format_date(c.author)}")
    lines.append("")
    lines += ["    " + line for line in c.message.rstrip("\n").split("\n")]
    lines.append("")
    return lines


def cmd_log(args, out: Out) -> int:
    repo = Repository.discover()
    if not args.revs and not repo.head_commit():
        raise MgitError(f"your current branch '{repo.current_branch()}' does not have any commits yet")
    starts = [repo.rev_parse_commit(r) for r in (args.revs or ["HEAD"])]
    if args.all:
        starts += list(repo.branches().values())
    deco = _decorations(repo)
    rows = []
    for n, (sha, c) in enumerate(ops.iter_history(repo, starts, first_parent=args.first_parent)):
        if args.max_count is not None and n >= args.max_count:
            break
        if args.oneline:
            labels = deco.get(sha)
            label = (" " + out.c("(", "yellow") + out.c(", ".join(labels), "cyan", "bold") + out.c(")", "yellow")) if labels else ""
            text = [out.c(_short(sha), "yellow") + label + " " + c.summary]
        else:
            text = _print_commit(repo, sha, c, out, deco)
        rows.append((sha, c.parents[:1] if args.first_parent else c.parents, text))
    if args.graph:
        for line in graph_lines(rows):
            out(line)
    else:
        for _sha, _parents, text in rows:
            for line in text:
                out(line)
    return EXIT_OK


def cmd_diff(args, out: Out) -> int:
    repo = Repository.discover()
    revs = list(args.revs)
    if len(revs) == 1 and ".." in revs[0]:
        left, _, right = revs[0].partition("..")
        revs = [left or "HEAD", right or "HEAD"]
    paths = [repo.to_rel(p) for p in args.pathspec] if args.pathspec else None
    index = repo.load_index()
    if args.cached:
        if len(revs) > 1:
            raise MgitError("--cached takes at most one revision")
        base = repo.rev_parse_commit(revs[0]) if revs else repo.head_commit()
        old, new, reader = repo.commit_tree_map(base), index.as_map(), None
    elif len(revs) == 2:
        old = repo.commit_tree_map(repo.rev_parse_commit(revs[0]))
        new = repo.commit_tree_map(repo.rev_parse_commit(revs[1]))
        reader = None
    elif len(revs) == 1:
        old = repo.commit_tree_map(repo.rev_parse_commit(revs[0]))
        new = repo.worktree_map(sorted(set(old) | set(index.entries)), index)
        reader = _worktree_reader(repo)
    elif not revs:
        old = index.as_map()
        new = repo.worktree_map(sorted(index.entries), index)
        reader = _worktree_reader(repo)
    else:
        raise MgitError("too many revisions (expected at most two)")

    def worktree_or_store(path: str, sha: str) -> bytes:
        return reader(path, sha) if reader else repo.read_blob(sha)

    render_diff(repo, old, new, out, worktree_or_store if reader else None, paths, stat_only=args.stat)
    return EXIT_OK


def cmd_branch(args, out: Out) -> int:
    repo = Repository.discover()
    if args.delete or args.force_delete:
        name = args.delete or args.force_delete
        sha = ops.delete_branch(repo, name, force=bool(args.force_delete))
        out(f"Deleted branch {name} (was {_short(sha)}).")
        return EXIT_OK
    if args.name:
        ops.create_branch(repo, args.name, args.start or "HEAD", force=args.force)
        return EXIT_OK
    current = repo.current_branch()
    if not current and repo.head_commit():
        out(out.c(f"* (HEAD detached at {_short(repo.head_commit())})", "green"))
    for name, sha in repo.branches().items():
        if name == current:
            line = out.c(f"* {name}", "green")
        else:
            line = f"  {name}"
        if args.verbose:
            line += f"  {_short(sha)} {repo.read_commit(sha).summary}"
        out(line)
    return EXIT_OK


def cmd_checkout(args, out: Out) -> int:
    repo = Repository.discover()
    if args.pathspec is not None:
        restored = ops.restore_paths(repo, args.pathspec, source=args.target)
        out(f"Updated {len(restored)} path{'s' if len(restored) != 1 else ''}")
        return EXIT_OK
    if args.new_branch:
        res = ops.checkout(repo, args.target or "", new_branch=args.new_branch, force=args.force)
        out(f"Switched to a new branch '{res.branch}'")
        return EXIT_OK
    if not args.target:
        raise MgitError("you must specify a branch or commit to check out")
    res = ops.checkout(repo, args.target, force=args.force)
    if res.branch:
        out(f"Switched to branch '{res.branch}'")
    else:
        c = repo.read_commit(res.sha)
        out(f"Note: switching to '{args.target}'. You are in 'detached HEAD' state.")
        out(f"HEAD is now at {_short(res.sha)} {c.summary}")
    return EXIT_OK


def cmd_merge(args, out: Out) -> int:
    repo = Repository.discover()
    if args.abort:
        ops.merge_abort(repo)
        out("Merge aborted; working tree restored to HEAD.")
        return EXIT_OK
    if not args.rev:
        raise MgitError("specify a branch or commit to merge")
    before = repo.head_commit()
    res = ops.merge(repo, args.rev, message=args.message, ff_only=args.ff_only, no_ff=args.no_ff)
    if res.kind == "up-to-date":
        out("Already up to date.")
    elif res.kind == "fast-forward":
        out(f"Updating {_short(before)}..{_short(res.sha)}")
        out("Fast-forward")
    elif res.kind == "merge":
        for p in res.merged_files:
            out(f"Auto-merging {p}")
        out(f"Merge made by the 'three-way' strategy: {_short(res.sha)}")
    else:
        for p in res.merged_files:
            out(f"Auto-merging {p}")
        for p in res.conflicts:
            out(out.c(f"CONFLICT (content): Merge conflict in {p}", "red"))
        out("Automatic merge failed; fix conflicts, 'mgit add' them, then 'mgit commit'.")
        return EXIT_ERROR
    return EXIT_OK


def cmd_cat_file(args, out: Out) -> int:
    repo = Repository.discover()
    sha = repo.rev_parse(args.object)
    obj_type, payload = repo.store.read(sha)
    if args.t:
        out(obj_type)
    elif args.s:
        out(str(len(payload)))
    elif args.e:
        return EXIT_OK
    elif obj_type == "tree":
        for e in repo.read_tree(sha):
            kind = "tree" if e.mode == MODE_TREE else "blob"
            out(f"{e.mode.zfill(6)} {kind} {e.sha}\t{e.name}")
    elif obj_type == "blob":
        sys.stdout.flush()
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        else:
            sys.stdout.write(decode(payload))
    else:
        sys.stdout.write(payload.decode("utf-8", errors="replace"))
    return EXIT_OK


def cmd_show(args, out: Out) -> int:
    repo = Repository.discover()
    sha = repo.rev_parse(args.object)
    obj_type, payload = repo.store.read(sha)
    if obj_type == "tag":
        tag = Tag.parse(payload)
        out(out.c(f"tag {tag.tag}", "yellow"))
        out(f"Tagger: {tag.tagger.name} <{tag.tagger.email}>")
        out(f"Date:   {format_date(tag.tagger)}")
        out()
        out(tag.message.rstrip())
        out()
        sha = repo.peel(sha)
        obj_type, payload = repo.store.read(sha)
    if obj_type == "commit":
        c = Commit.parse(payload)
        for line in _print_commit(repo, sha, c, out, _decorations(repo)):
            out(line)
        old = repo.commit_tree_map(c.parents[0]) if c.parents else {}
        render_diff(repo, old, repo.flatten_tree(c.tree), out, stat_only=args.stat)
    elif obj_type == "tree":
        out(out.c(f"tree {args.object}", "yellow"))
        out()
        for e in repo.read_tree(sha):
            out(e.name + ("/" if e.mode == MODE_TREE else ""))
    else:
        sys.stdout.write(decode(payload))
    return EXIT_OK


def cmd_tag(args, out: Out) -> int:
    repo = Repository.discover()
    if args.delete:
        sha = ops.delete_tag(repo, args.delete)
        out(f"Deleted tag '{args.delete}' (was {_short(sha)})")
        return EXIT_OK
    if args.name:
        message = args.message if (args.annotate or args.message) else None
        if args.annotate and message is None:
            raise MgitError('annotated tags need a message: use -m "message"')
        ops.create_tag(repo, args.name, args.rev or "HEAD", message=message, force=args.force)
        return EXIT_OK
    for name in repo.tags():
        out(name)
    return EXIT_OK


def cmd_reset(args, out: Out) -> int:
    repo = Repository.discover()
    if args.pathspec is not None:
        if args.mode != "mixed":
            raise MgitError("--soft/--hard cannot be used with paths")
        ops.reset_paths(repo, args.pathspec, args.rev or "HEAD")
        st = repo.status(include_untracked=False)
        if st.unstaged:
            out("Unstaged changes after reset:")
            for p, kind in st.unstaged.items():
                out(f"{kind[0].upper()}\t{p}")
        return EXIT_OK
    sha = ops.reset(repo, args.rev or "HEAD", args.mode)
    if args.mode == "hard":
        out(f"HEAD is now at {_short(sha)} {repo.read_commit(sha).summary}")
    return EXIT_OK


def cmd_ls_files(args, out: Out) -> int:
    repo = Repository.discover()
    for p, e in sorted(repo.load_index().entries.items()):
        out(f"{e.mode} {e.sha}\t{p}" if args.stage else p)
    return EXIT_OK


def cmd_hash_object(args, out: Out) -> int:
    with open(args.file, "rb") as fh:
        data = fh.read()
    if args.write:
        out(Repository.discover().store.write("blob", data))
    else:
        out(hash_object("blob", data))
    return EXIT_OK


def cmd_config(args, out: Out) -> int:
    repo = Repository.discover()
    if args.value is None:
        value = repo.config_get(args.key)
        if value is None:
            return EXIT_ERROR
        out(value)
    else:
        repo.config_set(args.key, args.value)
    return EXIT_OK


# ------------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mgit", description="A mini version control system built on git's object model.")
    p.add_argument("--version", action="version", version=f"mgit {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def cmd(name: str, func, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    s = cmd("init", cmd_init, "create an empty repository")
    s.add_argument("directory", nargs="?", default=".")
    s.add_argument("-b", "--initial-branch", default="main")

    s = cmd("add", cmd_add, "stage file contents (directories recursively, honours .mgitignore)")
    s.add_argument("paths", nargs="*")
    s.add_argument("-f", "--force", action="store_true", help="allow adding ignored files")
    s.add_argument("-v", "--verbose", action="store_true")

    s = cmd("rm", cmd_rm, "remove files from the index and the working tree")
    s.add_argument("paths", nargs="+")
    s.add_argument("--cached", action="store_true", help="only remove from the index")
    s.add_argument("-f", "--force", action="store_true")
    s.add_argument("-r", dest="recursive", action="store_true")

    s = cmd("status", cmd_status, "show staged, modified and untracked files")
    s.add_argument("-s", "--short", action="store_true")

    s = cmd("commit", cmd_commit, "record the staged snapshot")
    s.add_argument("-m", "--message")
    s.add_argument("--allow-empty", action="store_true")

    s = cmd("log", cmd_log, "show commit history")
    s.add_argument("revs", nargs="*")
    s.add_argument("--oneline", action="store_true")
    s.add_argument("--graph", action="store_true")
    s.add_argument("--all", action="store_true", help="include all branches")
    s.add_argument("--first-parent", action="store_true")
    s.add_argument("-n", "--max-count", type=int)

    s = cmd("diff", cmd_diff, "show changes (worktree vs index, --cached: index vs HEAD, or between commits)")
    s.add_argument("revs", nargs="*")
    s.add_argument("--cached", "--staged", action="store_true")
    s.add_argument("--stat", action="store_true")

    s = cmd("branch", cmd_branch, "list, create or delete branches")
    s.add_argument("name", nargs="?")
    s.add_argument("start", nargs="?")
    s.add_argument("-d", "--delete", metavar="BRANCH")
    s.add_argument("-D", dest="force_delete", metavar="BRANCH")
    s.add_argument("-f", "--force", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")

    s = cmd("checkout", cmd_checkout, "switch branches or restore files (refuses when the tree is dirty)")
    s.add_argument("target", nargs="?")
    s.add_argument("-b", dest="new_branch", metavar="NEW_BRANCH")
    s.add_argument("-f", "--force", action="store_true", help="discard local changes")

    s = cmd("merge", cmd_merge, "join another branch into the current one")
    s.add_argument("rev", nargs="?")
    s.add_argument("-m", "--message")
    s.add_argument("--abort", action="store_true")
    s.add_argument("--ff-only", action="store_true")
    s.add_argument("--no-ff", action="store_true")

    s = cmd("cat-file", cmd_cat_file, "print an object's type, size or content")
    s.add_argument("object")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-t", action="store_true", help="type")
    g.add_argument("-s", action="store_true", help="size")
    g.add_argument("-p", action="store_true", help="pretty-print (default)")
    g.add_argument("-e", action="store_true", help="exit 0 if the object exists")

    s = cmd("show", cmd_show, "show a commit (with diff), tag, tree or blob")
    s.add_argument("object", nargs="?", default="HEAD")
    s.add_argument("--stat", action="store_true")

    s = cmd("tag", cmd_tag, "list, create (lightweight or annotated) or delete tags")
    s.add_argument("name", nargs="?")
    s.add_argument("rev", nargs="?")
    s.add_argument("-a", "--annotate", action="store_true")
    s.add_argument("-m", "--message")
    s.add_argument("-d", "--delete", metavar="TAG")
    s.add_argument("-f", "--force", action="store_true")

    s = cmd("reset", cmd_reset, "move HEAD (--soft/--mixed/--hard) or unstage paths")
    s.add_argument("rev", nargs="?")
    m = s.add_mutually_exclusive_group()
    m.add_argument("--soft", dest="mode", action="store_const", const="soft")
    m.add_argument("--mixed", dest="mode", action="store_const", const="mixed")
    m.add_argument("--hard", dest="mode", action="store_const", const="hard")
    s.set_defaults(mode="mixed")

    s = cmd("ls-files", cmd_ls_files, "list files in the index")
    s.add_argument("-s", "--stage", action="store_true")

    s = cmd("hash-object", cmd_hash_object, "compute a blob id (optionally store it with -w)")
    s.add_argument("file")
    s.add_argument("-w", "--write", action="store_true")

    s = cmd("config", cmd_config, "get or set a repository config value (e.g. user.name)")
    s.add_argument("key")
    s.add_argument("value", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pathspec = None
    if "--" in argv:  # everything after "--" is a path, never a revision
        i = argv.index("--")
        argv, pathspec = argv[:i], argv[i + 1 :]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else EXIT_USAGE
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    args.pathspec = pathspec
    out = Out()
    try:
        return args.func(args, out)
    except MgitError as exc:
        err_color = color_enabled(sys.stderr)
        sys.stdout.flush()
        sys.stderr.write(paint("fatal: ", "red", "bold", on=err_color) + str(exc) + "\n")
        if exc.hint:
            sys.stderr.write(paint("hint: ", "yellow", on=err_color) + exc.hint + "\n")
        return exc.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # e.g. `mgit log | head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_OK
