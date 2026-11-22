"""Git-like history over the store: commits are crumbs, refs are small files.

A commit crumb has kind "commit", kids = the root crumb ids it records,
heritage = [parent commit id], meta = {"ts": seconds, "msg": text}. Its id
covers all of these, so the commit chain is a Merkle chain: the causal spine.
Refs are the only mutable state; each ref file holds one commit id.

Two rules keep a ref whole when two processes write (the gateway of a harness and a command line):
  - A ref never names a crumb that is not on disk. commit() flushes the store before it sets the ref, and a bulk
    ingest sets the refs of its sessions after its flush (Later). A writer that dies between the two leaves the old
    head, never a head that nothing can read.
  - A head is read and then written by one writer at a time. commit() holds the lock of the refs (Refs.locked) from
    its read of the old head to its write of the new one. Without it two writers read the same head and each wrote a
    commit over it: one of the two commits left the chain.
A ref file gets its new value in one step (atomic.write): a reader sees the old id or the new one.
"""

import os
import re
import time

from . import atomic
from .crumbs import Crumb, get_crumb, put_crumb

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _ref_name(prefix, session_id):
    """The name of the ref of a session: prefix ("sess-", "rcpt-") and the id, in the letters that a ref may have."""
    return prefix + re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "none")[:100]


class Refs:
    def __init__(self, path, readonly=False):
        if readonly:
            if not os.path.isdir(path):
                raise FileNotFoundError(path)
        else:
            os.makedirs(path, exist_ok=True)
        self.path, self.readonly = path, readonly

    def _file(self, name):
        if not _NAME.match(name):
            raise ValueError("bad ref name: %r" % name)
        return os.path.join(self.path, name)

    def get(self, name):
        try:
            with open(self._file(name), encoding="ascii") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def set(self, name, crumb_id):
        if self.readonly:
            raise PermissionError("read-only refs: %s" % self.path)
        atomic.write(self._file(name), (crumb_id + "\n").encode("ascii"))

    def names(self):
        return sorted(n for n in os.listdir(self.path) if _NAME.match(n) and not n.endswith(".tmp"))

    def locked(self):
        """One writer at a time for a head that is read and then written (see the module doc): hold it from the
        get() to the set(). The lock is on the folder of the refs itself, so it makes no file."""
        return atomic.locked(self.path)


class Later:
    """Ref writes that wait for the store: set() keeps the name and the id, flush() writes them. A bulk ingest gives
    this in place of its Refs, flushes the store, and then calls flush(): no ref names a crumb that a crash can lose.
    get() gives the value that is waiting, else the value of the ref."""

    def __init__(self, refs):
        self.refs, self.waiting = refs, {}

    def get(self, name):
        return self.waiting.get(name) or self.refs.get(name)

    def set(self, name, crumb_id):
        self.refs._file(name)  # a bad name fails now, as Refs.set does
        self.waiting[name] = crumb_id

    def names(self):
        return sorted(set(self.refs.names()) | set(self.waiting))

    def locked(self):
        return self.refs.locked()

    def flush(self):
        """Write the refs that wait. Call it after store.flush()."""
        waiting, self.waiting = self.waiting, {}
        for name, crumb_id in waiting.items():
            self.refs.set(name, crumb_id)


def commit(store, refs, name, roots, msg="", ts=None):
    """Record roots on ref `name`; the old ref value becomes the parent. The commit and what the store holds for it
    are on disk before the ref names it, and no other writer of these refs comes between the read of the old head and
    the write of the new one (see the module doc)."""
    with refs.locked():
        parent = refs.get(name)
        crumb = Crumb("commit", msg[:60], kids=list(roots), heritage=[parent] if parent else [],
                      meta={"ts": int(time.time() if ts is None else ts), "msg": msg})
        cid = put_crumb(store, crumb)
        store.flush()
        refs.set(name, cid)
    return cid


def log(store, cid):
    """Walk first parents from cid: (commit id, Crumb), newest first."""
    while cid:
        crumb = get_crumb(store, cid)
        yield cid, crumb
        cid = crumb.heritage[0] if crumb.heritage else None
