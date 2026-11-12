"""sync: the sessions of a harness program, each in the memory of the place where it ran.

`ingest HARNESS` reads every session into one store. `sync HARNESS` reads the same sessions and puts each one where its
folder (the cwd of the session) belongs, by scope.py: a folder with a `.breadcrumbs` entry, else a git repository, else a
source tree owns its memory, <base>/scopes/<id>. A session that ran in no scope (no folder, or the home folder, /tmp)
goes to the default memory, <base> itself. So one project never holds the sessions of another, and nothing is written
into the project. BREADCRUMBS_SCOPE=off puts every session into the default memory.

sync is safe to run again and again (a hook runs it at the start and the end of each session):
- a session that is in its store unchanged adds nothing, and a store with nothing new gets no commit;
- the usage of a request is added once (usage.append);
- each store gets its state.db (statedb.py) for the viewers, and each scope its scope.json (scope.ensure).

`--here` (or BREADCRUMBS_HERE=1) of the command line is the other half: the memory of the scope of the current folder,
where() below. The servers read it, and so do the viewers, so a harness in a project looks at that project only.
"""
import json
import os
import time

from . import ports, scope, statedb, usage
from .history import Refs, _ref_name, commit
from .index import Index
from .ingest import session as put_session
from .runtime import no_gc
from .store import Store

OFF = ("off", "0", "false", "no")
YES = ("1", "on", "true", "yes", "here")


def enabled(env=None):
    """False when BREADCRUMBS_SCOPE says off: every session then goes to the default memory."""
    return str((env if env is not None else os.environ).get("BREADCRUMBS_SCOPE", "auto")).strip().lower() not in OFF


def here(env=None):
    """True when BREADCRUMBS_HERE asks for the memory of the current folder (the command line flag --here does too)."""
    return str((env if env is not None else os.environ).get("BREADCRUMBS_HERE", "")).strip().lower() in YES


def route(base, cwd, on=True, home=None):
    """(kind, root, store folder) of a session that ran in `cwd`: its scope and the memory of that scope, or ("", "", base)
    when it has none. `home` is the home folder that bounds the repositories (default: the user's)."""
    if on and cwd and os.path.isabs(os.path.expanduser(cwd)):
        found = scope.nearest(cwd, home)
        if found:
            return found[0], found[1], scope.store_home(base, found[1])
    return "", "", base


def where(base, cwd=None, on=None, home=None):
    """The memory folder of the scope of the current folder (or `cwd`) under `base`: what --here opens."""
    return route(base, cwd or os.getcwd(), enabled() if on is None else on, home)[2]


def _put(home, sessions, message):
    """The sessions into the store at `home`: (sessions, new sessions, messages, bytes). One commit on main, and only when
    a session is new or has changed; the blobs and index rows of what is there already dedupe."""
    os.makedirs(home, exist_ok=True)
    store, index, refs = Store(os.path.join(home, "store")), Index(os.path.join(home, "index.db")), Refs(os.path.join(home, "refs"))
    stats, roots = {"sessions": 0, "messages": 0, "bytes": 0}, []
    try:
        with no_gc():
            for sid, title, meta, rows in sessions:
                before = refs.get(_ref_name("sess-", sid))
                crumb = put_session(store, index, refs, sid, title, meta, rows, stats)
                if crumb and crumb != before:
                    roots.append(crumb)
        if roots:
            commit(store, refs, "main", roots, message)
        store.flush()
        index.commit()
    finally:
        index.close()
        store.close()
    return stats["sessions"], len(roots), stats["messages"], stats["bytes"]


def sync(harness, path, base, on=None, home=None):
    """Read the sessions of `harness` (from `path`, default: where the program keeps them) into the memories under `base`.
    Returns the stats: {harness, sessions, new, messages, bytes, seconds, stores: [{store, scope, kind, sessions, new}],
    usage: {requests, added}}. ValueError: the harness is unknown or gives no sessions."""
    t0 = time.perf_counter()
    on = enabled() if on is None else on
    entry = ports.harnesses().get(harness)
    if entry is None or entry["source"] is None:
        raise ValueError("harness is one of %s" % ", ".join(k for k, v in ports.harnesses().items() if v["source"]))
    place = {}  # store folder -> {"kind", "root", "sessions"}
    for sess in entry["source"](path):
        kind, root, folder = route(base, (sess[2] or {}).get("cwd"), on, home)
        place.setdefault(folder, {"kind": kind, "root": root, "sessions": []})["sessions"].append(sess)
    where_is = {sess[0]: folder for folder, one in place.items() for sess in one["sessions"]}
    records = {}
    for r in (entry["usage"](path) if entry.get("usage") else ()):
        records.setdefault(where_is.get(r.get("sess"), base), []).append(r)
    out = {"harness": harness, "sessions": 0, "new": 0, "messages": 0, "bytes": 0, "stores": [],
           "usage": {"requests": 0, "added": 0}}
    for folder, one in sorted(place.items()):
        if one["root"]:
            scope.ensure(base, one["kind"], one["root"])
        n, new, messages, size = _put(folder, one["sessions"], "sync " + harness)
        got = records.get(folder, [])
        out["usage"]["requests"] += len(got)
        out["usage"]["added"] += usage.append(folder, got)
        statedb.write(folder, harness, one["sessions"], got, one["root"] or None)
        out["stores"].append({"store": folder, "scope": scope.short(one["root"]) if one["root"] else "", "kind": one["kind"],
                              "sessions": n, "new": new})
        out["sessions"] += n
        out["new"] += new
        out["messages"] += messages
        out["bytes"] += size
    out["seconds"] = round(time.perf_counter() - t0, 3)
    return out


def command(args, home):
    """`sync HARNESS [PATH]`: `home` is the base (the default memory, and <home>/scopes holds the others). Prints the stats."""
    try:
        stats = sync(args.harness, args.path, home)
    except ValueError as e:
        print("breadcrumbs: %s" % e, file=__import__("sys").stderr)
        return 2
    print(json.dumps(stats))
    return 0
