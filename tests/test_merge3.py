"""Three-way text merge."""

from __future__ import annotations

import unittest

from mgit.merge import merge3


def L(text: str) -> list[str]:
    return text.splitlines(keepends=True)


class Merge3Tests(unittest.TestCase):
    def test_non_overlapping_changes_merge_cleanly(self):
        base = L("a\nb\nc\nd\ne\n")
        ours = L("A\nb\nc\nd\ne\n")
        theirs = L("a\nb\nc\nd\nE\n")
        res = merge3(base, ours, theirs)
        self.assertTrue(res.clean)
        self.assertEqual("".join(res.lines), "A\nb\nc\nd\nE\n")

    def test_identical_change_on_both_sides_is_not_a_conflict(self):
        base, same = L("a\nb\nc\n"), L("a\nB\nc\n")
        res = merge3(base, same, same)
        self.assertTrue(res.clean)
        self.assertEqual(res.lines, same)

    def test_overlapping_changes_conflict_with_markers(self):
        res = merge3(L("a\nb\nc\n"), L("a\nours\nc\n"), L("a\ntheirs\nc\n"), "HEAD", "feature")
        self.assertEqual(res.conflicts, 1)
        self.assertEqual("".join(res.lines), "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nc\n")

    def test_insertions_and_deletions(self):
        base = L("1\n2\n3\n4\n")
        ours = L("1\n3\n4\n")  # deleted 2
        theirs = L("1\n2\n3\n4\n5\n")  # appended 5
        res = merge3(base, ours, theirs)
        self.assertTrue(res.clean)
        self.assertEqual("".join(res.lines), "1\n3\n4\n5\n")

    def test_conflict_side_without_trailing_newline(self):
        res = merge3(L("x\n"), L("ours"), L("theirs"))
        self.assertEqual(res.conflicts, 1)
        self.assertIn("ours\n", res.lines)
        self.assertIn("=======\n", res.lines)


if __name__ == "__main__":
    unittest.main()
