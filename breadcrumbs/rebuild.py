"""Rebuild: a memory written again, with every text masked (mask.py), into a new home.

An id is the CRC32C of a blob, and a crumb holds the ids of its texts, kids and heritage, so a text that changes
gives a new id to every crumb above it. A memory that holds a key therefore cannot be mended in place, and a tag
that supersedes a crumb leaves its bytes in the pack. rebuild(src, dst) reads src and writes dst, which holds:

  - each crumb of src, in dependency order, with its texts masked and every id that it names (kids, heritage, ranges and
    the ids in its title and meta: a receipt's items, ctx, prev, a commit's message) changed to its new id;
  - each text with the line structure of the old one (mask keeps the length, so the lines are cut at the old
    line ends and a range a-b still names the same lines);
  - the refs, with their new ids;
  - the index, made again from dst: the rows and their FTS text, the neighbor links, the heat, the summaries
    (masked) and the tags.

A crumb with no key keeps its id, with one exception: a blob id that carries a clash ordinal ("ab12cd34.1", two
blobs of one CRC32C) takes its ordinal from the order of the puts, and the rebuild puts in another order than the
first writer did. Such an id, and the ids above it, can change in a memory that holds no key. Blobs that no crumb names (lines of a
text that was replaced) are not copied: the new pack is also smaller. src is opened to read and never changed; dst must
be empty or absent. Swap the folders after the rebuild, with the provider stopped. Files that are not the store
(trail.json, usage.log, provider.log, ace.log) hold ids and numbers, not text, and are not copied.
"""

import os
import re
import sqlite3

from .crumbs import Crumb, get_crumb, is_crumb, put_crumb
from .history import Refs
from .index import Index
from .lines import encode_spine, line_ids
from .mask import mask
from .store import Store

# An id as a title, a meta text or an index key holds it: eight hex digits (and .N after a CRC clash), then
# optionally :a-b (lines of a text). The same shape as the ref of hermes.REF.
_ID = re.compile(r"(?<![0-9A-Za-z_.])([0-9a-f]{8}(?:\.\d+)?)((?::\d{1,9}-\d{1,9})?)(?![0-9A-Za-z_])")


def _strings(x):
    """Every string in a meta value (a dict, a list or a text)."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _walk(x, f):
    if isinstance(x, str):
        return f(x)
    if isinstance(x, dict):
        return {k: _walk(v, f) for k, v in x.items()}
    if isinstance(x, list):
        return [_walk(v, f) for v in x]
    return x


class _Rebuild:
    def __init__(self, src, dst):
        self.src, self.dst = src, dst
        self.crumbs, self.spines = set(), set()
        for blob_id in src.ids():
            blob = src.get(blob_id)
            if is_crumb(blob):
                self.crumbs.add(blob_id)
            elif bytes(blob[:4]) == b"SP01":
                self.spines.add(blob_id)
        self.doc, self.crumb = {}, {}
        self.changed_docs = self.changed_crumbs = 0

    # -- texts ---------------------------------------------------------------------------------------------
    def new_doc(self, doc):
        got = self.doc.get(doc)
        if got is None:
            old = [self.src.get(i) for i in line_ids(self.src, doc)]
            masked, pos, lines = mask(b"".join(old)), 0, []
            for line in old:
                lines.append(masked[pos:pos + len(line)])
                pos += len(line)
            got = self.doc[doc] = self.dst.put(encode_spine([self.dst.put(x) for x in lines]))
            self.changed_docs += got != doc
        return got

    def fix(self, text):
        """text with each id that it names (a crumb or a text of src) changed to the id in dst."""
        def one(m):
            i = m.group(1)
            if i in self.crumb:
                return self.crumb[i] + m.group(2)
            if i in self.spines:
                return self.new_doc(i) + m.group(2)
            return m.group(0)
        return _ID.sub(one, text)

    # -- crumbs --------------------------------------------------------------------------------------------
    def needs(self, c, own):
        """The crumbs of src that c names and that are not yet in dst."""
        names = set(c.kids) | set(c.heritage)
        for s in list(_strings(c.meta)) + [c.title]:
            names.update(m.group(1) for m in _ID.finditer(s))
        return [i for i in names if i in self.crumbs and i not in self.crumb and i != own]

    def new_crumb(self, cid):
        stack, path = [cid], set()
        while stack:
            top = stack[-1]
            if top in self.crumb:
                stack.pop()
                continue
            c = get_crumb(self.src, top)
            wait = [i for i in self.needs(c, top) if i not in path]
            if wait:
                path.add(top)
                stack.extend(wait)
                continue
            stack.pop()
            path.discard(top)
            made = Crumb(c.kind, mask(self.fix(c.title)), [[self.new_doc(d) if d in self.spines else d, a, b] for d, a, b in c.ranges],
                         [self.crumb.get(k, k) for k in c.kids], [self.crumb.get(h, h) for h in c.heritage],
                         c.public, _walk(c.meta, lambda s: mask(self.fix(s))))
            self.crumb[top] = put_crumb(self.dst, made)
            self.changed_crumbs += self.crumb[top] != top
        return self.crumb[cid]

    # -- the index -----------------------------------------------------------------------------------------
    def text_of(self, ref):
        doc, _, span = ref.partition(":")
        a, _, b = span.partition("-")
        ids = line_ids(self.dst, doc)[int(a or 0):int(b) if b else None]
        return b"".join(self.dst.get(i) for i in ids).decode("utf-8", "replace")

    def index(self, old, new):
        rows = 0
        for crumb, ref, kind, public in old.execute("SELECT crumb, ref, kind, public FROM rows ORDER BY id"):
            if crumb not in self.crumb:
                continue
            new_ref = self.fix(ref)
            rows += new.add(self.crumb[crumb], new_ref, self.text_of(new_ref), kind=kind, public=bool(public))
        for table, sql, make in (
                ("nbr", "SELECT a, b FROM nbr", lambda a, b: ("INSERT OR IGNORE INTO nbr VALUES (?, ?)", (self.fix(a), self.fix(b)))),
                ("heat", "SELECT id, accessed, uses FROM heat",
                 lambda i, t, n: ("INSERT OR REPLACE INTO heat VALUES (?, ?, ?)", (self.fix(i), t, n))),
                ("sums", "SELECT src, sum FROM sums",
                 lambda s, text: ("INSERT OR REPLACE INTO sums VALUES (?, ?)", (self.fix(s), mask(self.fix(text))))),
                ("tags", "SELECT key, old, new, n FROM tags",
                 lambda k, o, w, n: ("INSERT OR REPLACE INTO tags VALUES (?, ?, ?, ?)",
                                     (self.fix(k), self.fix(o), self.fix(w), n)))):
            try:
                found = old.execute(sql).fetchall()
            except sqlite3.OperationalError:  # an index from before this table
                continue
            for row in found:
                new.db.execute(*make(*row))
        new.commit()
        return rows


def rebuild(src_home, dst_home):
    """Write src_home's memory again into dst_home, masked. Returns the counts."""
    if os.path.exists(dst_home) and os.listdir(dst_home):
        raise FileExistsError("%s is not empty" % dst_home)
    os.makedirs(dst_home, exist_ok=True)
    src = Store(os.path.join(src_home, "store"), readonly=True)
    old_refs = Refs(os.path.join(src_home, "refs"), readonly=True)
    old_db = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(os.path.join(src_home, "index.db")), uri=True)
    dst = Store(os.path.join(dst_home, "store"))
    index = Index(os.path.join(dst_home, "index.db"))
    try:
        job = _Rebuild(src, dst)
        for cid in sorted(job.crumbs):
            job.new_crumb(cid)
        refs = Refs(os.path.join(dst_home, "refs"))
        for name in old_refs.names():
            head = old_refs.get(name)
            refs.set(name, job.crumb.get(head, head))
        dst.flush()
        rows = job.index(old_db, index)
        return {"crumbs": len(job.crumbs), "changed_crumbs": job.changed_crumbs, "texts": len(job.spines),
                "changed_texts": job.changed_docs, "blobs_before": len(src), "blobs_after": len(dst),
                "refs": len(old_refs.names()), "index_rows": rows}
    finally:
        index.close()
        dst.close()
        old_db.close()
        src.close()
