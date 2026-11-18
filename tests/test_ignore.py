"""Ignore rules and how add/status honour them."""

from __future__ import annotations

import unittest

from mgit import ops
from mgit.errors import MgitError
from mgit.ignore import IgnoreRules

from .helpers import RepoTestCase


class IgnoreRuleTests(unittest.TestCase):
    def test_basename_glob_matches_at_any_depth(self):
        r = IgnoreRules(["*.log"])
        self.assertTrue(r.is_ignored("app.log"))
        self.assertTrue(r.is_ignored("deep/dir/app.log"))
        self.assertFalse(r.is_ignored("app.log.txt"))

    def test_directory_only_and_anchored_patterns(self):
        r = IgnoreRules(["build/", "/TODO"])
        self.assertTrue(r.is_ignored("build", is_dir=True))
        self.assertTrue(r.is_ignored("build/out.bin"))  # inside an ignored directory
        self.assertFalse(r.is_ignored("build"))  # a *file* called build is not a directory
        self.assertTrue(r.is_ignored("TODO"))
        self.assertFalse(r.is_ignored("docs/TODO"))  # anchored to the root only

    def test_negation_and_last_match_wins(self):
        r = IgnoreRules(["*.txt", "!keep.txt", "# comment", ""])
        self.assertTrue(r.is_ignored("notes.txt"))
        self.assertFalse(r.is_ignored("keep.txt"))

    def test_double_star(self):
        r = IgnoreRules(["**/cache/**", "docs/**/*.tmp"])
        self.assertTrue(r.is_ignored("a/b/cache/x.bin"))
        self.assertTrue(r.is_ignored("docs/x.tmp"))
        self.assertTrue(r.is_ignored("docs/a/b/x.tmp"))
        self.assertFalse(r.is_ignored("src/x.tmp"))

    def test_mgit_dir_always_ignored(self):
        self.assertTrue(IgnoreRules([]).is_ignored(".mgit/HEAD"))


class IgnoreIntegrationTests(RepoTestCase):
    def test_add_dot_skips_ignored_and_status_hides_them(self):
        self.write(".mgitignore", "*.log\nnode_modules/\n")
        self.write("app.py", "print('hi')\n")
        self.write("debug.log", "noise\n")
        self.write("node_modules/pkg/index.js", "x\n")
        ops.add(self.repo, ["."])
        self.assertEqual(sorted(self.repo.load_index().entries), [".mgitignore", "app.py"])
        self.assertEqual(self.repo.status().untracked, [])

    def test_explicitly_adding_an_ignored_file_needs_force(self):
        self.write(".mgitignore", "*.log\n")
        self.write("debug.log", "noise\n")
        with self.assertRaises(MgitError):
            ops.add(self.repo, ["debug.log"])
        ops.add(self.repo, ["debug.log"], force=True)
        self.assertIn("debug.log", self.repo.load_index().entries)
