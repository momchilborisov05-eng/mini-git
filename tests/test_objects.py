"""Object model: hashing, framing, compression and serialisation round trips."""

from __future__ import annotations

import os
import zlib

from mgit.errors import MgitError
from mgit.objects import (
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

from .helpers import TempDirTestCase

SIG = Signature("Ada Lovelace", "ada@example.com", 1700000000, "+0000")


class HashingTests(TempDirTestCase):
    def test_blob_ids_match_git(self):
        # Well-known ids: `printf 'hello\n' | git hash-object --stdin` and the empty blob.
        self.assertEqual(hash_object("blob", b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(hash_object("blob", b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")

    def test_empty_tree_id_matches_git(self):
        self.assertEqual(hash_object("tree", serialize_tree([])), "4b825dc642cb6eb9a060e54bf8d69288fbee4904")

    def test_hashing_is_deterministic_and_content_sensitive(self):
        a1 = hash_object("blob", b"same bytes")
        a2 = hash_object("blob", b"same bytes")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, hash_object("blob", b"same bytes!"))
        self.assertNotEqual(a1, hash_object("tree", b"same bytes"))  # the type is part of the hash


class StoreTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.store = ObjectStore(os.path.join(self.tmp, "objects"))

    def test_write_read_round_trip_and_layout(self):
        sha = self.store.write("blob", b"payload\n")
        self.assertEqual(sha, hash_object("blob", b"payload\n"))
        path = os.path.join(self.tmp, "objects", sha[:2], sha[2:])
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            self.assertEqual(zlib.decompress(fh.read()), b"blob 8\0payload\n")
        self.assertEqual(self.store.read(sha), ("blob", b"payload\n"))

    def test_identical_content_is_stored_once(self):
        a = self.store.write("blob", b"x" * 1000)
        b = self.store.write("blob", b"x" * 1000)
        self.assertEqual(a, b)
        self.assertEqual(list(self.store.iter_ids()), [a])

    def test_corruption_is_detected(self):
        sha = self.store.write("blob", b"original")
        with open(self.store.path_for(sha), "wb") as fh:
            fh.write(zlib.compress(b"blob 8\0tampered"))
        with self.assertRaises(MgitError):
            self.store.read(sha)

    def test_missing_object_raises(self):
        with self.assertRaises(MgitError):
            self.store.read("0" * 40)

    def test_find_prefix(self):
        sha = self.store.write("blob", b"abc")
        self.assertEqual(self.store.find_prefix(sha[:6]), [sha])
        self.assertEqual(self.store.find_prefix(sha[:3]), [])  # too short to be meaningful


class SerialisationTests(TempDirTestCase):
    def test_tree_round_trip_and_git_sort_order(self):
        blob = hash_object("blob", b"x")
        entries = [
            TreeEntry(MODE_FILE, "a0", blob),
            TreeEntry(MODE_TREE, "a", blob),
            TreeEntry(MODE_FILE, "a.txt", blob),
        ]
        parsed = parse_tree(serialize_tree(entries))
        # git compares "a/" for trees, so: a.txt < a/ < a0
        self.assertEqual([e.name for e in parsed], ["a.txt", "a", "a0"])
        self.assertEqual(parsed[1], TreeEntry(MODE_TREE, "a", blob))

    def test_tree_rejects_bad_names(self):
        with self.assertRaises(ValueError):
            serialize_tree([TreeEntry(MODE_FILE, "a/b", "0" * 40)])

    def test_commit_round_trip(self):
        c = Commit("1" * 40, ["2" * 40, "3" * 40], SIG, SIG, "Subject line\n\nBody text.\n")
        parsed = Commit.parse(c.serialize())
        self.assertEqual(parsed, c)
        self.assertEqual(parsed.summary, "Subject line")
        self.assertTrue(c.serialize().startswith(b"tree 111"))

    def test_tag_round_trip(self):
        t = Tag("1" * 40, "commit", "v1.0", SIG, "Release 1.0\n")
        self.assertEqual(Tag.parse(t.serialize()), t)

    def test_signature_parse(self):
        sig = Signature.parse("Grace Hopper <grace@navy.mil> 1700000123 -0500")
        self.assertEqual((sig.name, sig.email, sig.timestamp, sig.tz), ("Grace Hopper", "grace@navy.mil", 1700000123, "-0500"))
