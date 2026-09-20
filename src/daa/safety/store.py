"""Where safety writes to disk, and the permissions it writes with.

`~/.daa` holds the two files that know the most about the user: the audit log
(what they said, what we thought, what we did) and the undo journal (the
inverse of every mutation, which for `set_clipboard` means the text that was on
the clipboard BEFORE we replaced it -- a password copied ten seconds ago is the
tool's own motivating example).

Both were being created at the process umask, which on a stock macOS install
means a world-readable 0644 file inside a world-executable 0755 directory. That
is bad two ways round:

    READ  -- any process running on the box can read a day of someone's life
             out of the audit log.
    WRITE -- any process running AS THE USER can append a row to the undo
             journal, and `daa undo` will read that row and run the tool it
             names with the arguments it carries. The journal is untrusted
             input in exactly the way LLM output is untrusted input.

Tightening the mode does not make the journal trusted -- nothing a file mode
can do stops the user's own shell from appending to their own file, and the
validation in undo.py is what actually defends that path. What it does is stop
every OTHER account on the machine, and every process in another sandbox, from
reading or writing either file at all.

Modes are applied on creation (so there is no window where the file exists at
0644) AND to files that already exist (so an install that predates this module
is tightened the first time it is opened, rather than staying wrong forever).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO, Any

__all__ = ["DIR_MODE", "FILE_MODE", "harden_dir", "harden_file", "open_append", "secure_dir"]

# rwx------ / rw-------: the owner, and nobody else, ever.
DIR_MODE = 0o700
FILE_MODE = 0o600

# Directories we will create inside but must NEVER chmod: they are shared by
# construction, and tightening one would break every other user of the box.
# A caller pointing a journal at /tmp gets a 0600 file in a 0777 directory,
# which is still the right answer -- the file is the thing with the secrets.
_SHARED_DIRS = frozenset({"/", "/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"})


def secure_dir(path: Path) -> Path:
    """`mkdir -p` the directory, then make sure it is 0700. Never raises."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return path
    harden_dir(path)
    return path


def harden_dir(path: Path) -> None:
    """Drop group/other bits from an existing directory, if it is ours to drop."""
    _chmod_if_needed(path, DIR_MODE)


def harden_file(path: Path) -> None:
    """Drop group/other bits from an existing file. The fix for a file created
    before this module existed: it must be tightened, not left as it was."""
    _chmod_if_needed(path, FILE_MODE)


def open_append(path: Path, encoding: str = "utf-8") -> IO[Any]:
    """Open `path` for append, creating it 0600 if it is not there yet.

    Uses os.open with an explicit mode rather than Path.open so the file is
    never briefly visible at the umask default -- the creation and the
    permission are one syscall. An existing file is hardened first, since
    O_CREAT's mode argument is ignored when the file already exists.
    """
    harden_file(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    return os.fdopen(fd, "a", encoding=encoding)


def _chmod_if_needed(path: Path, mode: int) -> None:
    try:
        st = path.stat()
    except OSError:
        return  # not there yet; creation will apply the mode
    if str(path) in _SHARED_DIRS:
        return
    if os.name == "posix" and st.st_uid != os.getuid():
        return  # not ours; chmod would fail anyway, and trying is noise
    if stat.S_IMODE(st.st_mode) == mode:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        # A read-only mount or an exotic filesystem. The caller is a log or a
        # journal; neither may abort a mutation over a permission bit.
        return
