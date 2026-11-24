"""Line CAS: a text is a spine of line blobs.

Each line (with its own line ending) is one blob in the Store. The spine is
the sequence of line CRC32Cs: the line-spine fingerprint. The spine itself is
a blob, so the document id is the CRC32C of the spine, a Merkle hash over its
lines. Equal lines are stored once across all documents and sessions.

Spine blob: b"SP01" | n u32 | k u32 | n x crc u32 | k x (pos u32, ordinal u16)
(little-endian). The k entries name the rare lines whose blob id has an
ordinal (a CRC32C collision). The crc array can be read in place as a
memoryview cast to "I".

A text whose exact bytes a harness keeps (a row of Hermes's state.db) is not copied: put_text(store, data, loc) writes
its spine and the locator loc (Store.refer), and no line blob. The spine stays the fingerprint, so the document id, the
crumbs over it and their ranges are what they are with the lines. A text with a locator is read from the harness only,
through the port "source:<harness>" (ports.py; a locator is "<harness>:<place>"): source(store, loc) -> (the bytes as
the harness keeps them, the bytes as the harness masks them, its scrub: str -> str or None), or None. Either form is
checked against the spine by CRC32C; a row that the harness changed in place is shown while it has the spine's count of
lines. It always shows masked, one line for each line of the spine (masked_lines), so every range aligns; else the text
reads as SOURCE_GONE. relocate() turns the copies in a store into locators.
"""

import os
import struct
import sys

from .crc32c import crc32c
from .store import format_id, parse_id

_HEAD = struct.Struct("<4sII")
_ORD = struct.Struct("<IH")
_LITTLE = sys.byteorder == "little"


MAX_LINE = 256  # longer lines are split after a sentence end, a space, or a UTF-8 boundary
_ENDS = (b". ", b"! ", b"? ", b"; ")


def _soft_split(line, out):
    while len(line) > MAX_LINE:
        window = line[:MAX_LINE]
        cut = max(window.rfind(e) for e in _ENDS) + 2
        if cut < MAX_LINE // 4:
            cut = window.rfind(b" ") + 1
        if cut < MAX_LINE // 4:
            cut = MAX_LINE
            while cut > 1 and (line[cut] & 0xC0) == 0x80:  # never cut inside a character
                cut -= 1
        out.append(line[:cut])
        line = line[cut:]
    if line:
        out.append(line)


def split_lines(data):
    """Split at b"\\n" (endings kept); split lines over MAX_LINE bytes further.

    b"".join(split_lines(data)) == data always holds.
    """
    data = bytes(data)
    out, start = [], 0
    while True:
        end = data.find(b"\n", start)
        if end < 0:
            break
        _soft_split(data[start:end + 1], out)
        start = end + 1
    if start < len(data):
        _soft_split(data[start:], out)
    return out


def encode_spine(ids):
    crcs, ords = [], []
    for pos, blob_id in enumerate(ids):
        crc, ordinal = parse_id(blob_id)
        crcs.append(crc)
        if ordinal:
            ords.append((pos, ordinal))
    body = struct.pack("<%dI" % len(crcs), *crcs)
    tail = b"".join(_ORD.pack(p, o) for p, o in ords)
    return _HEAD.pack(b"SP01", len(crcs), len(ords)) + body + tail


def decode_spine(blob):
    magic, n, k = _HEAD.unpack_from(blob, 0)
    if magic != b"SP01":
        raise ValueError("not a spine blob")
    start = _HEAD.size
    if _LITTLE:
        crcs = memoryview(blob)[start:start + 4 * n].cast("I")
    else:  # pragma: no cover - big-endian hosts
        crcs = struct.unpack_from("<%dI" % n, blob, start)
    ords = {}
    off = start + 4 * n
    for i in range(k):
        pos, ordinal = _ORD.unpack_from(blob, off + i * _ORD.size)
        ords[pos] = ordinal
    return [(crcs[i], ords.get(i, 0)) for i in range(n)]


SOURCE_GONE = b"(source gone: the harness no longer holds this text)\n"


def _line_id(store, line):
    """The id of a line blob as put() gives it, without a write."""
    found = store.find(line)
    return found if found is not None else format_id(crc32c(line), 0)


def put_text(store, data, loc=None):
    """Store a text; return its document id (the spine blob id). loc: where the harness keeps these exact bytes; the
    store then keeps the spine and the locator, no line."""
    lines = split_lines(data)
    if loc and lines:
        doc = store.put(encode_spine([_line_id(store, line) for line in lines]))
        store.refer(doc, loc)
        return doc
    return store.put(encode_spine([store.put(line) for line in lines]))


def doc_of(store, data):
    """The document id of these bytes when the store holds their spine, else None. It writes nothing."""
    return store.find(encode_spine([_line_id(store, line) for line in split_lines(bytes(data))]))


def doc_lines(store, doc_id, blob=None):
    """All the lines of a text: from the harness when the text has a locator, else its line blobs."""
    spine = decode_spine(store.get(doc_id) if blob is None else blob)
    loc = store.locator(doc_id) if hasattr(store, "locator") else None  # a hive's received blobs have no locators
    if loc is None:
        return [store.get(format_id(c, o)) for c, o in spine]
    from . import ports
    source = ports.get("source:" + loc.partition(":")[0])
    got = source(store, loc) if source is not None else None
    if got is not None:
        raw, masked, scrub = got
        lines, shown = split_lines(raw), split_lines(masked)
        crcs = [c for c, _o in spine]
        if [crc32c(line) for line in shown] == crcs:  # the row as it was read, masked
            return shown
        if [crc32c(line) for line in lines] == crcs or len(lines) == len(spine):  # as it was read, or changed in place
            return shown if len(shown) == len(spine) else masked_lines(lines, scrub)
    return [SOURCE_GONE] + [b"\n"] * (len(spine) - 1) if spine else []


def masked_lines(lines, scrub):
    """The lines of a text as the harness's scrub shows it, one for each line, so every range stays aligned. A scrub
    that joins lines (a mask over a line end) shows each line alone; a line of such a joined span that its own
    scrub leaves as it was reads "***"."""
    if scrub is None:
        return lines
    whole = split_lines(scrub(b"".join(lines).decode("utf-8", "replace")).encode("utf-8"))
    if len(whole) == len(lines):
        return whole
    import difflib
    each = [scrub(line.decode("utf-8", "replace")).encode("utf-8") for line in lines]
    out = list(each)
    for op, a, b, _c, _d in difflib.SequenceMatcher(None, lines, whole, autojunk=False).get_opcodes():
        if op != "equal":
            for i in range(a, b):
                if each[i] == lines[i]:
                    out[i] = b"***" + lines[i][len(lines[i].rstrip(b"\r\n")):]
    return out


def spine(store, doc_id):
    """The line-spine fingerprint: [(crc, ordinal), ...]."""
    return decode_spine(store.get(doc_id))


def line_ids(store, doc_id):
    return [format_id(c, o) for c, o in spine(store, doc_id)]


def get_lines(store, doc_id, start=0, stop=None):
    return doc_lines(store, doc_id)[start:stop]


def get_text(store, doc_id):
    return b"".join(get_lines(store, doc_id))


def relocate(path, texts):
    """Turn the copies in the store at path into locators, once: texts yields (loc, candidates), the bytes that a
    store may hold for the text at loc (a harness's own text and its masked form), each a callable or bytes. A
    document with those bytes gets the locator, and the line blobs that only such documents use are dropped: the store
    is written again without them (every other blob keeps its id), under its lock. Returns the blobs dropped."""
    import shutil
    from .store import Store
    store = Store(path)
    try:
        located = store.located()
        for loc, cands in texts:
            for data in cands:
                data = data() if callable(data) else data
                doc = doc_of(store, data) if data else None
                if doc is not None:
                    if doc not in located:
                        store.refer(doc, loc)
                        located[doc] = loc
                    break
        store.flush()
        keep, only = set(), set()
        for bid in store.ids():
            blob = store.get(bid)
            if blob[:4] == b"SP01":
                (only if bid in located else keep).update(format_id(c, o) for c, o in decode_spine(blob))
        drop = only - keep - set(located)
        for bid in list(drop):  # a kept blob with ordinal k keeps its id only when ordinals 0..k-1 are there too
            crc, o = parse_id(bid)
            while format_id(crc, o + 1) in store:
                o += 1
                if format_id(crc, o) not in drop:
                    drop.discard(bid)
                    break
        if not drop:
            return 0
        new = path + ".new"
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        with store._locked():
            out = Store(new)
            try:
                for bid in store.ids():
                    if bid not in drop:
                        got = out.put(store.get(bid))
                        assert got == bid, (bid, got)
                for doc, loc in located.items():
                    out.refer(doc, loc)
                out.flush()
            finally:
                out.close()
            old = path + ".old"
            os.rename(path, old)
            os.rename(new, path)
        shutil.rmtree(old, ignore_errors=True)
        return len(drop)
    finally:
        store.close()
