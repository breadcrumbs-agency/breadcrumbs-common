"""Atomic writes and file locks: what keeps a small file whole when two processes write it.

    write(path, data)   the file gets its new content in one step: a reader sees the old file or the new one, never half
                        of one and never an empty one. The content goes to a temporary file in the same folder first, and
                        that file has a name of its own (the process, the thread, a counter), so two writers of one file
                        never share it. A name that is fixed (path + ".tmp") is shared: the second writer empties the
                        file of the first, the first then moves an empty file into place, and the second fails because
                        its file is gone.
    locked(path)        one holder at a time of the lock `path`, across threads and processes (flock). It is for a
                        read that a write follows: read the head, then write the next head. Without it two writers read
                        the same head and one write is lost. `path` is a folder (the lock is on the folder itself, so no
                        file is made in it) or a lock file (made when it is missing).

The temporary file ends in ".tmp" and starts with a dot: a writer that dies leaves one behind, no reader takes it for
data, and `doctor` clears the old ones. Where there is no flock (Windows) locked() holds nothing: use one writer.
"""
import contextlib
import itertools
import os
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - no flock on this platform
    fcntl = None

_count = itertools.count()


def temp_name(path):
    """A name beside `path` that no other writer has: .<name>.<pid>.<thread>.<n>.tmp in the same folder (the same
    file system, so the rename is atomic)."""
    folder, name = os.path.split(path)
    return os.path.join(folder, ".%s.%d.%d.%d.tmp" % (name, os.getpid(), threading.get_ident(), next(_count)))


def write(path, data, mode=None, sync=False):
    """Give the file `path` the content `data` (bytes, or a text as UTF-8) in one step. mode: its permission bits (the
    default is the mode that a new file gets). sync: the bytes are on the disk before the rename (os.fsync), for a file
    that must outlive a power loss. A failure leaves the old file as it was and no temporary file."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = temp_name(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if sync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


_held = threading.local()  # the locks that this thread holds: {(device, inode): depth}


def _open(path):
    """An open file descriptor of the lock `path`: the folder itself, or the lock file (made when it is missing)."""
    if os.path.isdir(path):
        return os.open(path, os.O_RDONLY)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o666)


@contextlib.contextmanager
def locked(path):
    """Hold the lock `path` (a folder, or a lock file that is made when it is missing) until the block ends. Each call
    opens it again, so two threads of one process wait for each other as two processes do. A thread that holds the
    lock already goes on: a block inside a block of the same lock does not wait for itself. The lock ends with the
    process that holds it: a crash leaves no lock."""
    if fcntl is None:  # pragma: no cover - see the module doc
        yield
        return
    held = _held.__dict__.setdefault("locks", {})
    fd = _open(path)
    try:
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        outer = key not in held
        if outer:
            fcntl.flock(fd, fcntl.LOCK_EX)  # flock belongs to the open file: a second open of it by this thread would wait
        held[key] = held.get(key, 0) + 1
        try:
            yield
        finally:
            held[key] -= 1
            if not held[key]:
                del held[key]
            if outer:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
