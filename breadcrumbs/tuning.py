"""The update interval of the live viewers, in milliseconds: the dial `update_ms`.

It is read from the environment variable BREADCRUMBS_UPDATE_MS, else from the top level of <root>/config.json (the
dials file of a store), else the default. A value that is not a whole number of milliseconds in its range is no value:
the default is used. The viewers (spacegraph's explorer page) poll at this interval; the measures poll at three times it.
hermes's dials.py lists the same dial (with the same default and range; a test keeps them equal).
"""
import json
import os

DEFAULT_MS = 2000
LOW_MS, HIGH_MS = 200, 60_000
ENV = "BREADCRUMBS_UPDATE_MS"


def _ok(v):
    return isinstance(v, int) and not isinstance(v, bool) and LOW_MS <= v <= HIGH_MS


def update_ms(root=None, environ=None):
    """The interval in milliseconds that the viewers poll at, for the store at root."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV)
    if raw not in (None, ""):
        try:
            v = int(raw)
            if _ok(v):
                return v
        except ValueError:
            pass
    if root:
        try:
            with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict) and _ok(cfg.get("update_ms")):
                return cfg["update_ms"]
        except (OSError, ValueError):
            pass
    return DEFAULT_MS
