"""Repository behaviour: staging, status, commits, refs, checkout, reset and merges."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

from mgit import ops
from mgit.errors import MgitError, NotARepositoryError
from mgit.objects import hash_object
from mgit.repo import Repository

from .helpers import RepoTestCase, TempDirTestCase


class InitTests(TempDirTestCase):
    def test_init_creates_layout_and_is_idempotent(self):
        repo, existed = Repository.init(self.tmp)
        self.assertFalse(existed)
        for part in ("objects", "refs/heads", "refs/tags", "HEAD", "config"):
            self.assertTrue(os.path.exists(os.path.join(repo.mgit_dir, *part.split("/"))), part)
        self.assertEqual(repo.current_branch(), "main")
        self.assertIsNone(repo.head_commit())
        _, existed = Repository.init(self.tmp)
        self.assertTrue(existed)

    def test_discover_walks_up_and_fails_outside(self):
        Repository.init(self.tmp)
        os.makedirs(os.path.join(self.tmp, "a", "b"))
        self.assertEqual(Repository.discover(os.path.join(self.tmp, "a", "b")).root, os.path.abspath(self.tmp))
        outside = os.path.join(self.tmp, "..")
        with self.assertRaises(NotARepositoryError):
            Repository.discover(os.path.join(outside, os.path.basename(self.tmp) + "-nope-does-not-exist"))


class StagingAndStatusTests(RepoTestCase):
    def test_add_stores_blob_and_index_entry(self):
        self.write("hello.txt", "hello\n")
        ops.add(self.repo, ["hello.txt"])
        entry = self.repo.load_index().entries["hello.txt"]
        self.assertEqual(entry.sha, "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertTrue(self.repo.store.exists(entry.sha))

    def test_add_directory_recursively(self):
        self.write("src/pkg/a.py", "a\n")
        self.write("src/b.py", "b\n")
        self.write("top.txt", "t\n")
        ops.add(self.repo, ["src"])
        self.assertEqual(sorted(self.repo.load_index().entries), ["src/b.py", "src/pkg/a.py"])

    def test_add_unknown_path_fails(self):
        with self.assertRaises(MgitError):
            ops.add(self.repo, ["nope.txt"])

    def test_status_reports_staged_modified_untracked(self):
        self.commit_files("init", a="1\n", b="2\n", c="3\n")
        self.write("a", "changed\n")  # modified, unstaged
        self.write("b", "staged\n")
        ops.add(self.repo, ["b"])  # modified, staged
        os.remove(os.path.join(self.tmp, "c"))  # deleted, unstaged
        self.write("new.txt", "n\n")  # untracked
        st = self.repo.status()
        self.assertEqual(st.staged, {"b": "modified"})
        self.assertEqual(st.unstaged, {"a": "modified", "c": "deleted"})
        self.assertEqual(st.untracked, ["new.txt"])
        self.assertFalse(st.clean)

    def test_same_size_edit_right_after_add_is_detected(self):
        # Racy-clean protection: same size, possibly same mtime tick.
        self.write("f.txt", "aaaa\n")
        ops.add(self.repo, ["f.txt"])
        self.write("f.txt", "bbbb\n")
        self.assertEqual(self.repo.status().unstaged, {"f.txt": "modified"})

    def test_add_stages_deletion_and_rm(self):
        self.commit_files("init", a="1\n", b="2\n")
        os.remove(os.path.join(self.tmp, "a"))
        ops.add(self.repo, ["."])
        ops.rm(self.repo, ["b"])
        self.assertFalse(self.exists("b"))
        self.assertEqual(self.repo.status().staged, {"a": "deleted", "b": "deleted"})

    def test_rm_refuses_modified_file_unless_cached(self):
        self.commit_files("init", a="1\n")
        self.write("a", "local edit\n")
        with self.assertRaises(MgitError):
            ops.rm(self.repo, ["a"])
        ops.rm(self.repo, ["a"], cached=True)
        self.assertTrue(self.exists("a"))
        self.assertEqual(self.repo.status().untracked, ["a"])

    def test_reset_paths_unstages(self):
        self.commit_files("init", a="1\n")
        self.write("a", "2\n")
        ops.add(self.repo, ["a"])
        ops.reset_paths(self.repo, ["a"])
        st = self.repo.status()
        self.assertEqual(st.staged, {})
        self.assertEqual(st.unstaged, {"a": "modified"})


class CommitTests(RepoTestCase):
    def test_commit_builds_nested_trees_and_moves_branch(self):
        sha = self.commit_files("first", **{"README.md": "# hi\n", "src__app.py": "x = 1\n"})
        self.assertEqual(self.repo.read_ref("refs/heads/main"), sha)
        c = self.repo.read_commit(sha)
        self.assertEqual(c.parents, [])
        self.assertEqual(c.author.name, "Test User")
        top = {e.name: e for e in self.repo.read_tree(c.tree)}
        self.assertTrue(top["src"].is_tree)
        self.assertEqual(self.repo.flatten_tree(c.tree)["src/app.py"][1], hash_object("blob", b"x = 1\n"))

    def test_commit_chain_and_rev_parse(self):
        c1 = self.commit_files("one", a="1\n")
        c2 = self.commit_files("two", a="2\n")
        c3 = self.commit_files("three", a="3\n")
        self.assertEqual(self.repo.read_commit(c3).parents, [c2])
        self.assertEqual(self.repo.rev_parse("HEAD~2"), c1)
        self.assertEqual(self.repo.rev_parse("HEAD^"), c2)
        self.assertEqual(self.repo.rev_parse("main"), c3)
        self.assertEqual(self.repo.rev_parse(c1[:7]), c1)
        with self.assertRaises(MgitError):
            self.repo.rev_parse("HEAD~5")
        with self.assertRaises(MgitError):
            self.repo.rev_parse("no-such-branch")

    def test_nothing_to_commit_and_empty_message(self):
        self.commit_files("one", a="1\n")
        with self.assertRaises(MgitError):
            ops.commit(self.repo, "again")
        with self.assertRaises(MgitError):
            ops.commit(self.repo, "  ")
        self.assertTrue(ops.commit(self.repo, "empty ok", allow_empty=True))

    def test_commits_are_reproducible_with_fixed_date(self):
        os.environ["MGIT_DATE"] = "1700000000 +0000"
        a = self.commit_files("same", f="x\n")
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        repo2, _ = Repository.init(other)
        with open(os.path.join(other, "f"), "w", encoding="utf-8", newline="") as fh:
            fh.write("x\n")
        ops.add(repo2, [os.path.join(other, "f")])
        self.assertEqual(ops.commit(repo2, "same"), a)

    def test_history_order(self):
        c1 = self.commit_files("one", a="1\n")
        c2 = self.commit_files("two", a="2\n")
        self.assertEqual([s for s, _ in ops.iter_history(self.repo, [c2])], [c2, c1])


class BranchCheckoutTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.commit_files("base", a="base\n", shared="s\n")

    def test_branch_create_list_delete(self):
        ops.create_branch(self.repo, "feature")
        self.assertEqual(set(self.repo.branches()), {"main", "feature"})
        with self.assertRaises(MgitError):
            ops.create_branch(self.repo, "feature")
        with self.assertRaises(MgitError):
            ops.create_branch(self.repo, "bad..name")
        with self.assertRaises(MgitError):
            ops.delete_branch(self.repo, "main")  # checked out
        ops.delete_branch(self.repo, "feature")
        self.assertEqual(set(self.repo.branches()), {"main"})

    def test_unmerged_branch_needs_force_delete(self):
        ops.checkout(self.repo, "", new_branch="topic")
        self.commit_files("topic work", t="t\n")
        ops.checkout(self.repo, "main")
        with self.assertRaises(MgitError):
            ops.delete_branch(self.repo, "topic")
        ops.delete_branch(self.repo, "topic", force=True)

    def test_checkout_switches_files(self):
        ops.checkout(self.repo, "", new_branch="feature")
        self.commit_files("feature", a="feature\n", only_feature="f\n")
        ops.checkout(self.repo, "main")
        self.assertEqual(self.read("a"), "base\n")
        self.assertFalse(self.exists("only_feature"))
        ops.checkout(self.repo, "feature")
        self.assertEqual(self.read("only_feature"), "f\n")
        self.assertEqual(self.repo.current_branch(), "feature")
        self.assertTrue(self.repo.status().clean)

    def test_checkout_refuses_when_dirty(self):
        ops.create_branch(self.repo, "other")
        ops.checkout(self.repo, "other")
        self.commit_files("other", a="other\n")
        self.write("a", "uncommitted\n")
        with self.assertRaises(MgitError) as cm:
            ops.checkout(self.repo, "main")
        self.assertIn("uncommitted changes", str(cm.exception))
        self.assertEqual(self.read("a"), "uncommitted\n")  # nothing was touched
        ops.checkout(self.repo, "main", force=True)
        self.assertEqual(self.read("a"), "base\n")

    def test_checkout_refuses_to_clobber_untracked_file(self):
        ops.checkout(self.repo, "", new_branch="feature")
        self.commit_files("adds x", x="from feature\n")
        ops.checkout(self.repo, "main")
        self.write("x", "precious untracked work\n")
        with self.assertRaises(MgitError):
            ops.checkout(self.repo, "feature")
        self.assertEqual(self.read("x"), "precious untracked work\n")

    def test_detached_checkout_of_commit(self):
        second = self.commit_files("second", a="second\n")
        res = ops.checkout(self.repo, self.base[:8])
        self.assertIsNone(res.branch)
        self.assertIsNone(self.repo.current_branch())
        self.assertEqual(self.repo.head_commit(), self.base)
        self.assertEqual(self.read("a"), "base\n")
        ops.checkout(self.repo, "main")
        self.assertEqual(self.repo.head_commit(), second)

    def test_restore_path_discards_changes(self):
        self.write("a", "oops\n")
        ops.restore_paths(self.repo, ["a"])
        self.assertEqual(self.read("a"), "base\n")


class ResetAndTagTests(RepoTestCase):
    def test_reset_modes(self):
        c1 = self.commit_files("one", a="1\n")
        self.commit_files("two", a="2\n")
        ops.reset(self.repo, "HEAD~1", "soft")
        self.assertEqual(self.repo.head_commit(), c1)
        self.assertEqual(self.repo.status().staged, {"a": "modified"})  # index kept
        ops.reset(self.repo, "HEAD", "mixed")
        st = self.repo.status()
        self.assertEqual((st.staged, st.unstaged), ({}, {"a": "modified"}))
        ops.reset(self.repo, "HEAD", "hard")
        self.assertEqual(self.read("a"), "1\n")
        self.assertTrue(self.repo.status().clean)

    def test_reset_hard_removes_files_added_after_target(self):
        c1 = self.commit_files("one", a="1\n")
        self.commit_files("two", b="new\n")
        ops.reset(self.repo, c1, "hard")
        self.assertFalse(self.exists("b"))

    def test_lightweight_and_annotated_tags(self):
        c1 = self.commit_files("one", a="1\n")
        ops.create_tag(self.repo, "v0.1")
        tag_obj = ops.create_tag(self.repo, "v1.0", "HEAD", message="Release 1.0")
        self.assertEqual(self.repo.read_ref("refs/tags/v0.1"), c1)
        self.assertEqual(self.repo.store.read(tag_obj)[0], "tag")
        self.assertEqual(self.repo.rev_parse_commit("v1.0"), c1)
        with self.assertRaises(MgitError):
            ops.create_tag(self.repo, "v1.0")
        ops.delete_tag(self.repo, "v0.1")
        self.assertEqual(list(self.repo.tags()), ["v1.0"])


class MergeTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.base = self.commit_files("base", f="1\n2\n3\n4\n5\n6\n7\n", other="o\n")
        ops.create_branch(self.repo, "feature")

    def _on_feature(self, **files):
        ops.checkout(self.repo, "feature")
        sha = self.commit_files("feature work", **files)
        ops.checkout(self.repo, "main")
        return sha

    def test_fast_forward(self):
        feat = self._on_feature(f="1\n2\n3\n4\n5\n6\nseven\n")
        res = ops.merge(self.repo, "feature")
        self.assertEqual(res.kind, "fast-forward")
        self.assertEqual(self.repo.head_commit(), feat)
        self.assertEqual(self.read("f"), "1\n2\n3\n4\n5\n6\nseven\n")

    def test_already_up_to_date(self):
        self.assertEqual(ops.merge(self.repo, "feature").kind, "up-to-date")

    def test_clean_three_way_merge_creates_merge_commit(self):
        feat = self._on_feature(f="1\n2\n3\n4\n5\n6\nseven\n", added="from feature\n")
        main = self.commit_files("main work", f="one\n2\n3\n4\n5\n6\n7\n")
        res = ops.merge(self.repo, "feature")
        self.assertEqual(res.kind, "merge")
        c = self.repo.read_commit(res.sha)
        self.assertEqual(c.parents, [main, feat])
        self.assertEqual(self.read("f"), "one\n2\n3\n4\n5\n6\nseven\n")
        self.assertEqual(self.read("added"), "from feature\n")
        self.assertEqual(ops.merge_base(self.repo, main, feat), self.base)
        self.assertTrue(self.repo.status().clean)

    def test_conflict_then_resolve_and_commit(self):
        feat = self._on_feature(f="1\n2\nFEATURE\n4\n5\n6\n7\n")
        main = self.commit_files("main work", f="1\n2\nMAIN\n4\n5\n6\n7\n")
        res = ops.merge(self.repo, "feature")
        self.assertEqual((res.kind, res.conflicts), ("conflict", ["f"]))
        text = self.read("f")
        self.assertIn("<<<<<<< HEAD (main)\nMAIN\n=======\nFEATURE\n>>>>>>> feature\n", text)
        st = self.repo.status()
        self.assertEqual(st.conflicts, ["f"])
        self.assertEqual(st.merging, feat)
        with self.assertRaises(MgitError):
            ops.commit(self.repo, None)  # unresolved
        with self.assertRaises(MgitError):
            ops.checkout(self.repo, "feature")  # merge in progress
        self.write("f", "1\n2\nBOTH\n4\n5\n6\n7\n")
        ops.add(self.repo, ["f"])
        sha = ops.commit(self.repo, None)  # uses MERGE_MSG
        c = self.repo.read_commit(sha)
        self.assertEqual(c.parents, [main, feat])
        self.assertIn("Merge branch 'feature'", c.message)
        self.assertIsNone(self.repo.merge_head())

    def test_merge_abort_restores_head(self):
        self._on_feature(f="x\n")
        main = self.commit_files("main work", f="y\n")
        ops.merge(self.repo, "feature")
        ops.merge_abort(self.repo)
        self.assertEqual(self.repo.head_commit(), main)
        self.assertEqual(self.read("f"), "y\n")
        self.assertTrue(self.repo.status().clean)

    def test_modify_delete_conflict(self):
        ops.checkout(self.repo, "feature")
        ops.rm(self.repo, ["other"])
        ops.commit(self.repo, "delete other")
        ops.checkout(self.repo, "main")
        self.commit_files("edit other", other="edited\n")
        res = ops.merge(self.repo, "feature")
        self.assertEqual(res.conflicts, ["other"])
        self.assertEqual(self.read("other"), "edited\n")

    def test_merge_refuses_dirty_tree(self):
        self._on_feature(f="x\n")
        self.commit_files("main", other="m\n")
        self.write("other", "dirty\n")
        with self.assertRaises(MgitError):
            ops.merge(self.repo, "feature")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitCompatibilityTests(RepoTestCase):
    """mgit writes byte-identical objects to git: same blob, tree and commit ids."""

    def _git(self, *args: str, env: dict | None = None) -> str:
        full_env = {**os.environ, **(env or {})}
        return subprocess.run(["git", *args], cwd=self.git_dir, check=True, capture_output=True, text=True, env=full_env).stdout.strip()

    def test_tree_and_commit_ids_match_real_git(self):
        files = {"README.md": "# demo\n", "src/app.py": "print('hi')\n", "src/util/x.txt": "x\n", "a.txt": "a\n"}
        self.git_dir = os.path.join(self.tmp, "gitrepo")
        os.makedirs(self.git_dir)
        self._git("init", "-q")
        self._git("config", "core.autocrlf", "false")
        for rel, content in files.items():
            for base in (self.tmp, self.git_dir):
                path = os.path.join(base, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(content)
        self._git("add", ".")
        git_tree = self._git("write-tree")
        os.environ["MGIT_DATE"] = "1700000000 +0000"
        ops.add(self.repo, list(files))
        mgit_commit = ops.commit(self.repo, "Initial commit")
        self.assertEqual(self.repo.read_commit(mgit_commit).tree, git_tree)
        git_env = {
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": "1700000000 +0000",
            "GIT_COMMITTER_DATE": "1700000000 +0000",
        }
        git_commit = self._git("commit-tree", git_tree, "-m", "Initial commit", env=git_env)
        self.assertEqual(mgit_commit, git_commit)


if __name__ == "__main__":
    unittest.main()
