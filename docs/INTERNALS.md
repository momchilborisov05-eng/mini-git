# mgit internals

This page describes exactly what mgit writes to disk. Everything lives in `.mgit/` at the
repository root.

```
.mgit/
├── HEAD                 "ref: refs/heads/main\n"  or a bare commit id (detached HEAD)
├── config               INI file: [core], [user] name/email
├── index                the staging area (JSON, see below)
├── objects/
│   └── ab/cdef0123...   one zlib-compressed file per object (38 hex chars after the 2-char dir)
├── refs/
│   ├── heads/main       "<commit id>\n"  one file per branch
│   └── tags/v1.0        "<commit or tag-object id>\n"
├── ORIG_HEAD            previous HEAD, written by reset and merge
├── MERGE_HEAD           the commit being merged, while a conflicted merge is in progress
├── MERGE_MSG            default message for the merge commit
└── MERGE_CONFLICTS      one conflicted path per line
```

## Object encoding

Every object is stored as

```
zlib_compress( "<type> <payload length in bytes>" + "\0" + payload )
```

and its id is `sha1("<type> <length>\0" + payload)` as 40 lowercase hex digits, the hash of the
**uncompressed** bytes. The file is stored at `objects/<id[0:2]>/<id[2:40]>`. The two-character
fan-out directory keeps any single directory from holding millions of files.

Writes go to a temporary file in the same directory, which is then `os.replace`d into place. A
reader can never see a half-written object, and writing an object that already exists does
nothing. On every read the header length and the SHA-1 are checked again, so a corrupted or
tampered file raises an error instead of returning wrong data.

These are the same bytes git writes for loose objects. The test suite checks this against real
git: the blob, tree and commit ids match byte for byte.

### blob

The payload is the raw file content. File names and modes are not part of a blob, so two files
with the same content share one blob.

```
$ printf 'hello\n' > hello.txt && mgit hash-object hello.txt
ce013625030ba8dba906f756967f9e9ca394464a        # same id as `git hash-object`
```

### tree

A tree is a directory listing. The payload is a run of binary entries with no separators:

```
<mode as ASCII> SP <name as UTF-8> NUL <20 raw bytes of the child's SHA-1>
```

| mode     | meaning                         |
|----------|---------------------------------|
| `100644` | regular file                    |
| `100755` | executable file (POSIX only)    |
| `40000`  | subdirectory (points at a tree) |

Entries are sorted by name in byte order, with directory names compared as if they ended in
`/`. That is why `a.txt` sorts before the directory `a`, and `a` before `a0`. The order matters
because the same directory must always produce the same bytes and therefore the same id. The
empty tree's id is `4b825dc642cb6eb9a060e54bf8d69288fbee4904`, the same as in git.

`mgit cat-file -p <tree>` prints a tree in readable form:

```
100644 blob fc72a5c1094e203eefcd1c710f060957ebbbaac4	README.md
040000 tree 755d89e1c086583a7bef11c39dbdd6859858a3f6	src
```

### commit

Text, UTF-8:

```
tree <root tree id>
parent <id>                 zero lines (root commit), one, or two (merge commit)
author <name> <<email>> <unix seconds> <+hhmm>
committer <name> <<email>> <unix seconds> <+hhmm>

<message, always ending in a newline>
```

A commit records the **whole snapshot** (its root tree), not a delta. Because unchanged
subtrees keep their ids, a commit that edits one file only creates new objects for that blob
and the trees on its path up to the root. The benchmark measured this: editing 1% of a 1,000
file project produced 32 new objects (about 10 KiB).

`MGIT_DATE="<seconds> <+hhmm>"` pins both timestamps, so the same content, message and author
always give the same commit id. The tests use this, and so does the example session in the
README.

### tag (annotated)

```
object <id>
type commit
tag <name>
tagger <name> <<email>> <unix seconds> <+hhmm>

<message>
```

A lightweight tag is only a ref file (`refs/tags/<name>`) holding a commit id. An annotated tag
ref points at a tag object, and mgit follows it ("peels" it) to the commit whenever a commit is
needed.

## Refs and HEAD

A ref is a text file holding one object id. `HEAD` is a symbolic ref
(`ref: refs/heads/main`) while you are on a branch, and a raw commit id in detached mode.
Committing moves whatever HEAD points at: the branch file, or HEAD itself when detached. Ref
files are also written through a `.lock` temp file plus `os.replace`.

Revision syntax understood by `rev_parse`: `HEAD` / `@`, branch names, tag names, full ref
paths (`refs/heads/x`), object ids or unique prefixes of at least 4 hex digits, `ORIG_HEAD`,
`MERGE_HEAD`, and any chain of `~N` (Nth first-parent ancestor) and `^N` (Nth parent) suffixes,
e.g. `main~2^2`. `diff` also accepts `A..B`.

## The index

Git's index is a binary file. mgit uses JSON so you can open it and see what is staged:

```json
{
 "version": 1,
 "entries": {
  "src/app.py": {"sha": "9f1b437537a2acdadafd3174f6f0af9c1a04f5e4", "mode": "100644", "size": 12, "mtime_ns": 1763460000123456789}
 }
}
```

Paths are POSIX-style and relative to the root on every OS. `size` and `mtime_ns` are a stat
cache. `status` only re-hashes a file when its size or mtime differ from the cached values.

**Racy-clean entries.** A file edited within the same timestamp tick as the index write can keep
its size and mtime while its content changes. To catch this, when the index is saved, any entry
whose mtime falls within 2 seconds of "now" gets `mtime_ns = 0`. That forces a re-hash next time.
When `status` later confirms by hashing that such a file is unchanged and no longer recent, it
writes the real stat values back. Git uses the same idea.

## Status = three snapshots

```
HEAD tree  --(staged changes)-->  index  --(unstaged changes)-->  working tree
```

* **staged:** paths whose `(mode, sha)` differs between the flattened HEAD tree and the index.
* **unstaged:** index entries whose file on disk is missing or hashes differently.
* **untracked:** files on disk (not ignored) that are not in the index.

## Diff

`mgit/diff.py` implements Myers' O(ND) greedy algorithm ("An O(ND) Difference Algorithm and
Its Variations", 1986):

1. Lines are interned to integers so the inner loop compares ints, not strings.
2. The common prefix and suffix are trimmed (the usual "small edit in a big file" case).
3. For `d = 0, 1, 2...` the algorithm keeps, for every diagonal `k = x - y`, the furthest `x`
   reachable with `d` edits. Each step moves right (delete) or down (insert), then follows the
   "snake" of equal lines diagonally. It stops at the first `d` that reaches `(n, m)`, so the
   edit script is the shortest one possible.
4. Each round's `V` slice is saved, and the path is walked backwards to recover the operations.

The tests check minimality against an independent LCS dynamic program on 300 random inputs:
`edits == len(a) + len(b) - 2 * LCS`.

Hunks use 3 lines of context and are merged when their contexts overlap. Headers follow the
unified format (`@@ -l,s +l,s @@`, with `,1` omitted and a zero-length range pointing at the
preceding line), and a missing final newline is marked `\ No newline at end of file`. A file
with a NUL byte in its first 8,000 bytes is reported as `Binary files ... differ`.

## Merge

1. **Merge base:** take all common ancestors of `HEAD` and the other commit, then drop the ones
   that are ancestors of another common ancestor. If more than one is left (criss-cross history),
   the newest is chosen.
2. **Fast-forward:** if `HEAD` is an ancestor of the other commit, mgit moves the branch and
   updates the files without creating a commit (unless `--no-ff` is given).
3. **Tree level, per path** (`b` = base, `o` = ours, `t` = theirs):
   `o == t` → keep · `o == b` → take theirs (including deletion) · `t == b` → keep ours ·
   one side deleted while the other modified → conflict · both modified → line merge.
4. **Line level (diff3):** diff `base→ours` and `base→theirs`. Base lines kept by both sides
   are *sync points*. For each region between sync points: if only one side changed it, take
   that side. If both made the same change, take it once. Otherwise write
   `<<<<<<< HEAD (branch)` / `=======` / `>>>>>>> other` markers.
5. A clean merge creates a commit with two parents straight away. A conflicted merge writes the
   markers to the working tree, saves `MERGE_HEAD`, `MERGE_MSG` and `MERGE_CONFLICTS`, and
   leaves the index at "ours" for the conflicted paths. `mgit add <file>` marks a path resolved.
   `mgit commit` refuses to run while any conflicted path remains, then records both parents.
   `mgit merge --abort` is `reset --hard` to `HEAD`, plus removal of the conflict files.

## Safety rules

* `checkout` and `merge` refuse to run with staged or unstaged changes, or while a merge is in
  progress. They also refuse when an untracked file would be overwritten by different content.
  `checkout -f` discards local changes on purpose.
* `checkout -b` (or switching to a branch that points at the same commit) only moves HEAD, so
  local changes are kept, as in git.
* `rm` refuses to delete a file that has local or staged changes unless `--cached` or `-f` is
  given.
* Paths outside the repository, and anything inside `.mgit/`, are rejected.
