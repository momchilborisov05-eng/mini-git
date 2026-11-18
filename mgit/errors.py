"""Exception types and exit codes shared by the library and the CLI."""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1  # a command failed (bad revision, dirty tree, merge conflict...)
EXIT_USAGE = 2  # bad command-line arguments (argparse uses 2 as well)
EXIT_NOT_A_REPO = 128  # same code git uses for "not a git repository"


class MgitError(Exception):
    """A user-facing error. The CLI prints the message and exits with ``code``."""

    def __init__(self, message: str, code: int = EXIT_ERROR, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class NotARepositoryError(MgitError):
    def __init__(self, path: str):
        super().__init__(
            f"not an mgit repository (or any of the parent directories): {path}",
            code=EXIT_NOT_A_REPO,
            hint="run 'mgit init' to create one",
        )
