"""Myers diff correctness, checked against an independent LCS dynamic programme."""

from __future__ import annotations

import random
import unittest

from mgit.diff import DELETE, EQUAL, INSERT, NO_NEWLINE, edit_distance, myers_diff, unified_hunks


def lcs_length(a, b) -> int:
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def apply_ops(ops) -> tuple[list[str], list[str]]:
    old = [op.line for op in ops if op.tag != INSERT]
    new = [op.line for op in ops if op.tag != DELETE]
    return old, new


class MyersTests(unittest.TestCase):
    def test_classic_example_from_the_paper(self):
        a, b = list("ABCABBA"), list("CBABAC")
        self.assertEqual(edit_distance(a, b), 5)  # D = 5 in Myers (1986)

    def test_identical_and_empty_inputs(self):
        self.assertTrue(all(op.tag == EQUAL for op in myers_diff(["a", "b"], ["a", "b"])))
        self.assertEqual([op.tag for op in myers_diff([], ["x", "y"])], [INSERT, INSERT])
        self.assertEqual([op.tag for op in myers_diff(["x"], [])], [DELETE])
        self.assertEqual(myers_diff([], []), [])

    def test_script_reconstructs_both_sides_and_is_minimal(self):
        rng = random.Random(1234)
        for _ in range(300):
            a = [rng.choice("abcde") for _ in range(rng.randint(0, 25))]
            b = [rng.choice("abcde") for _ in range(rng.randint(0, 25))]
            ops = myers_diff(a, b)
            self.assertEqual(apply_ops(ops), (a, b))
            # A shortest edit script deletes/inserts exactly the lines not in an LCS.
            self.assertEqual(edit_distance(a, b), len(a) + len(b) - 2 * lcs_length(a, b))

    def test_indices_point_at_the_right_lines(self):
        a, b = ["x\n", "y\n", "z\n"], ["x\n", "q\n", "z\n"]
        for op in myers_diff(a, b):
            if op.tag != INSERT:
                self.assertEqual(a[op.a_index], op.line)
            if op.tag != DELETE:
                self.assertEqual(b[op.b_index], op.line)


class UnifiedFormatTests(unittest.TestCase):
    def test_single_change_hunk(self):
        a = ["one\n", "two\n", "three\n"]
        b = ["one\n", "TWO\n", "three\n"]
        self.assertEqual(unified_hunks(a, b), ["@@ -1,3 +1,3 @@", " one", "-two", "+TWO", " three"])

    def test_distant_changes_make_separate_hunks(self):
        a = [f"{i}\n" for i in range(1, 21)]
        b = list(a)
        b[1], b[17] = "two\n", "eighteen\n"
        hunks = [h for h in unified_hunks(a, b) if h.startswith("@@")]
        self.assertEqual(hunks, ["@@ -1,5 +1,5 @@", "@@ -15,6 +15,6 @@"])

    def test_close_changes_share_one_hunk(self):
        a = [f"{i}\n" for i in range(1, 11)]
        b = list(a)
        b[2], b[6] = "x\n", "y\n"
        self.assertEqual(sum(1 for h in unified_hunks(a, b) if h.startswith("@@")), 1)

    def test_new_file_and_deleted_file_ranges(self):
        self.assertEqual(unified_hunks([], ["a\n", "b\n"])[0], "@@ -0,0 +1,2 @@")
        self.assertEqual(unified_hunks(["a\n"], [])[0], "@@ -1 +0,0 @@")

    def test_missing_trailing_newline_is_marked(self):
        hunks = unified_hunks(["a\n", "b"], ["a\n", "b\n"])
        self.assertEqual(hunks, ["@@ -1,2 +1,2 @@", " a", "-b", NO_NEWLINE, "+b"])

    def test_no_changes_no_hunks(self):
        self.assertEqual(unified_hunks(["a\n"], ["a\n"]), [])


if __name__ == "__main__":
    unittest.main()
