"""End-to-end tests through the command-line entry point (output and exit codes)."""

from __future__ import annotations

import os
import subprocess
import sys

from mgit.errors import EXIT_ERROR, EXIT_NOT_A_REPO, EXIT_OK, EXIT_USAGE

from .helpers import TempDirTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliTests(TempDirTestCase):
    def init_with_commit(self) -> None:
        self.assertEqual(self.cli("init")[0], EXIT_OK)
        self.write("notes.txt", "alpha\nbeta\ngamma\n")
        self.cli("add", ".")
        self.cli("commit", "-m", "Add notes")

    def test_not_a_repository_exit_code(self):
        code, _, err = self.cli("status")
        self.assertEqual(code, EXIT_NOT_A_REPO)
        self.assertIn("fatal: not an mgit repository", err)
        self.assertIn("hint: run 'mgit init'", err)

    def test_usage_errors(self):
        self.assertEqual(self.cli()[0], EXIT_USAGE)
        self.assertEqual(self.cli("frobnicate")[0], EXIT_USAGE)
        self.assertEqual(self.cli("--version")[0], EXIT_OK)

    def test_init_add_commit_log_flow(self):
        code, out, _ = self.cli("init")
        self.assertIn("Initialized empty mgit repository", out)
        self.write("notes.txt", "alpha\n")
        code, out, _ = self.cli("status", "-s")
        self.assertEqual(out, "?? notes.txt\n")
        self.cli("add", "notes.txt")
        self.assertEqual(self.cli("status", "-s")[1], "A  notes.txt\n")
        code, out, _ = self.cli("commit", "-m", "First")
        self.assertEqual(code, EXIT_OK)
        self.assertRegex(out, r"^\[main \(root-commit\) [0-9a-f]{7}\] First\n")
        code, out, _ = self.cli("log", "--oneline")
        self.assertRegex(out, r"^[0-9a-f]{7} \(HEAD -> main\) First\n$")
        self.assertIn("nothing to commit, working tree clean", self.cli("status")[1])

    def test_diff_variants(self):
        self.init_with_commit()
        self.write("notes.txt", "alpha\nBETA\ngamma\n")
        code, out, _ = self.cli("diff")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n", out)
        self.assertEqual(self.cli("diff", "--cached")[1], "")  # nothing staged yet
        self.cli("add", "notes.txt")
        self.assertEqual(self.cli("diff")[1], "")
        self.assertIn("+BETA", self.cli("diff", "--cached")[1])
        self.cli("commit", "-m", "Shout")
        out = self.cli("diff", "HEAD~1", "HEAD")[1]
        self.assertIn("-beta\n+BETA", out)
        self.assertEqual(out, self.cli("diff", "HEAD~1..HEAD")[1])
        self.assertIn("notes.txt | 2 +-", self.cli("diff", "--stat", "HEAD~1", "HEAD")[1])

    def test_diff_new_and_binary_files(self):
        self.init_with_commit()
        self.write("new.txt", "fresh\n")
        self.write("img.bin", b"\x00\x01\x02")
        self.cli("add", ".")
        out = self.cli("diff", "--cached")[1]
        self.assertIn("new file mode 100644", out)
        self.assertIn("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+fresh", out)
        self.assertIn("Binary files a/img.bin and b/img.bin differ", out)

    def test_checkout_dirty_refusal_exit_code(self):
        self.init_with_commit()
        self.cli("branch", "other")
        self.cli("checkout", "other")
        self.write("notes.txt", "changed on other\n")
        self.cli("commit", "-m", "x")  # nothing staged -> error, tree stays dirty
        self.cli("add", ".")
        self.cli("commit", "-m", "Other change")
        self.write("notes.txt", "dirty\n")
        code, _, err = self.cli("checkout", "main")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("uncommitted changes", err)

    def test_merge_conflict_exit_code_and_status(self):
        self.init_with_commit()
        self.cli("checkout", "-b", "feature")
        self.write("notes.txt", "alpha\nfeature\ngamma\n")
        self.cli("add", ".")
        self.cli("commit", "-m", "feature edit")
        self.cli("checkout", "main")
        self.write("notes.txt", "alpha\nmain\ngamma\n")
        self.cli("add", ".")
        self.cli("commit", "-m", "main edit")
        code, out, _ = self.cli("merge", "feature")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("CONFLICT (content): Merge conflict in notes.txt", out)
        self.assertEqual(self.cli("status", "-s")[1], "UU notes.txt\n")
        self.write("notes.txt", "alpha\nresolved\ngamma\n")
        self.cli("add", "notes.txt")
        code, out, _ = self.cli("commit")
        self.assertEqual(code, EXIT_OK, out)
        graph = self.cli("log", "--oneline", "--graph")[1]
        self.assertIn("|\\", graph)
        self.assertIn("|/", graph)
        self.assertTrue(graph.startswith("* "))

    def test_cat_file_show_tag_and_branch_listing(self):
        self.init_with_commit()
        self.assertEqual(self.cli("cat-file", "-t", "HEAD")[1], "commit\n")
        out = self.cli("cat-file", "-p", "HEAD")[1]
        self.assertIn("author Test User <test@example.com>", out)
        tree = out.split("\n")[0].split()[1]
        self.assertRegex(self.cli("cat-file", "-p", tree)[1], r"100644 blob [0-9a-f]{40}\tnotes.txt")
        self.cli("tag", "-a", "v1", "-m", "first release")
        out = self.cli("show", "v1")[1]
        self.assertIn("tag v1", out)
        self.assertIn("+alpha", out)
        self.assertEqual(self.cli("tag")[1], "v1\n")
        self.cli("branch", "dev")
        self.assertEqual(self.cli("branch")[1], "  dev\n* main\n")

    def test_bad_revision_message(self):
        self.init_with_commit()
        code, _, err = self.cli("show", "doesnotexist")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("unknown revision 'doesnotexist'", err)

    def test_reset_paths_via_double_dash(self):
        self.init_with_commit()
        self.write("notes.txt", "edited\n")
        self.cli("add", ".")
        self.cli("reset", "--", "notes.txt")
        self.assertEqual(self.cli("status", "-s")[1], " M notes.txt\n")
        self.cli("checkout", "--", "notes.txt")
        self.assertEqual(self.cli("status", "-s")[1], "")

    def test_python_dash_m_entry_point(self):
        env = {**os.environ, "PYTHONPATH": ROOT}
        proc = subprocess.run([sys.executable, "-m", "mgit", "init"], cwd=self.tmp, capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Initialized", proc.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, ".mgit")))
