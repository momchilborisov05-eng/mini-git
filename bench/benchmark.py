"""Reproducible micro-benchmark for mgit.

    python bench/benchmark.py            # prints a Markdown table
    python bench/benchmark.py --files 2000 --runs 5

Generates a deterministic synthetic project (source-like text files), then times the core
operations in-process (no interpreter start-up included) and measures the object store size.
Each timing is the median of ``--runs`` fresh repositories. If ``git`` is installed, the same
add+commit is timed with real git for scale.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mgit import ops  # noqa: E402
from mgit.diff import myers_diff  # noqa: E402
from mgit.index import RACY_WINDOW_NS  # noqa: E402
from mgit.repo import Repository  # noqa: E402

WORDS = "def return class import self value result data index commit tree blob merge branch status diff".split()


def make_file(rng: random.Random) -> str:
    lines = []
    for _ in range(rng.randint(20, 120)):
        indent = "    " * rng.randint(0, 3)
        lines.append(indent + " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 9))))
    return "\n".join(lines) + "\n"


def make_project(root: str, n_files: int, seed: int = 42) -> int:
    rng = random.Random(seed)
    raw = 0
    for i in range(n_files):
        rel = os.path.join(f"pkg{i % 20:02d}", f"sub{i % 7}", f"module_{i:04d}.py")
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = make_file(rng).encode()
        raw += len(data)
        with open(path, "wb") as fh:
            fh.write(data)
    return raw


def dir_size(path: str) -> tuple[int, int]:
    total = count = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(dirpath, f))
            count += 1
    return total, count


def timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def run_once(n_files: int) -> dict:
    tmp = tempfile.mkdtemp(prefix="mgit-bench-")
    try:
        os.environ.setdefault("MGIT_AUTHOR_NAME", "Bench")
        os.environ.setdefault("MGIT_AUTHOR_EMAIL", "bench@example.com")
        raw = make_project(tmp, n_files)
        r: dict = {"raw_bytes": raw}
        # Baseline: a fresh copy of the project, read once with plain open() - the floor for `add`.
        base_dir = os.path.join(tmp, "..", os.path.basename(tmp) + "-baseline")
        make_project(base_dir, n_files)
        paths = [os.path.join(dp, f) for dp, _d, fs in os.walk(base_dir) for f in fs]
        r["read_baseline"] = timed(lambda: [open(p, "rb").read() for p in paths])  # noqa: SIM115
        shutil.rmtree(base_dir, ignore_errors=True)
        repo, _ = Repository.init(tmp)
        r["add"] = timed(lambda: ops.add(repo, [tmp]))
        r["commit"] = timed(lambda: ops.commit(repo, "initial import"))
        r["status_cold"] = timed(repo.status)  # entries are racily clean -> every file re-hashed
        time.sleep(RACY_WINDOW_NS / 1e9 + 0.2)
        repo.status()  # refreshes the stat cache
        r["status_warm"] = timed(repo.status)  # stat-only fast path
        r["store_bytes"], r["objects"] = dir_size(repo.store.root)

        # second commit touching 1% of files: only changed blobs and their trees are new
        files = repo.walk_files()
        for rel in files[:: max(1, len(files) // (n_files // 100 or 1))][: n_files // 100 or 1]:
            with open(repo.abspath(rel), "a", encoding="utf-8") as fh:
                fh.write("# edited\n")
        r["add_commit_1pct"] = timed(lambda: (ops.add(repo, [tmp]), ops.commit(repo, "edit 1%")))
        store2, objects2 = dir_size(repo.store.root)
        r["second_commit_new_objects"] = objects2 - r["objects"]
        r["second_commit_new_bytes"] = store2 - r["store_bytes"]

        # branch switch where 10% of files differ
        ops.checkout(repo, "", new_branch="feature")
        for rel in files[: n_files // 10]:
            with open(repo.abspath(rel), "a", encoding="utf-8") as fh:
                fh.write("# feature\n")
        ops.add(repo, [tmp])
        ops.commit(repo, "feature work")
        r["checkout_10pct"] = timed(lambda: ops.checkout(repo, "main"))
        r["log_3"] = timed(lambda: list(ops.iter_history(repo, [repo.head_commit()])))
        return r
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bench_diff() -> dict:
    rng = random.Random(7)
    a = [f"line {i} {rng.choice(WORDS)}\n" for i in range(10_000)]
    b = list(a)
    for i in rng.sample(range(10_000), 100):  # 1% of lines changed, scattered
        b[i] = "changed " + b[i]
    t_small = timed(lambda: myers_diff(a, b))
    c = [f"other {i}\n" for i in range(2_000)]
    d = [f"other {i}\n" if i % 2 else f"different {i}\n" for i in range(2_000)]
    t_heavy = timed(lambda: myers_diff(c, d))
    return {"diff_10k_1pct": t_small, "diff_2k_50pct": t_heavy}


def bench_git(n_files: int) -> float | None:
    if not shutil.which("git"):
        return None
    tmp = tempfile.mkdtemp(prefix="git-bench-")
    try:
        make_project(tmp, n_files)
        env = {**os.environ, "GIT_AUTHOR_NAME": "B", "GIT_AUTHOR_EMAIL": "b@x", "GIT_COMMITTER_NAME": "B", "GIT_COMMITTER_EMAIL": "b@x"}

        def go():
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=env)
            subprocess.run(["git", "-c", "core.autocrlf=false", "add", "."], cwd=tmp, check=True, env=env)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp, check=True, env=env)

        return timed(go)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ms(seconds: float) -> str:
    return f"{seconds * 1000:,.0f} ms"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=1000)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    runs = [run_once(args.files) for _ in range(args.runs)]
    med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
    diff = bench_diff()
    git_time = bench_git(args.files)

    print(f"Environment: Python {platform.python_version()} on {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"Workload: {args.files:,} generated text files, {med['raw_bytes'] / 1024:,.0f} KiB raw; median of {args.runs} runs\n")
    print("| Operation | Result |")
    print("|---|---|")
    print(f"| `add` {args.files:,} files (hash + compress + write blobs, write index) | {ms(med['add'])} |")
    print(f"| Baseline: just reading the same {args.files:,} fresh files with `open()` | {ms(med['read_baseline'])} |")
    print(f"| `commit` (build {med['objects'] - args.files:.0f} tree objects + commit) | {ms(med['commit'])} |")
    print(f"| **add + commit {args.files:,} files** | **{ms(med['add'] + med['commit'])}** |")
    print(f"| `status`, cold (racily-clean entries re-hashed) | {ms(med['status_cold'])} |")
    print(f"| `status`, warm (stat cache hit) | {ms(med['status_warm'])} |")
    print(f"| `add` + `commit` after editing 1% of files | {ms(med['add_commit_1pct'])} |")
    print(f"| `checkout` a branch where 10% of files differ | {ms(med['checkout_10pct'])} |")
    print(f"| Myers diff, 10,000 lines with 100 scattered edits | {ms(diff['diff_10k_1pct'])} |")
    print(f"| Myers diff, 2,000 lines with 50% changed (worst-ish case) | {ms(diff['diff_2k_50pct'])} |")
    print(f"| Object store after first commit | {med['store_bytes'] / 1024:,.0f} KiB in {med['objects']:.0f} objects |")
    print(f"| Store size vs raw content | {med['store_bytes'] / med['raw_bytes']:.1%} |")
    new_objs, new_kib = med["second_commit_new_objects"], med["second_commit_new_bytes"] / 1024
    print(f"| Second commit (1% edited): new objects / bytes | {new_objs:.0f} / {new_kib:,.1f} KiB |")
    if git_time is not None:
        print(f"| Reference: real `git init && git add . && git commit` | {ms(git_time)} |")


if __name__ == "__main__":
    main()
