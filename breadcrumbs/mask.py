"""Mask: well-known secret shapes in a text, before the text is hashed, stored or indexed.

mask(text) takes bytes or str and returns the same type. Each secret keeps its prefix (the kind of key) and its
length; every character of the rest becomes "*". The length stays, so the line count and the soft splits of
lines.split_lines are those of the unmasked text (a caller that counts lines before it stores is not wrong), and
mask(mask(x)) == mask(x). A private key block keeps its two marker lines and its line ends.

Hermes masks what tools return. The text a user types does not pass through it, so every write path of the store
runs this: lines.put_text, ingest.put_turn and Index.add.
"""

import re

# (prefix, body): a prefix of the kind, then a body of 16 or more token characters. The prefix stays.
_KEYS = (
    r"sk-ant-api\d\d-", r"sk-proj-", r"sk-", r"sk_live_", r"sk_test_", r"rk_live_", r"rk_test_",
    r"ghp_", r"gho_", r"ghu_", r"ghs_", r"ghr_", r"github_pat_",
    r"xox[abprs]-", r"hf_", r"AKIA", r"ASIA", r"AIza", r"glpat-", r"npm_",
)
_KEY = r"(?<![A-Za-z0-9_-])(%s)([A-Za-z0-9_-]{16,})" % "|".join(_KEYS)
_BLOCK = r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)(.*?)(?=-----END [A-Z ]*PRIVATE KEY-----)"

_STR = (re.compile(_KEY), re.compile(_BLOCK, re.S), "*", "\n", re.compile(r"[^\n]"))
_BIN = (re.compile(_KEY.encode()), re.compile(_BLOCK.encode(), re.S), b"*", b"\n", re.compile(rb"[^\n]"))


def mask(text):
    key, block, star, end, other = _BIN if isinstance(text, (bytes, bytearray, memoryview)) else _STR
    if isinstance(text, (bytearray, memoryview)):
        text = bytes(text)
    text = key.sub(lambda m: m.group(1) + star * len(m.group(2)), text)
    return block.sub(lambda m: m.group(1) + other.sub(star, m.group(2)), text)
