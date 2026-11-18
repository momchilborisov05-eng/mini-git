"""Shared fixtures: every test runs in its own temporary directory with a fixed identity."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from mgit.cli import main
from mgit.repo import Repository

ENV = {
    "MGIT_AUTHOR_NAME": "Test User",
    "MGIT_AUTHOR_EMAIL": "test@example.com",
    "MGIT_COLOR": "never",
}


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mgit-test-")
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self._old_env = {k: os.environ.get(k) for k in [*ENV, "MGIT_DATE", "NO_COLOR"]}
        os.environ.update(ENV)
        os.environ.pop("MGIT_DATE", None)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, content: str | bytes) -> str:
        path = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": ""}
        with open(path, mode, **kwargs) as fh:
            fh.write(content)
        return path

    def read(self, rel: str) -> str:
        with open(os.path.join(self.tmp, *rel.split("/")), encoding="utf-8", newline="") as fh:
            return fh.read()

    def exists(self, rel: str) -> bool:
        return os.path.exists(os.path.join(self.tmp, *rel.split("/")))

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()


class RepoTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo, _ = Repository.init(self.tmp)

    def commit_files(self, message: str, **files: str) -> str:
        from mgit import ops

        for rel, content in files.items():
            self.write(rel.replace("__", "/"), content)
        ops.add(self.repo, ["."])
        return ops.commit(self.repo, message)
