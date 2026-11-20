"""The model dictionary: one entry for each model of each vendor, with create, read, update and delete, cloning, and
links between vendors. It is the "catalog" of config.json (the same file as the dials, so a scope's file still counts
over its profile's):

  {"catalog": {
     "entries": {"anthropic/claude-haiku-5-5": {"settings": {"ace_tokens": 94000, "temperature": 0.7},
                                                "note": "cheap reader", "cloned_from": null},
                 "openrouter/anthropic/claude-haiku-5.5": {"settings": {}}},
     "links":    [["anthropic/claude-haiku-5-5", "bedrock/claude-haiku-5-5-v1"]],
     "unlinked": [["anthropic/claude-haiku-5-5", "openrouter/anthropic/claude-haiku-5.5"]]}}

An id is VENDOR/MODEL: the vendor is the provider that the harness calls (anthropic, openrouter, nous, bedrock, ...)
and the model is the name on that provider's wire, which may hold a slash itself. The vendor `*` is any vendor. A
model may be a glob (fnmatch, case-insensitive), as the keys of "models" always were. "settings" holds what a "models"
entry holds: the dials (dials.py) and the request options of the model (temperature, top_p, ...).

Three ways to relate entries:

  clone   a copy. `clone anthropic/claude-haiku-5-5 --to nous` writes nous/claude-haiku-5-5 with the same settings;
          the two then change alone. The copy remembers its source ("cloned_from").
  link    a live bond that you make. Entries that are linked share settings: a model that has no value of its own for
          a setting takes the value of a linked entry (the links are one group, in both directions, and through a
          third entry).
  family  a live bond that is made for you (the automagic). Two entries are of one family when their model names
          read the same after the names are cleaned: the vendor prefix of a slug, a bedrock region and maker
          prefix, a date or "-latest", ":free", and "5.5" read as "5-5", the words in any order. So
          openrouter/anthropic/claude-haiku-5.5 and anthropic/claude-haiku-5-5 are of one family without a word
          from you. `unlink` of a pair that is only a family stops the bond for that pair (it is kept in "unlinked").

A request (a provider and a model) reads these layers, the lowest first: the entries of its family, then the entries
linked to those or to its own, then its own (the vendor is the provider, or `*`; the exact name over a glob). Where
two entries of one layer set the same key, the newer implementation wins: each entry has a date, its "since" (the
date you give it: when the model or the setting was put in use) or else its "updated" (stamped when its settings
last changed), and a layer reads oldest to newest, so the newest member of a family or a link group is its master
(view() names it). Undated entries are the oldest; a tie is settled by id. A
dial resolves through them (dials.resolve) after the built-in entries and before the "models" globs of the file;
dials.options adds them under the "models" globs. The source of a value names the entry and the bond.

Built-in entries (builtin()) come with breadcrumbs and show in every list, flagged "builtin". Claude Haiku 5.5 is the
example of a cost model: its price per token steps up with the size of the prompt, so its entry carries the ceiling
that keeps a request under the step (ace_ceiling), and each entry that a cost profile (costs.py) prices shows that
profile beside its settings ("cost": the tiers, the context window and the sources). Changing a built-in entry
copies it into the file first (the copy is a file entry of the same id and wins, as any file entry does); removing
the copy gives the built-in one back. Resolution reads the built-in ones as it always did (dials.MODELS).

Mutations take the folder of the file (like dials.set_setting) and return the entry as the widget shows it.
"""
import copy
import fnmatch
import json
import os
import re
import time

KEY = "catalog"
GLOB = re.compile(r"[*?\[]")
# Tags that name a price or a route of the same model; any other tag (ollama's size, for one) stays in the name.
TAGS = {"free", "latest", "beta", "extended", "floor", "nitro", "online", "exacto", "preview-free"}
_BEDROCK = re.compile(r"^(?:(?:us|eu|apac|global|us-gov)\.)?(?:anthropic|meta|amazon|mistral|cohere|ai21|deepseek|"
                      r"openai|qwen|google|writer|stability|twelvelabs)\.")
_VERSION = re.compile(r"-v\d+(?::\d+)?$")
_DATE = re.compile(r"[-@](?:\d{8}|\d{4}-\d{2}-\d{2})$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?$")
SEEN_LINE = re.compile(r"\bmodel=(\S+)(?:.*?\bprov=(\S+))?")


def split_id(ident):
    """(vendor, model) of an id; ValueError when there is no vendor (`*` stands for any)."""
    ident = str(ident or "").strip()
    vendor, sep, model = ident.partition("/")
    if not sep or not vendor or not model:
        raise ValueError("a model id is VENDOR/MODEL (vendor * for any vendor): %r" % ident)
    return vendor.lower(), model


def make_id(vendor, model):
    return "%s/%s" % (str(vendor).strip().lower(), str(model).strip())


def canon(model):
    """The name of a model with the noise of a route taken off: lower case, no slug prefix, no bedrock region or
    maker, no -vN:N, no date or latest, no price tag, dots as dashes ("5.5" is "5-5")."""
    name = str(model or "").strip().lower()
    name = name.rsplit("/", 1)[-1]
    name = _BEDROCK.sub("", name)
    base, sep, tag = name.rpartition(":")
    if sep and tag in TAGS:
        name = base
    name = _VERSION.sub("", name)
    name = _DATE.sub("", name)
    if name.endswith("-latest"):
        name = name[:-len("-latest")]
    name = re.sub(r"(?<=\d)\.(?=\d)", "-", name)
    name = name.replace(":", "-").replace("_", "-").replace(" ", "-")
    return re.sub(r"-+", "-", name).strip("-")


def tokens(model):
    """The words of canon(model), as a sorted tuple: the order of the words does not count (claude-3-5-sonnet is
    claude-sonnet-3-5)."""
    return tuple(sorted(t for t in canon(model).split("-") if t))


def family_key(model):
    """The key that two models share when they are one family; None for a glob or an empty name."""
    if not model or GLOB.search(str(model)):
        return None
    return tokens(model) or None


def _near(a, b):
    """The share of words that two names have in common (0 to 1)."""
    ta, tb = set(tokens(a)), set(tokens(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def builtin():
    """{id: entry} of the entries that come with breadcrumbs: one for each built-in model entry of dials (MODELS),
    named by the glob of the model under the vendor `*`. Each carries a note that says how its value follows from
    its cost profile."""
    from . import costs, dials

    out = {}
    for glob, opts in dials.MODELS.items():
        prof = costs.BUILTIN.get(glob) or {}
        step = dials.PRICE_STEPS.get(glob)
        rates = list(prof.get("tiers", ()))
        note = "built in; a cost model."
        if step and len(rates) > 1:
            note += (" The price steps up at %s prompt tokens (input %g, then %g USD per 1M), so ace_ceiling is the "
                     "step less the %g%% that the token estimate reads low, down to the thousand: %s. A request "
                     "then stays under the step. Clone it for another model with a price step."
                     % (format(step, ","), rates[0]["input"], rates[1]["input"], dials.UNDER_READ * 100,
                        format(opts.get("ace_ceiling", 0), ",")))
        out[make_id("*", glob)] = {"settings": copy.deepcopy(opts), "note": note, "builtin": True}
    return out


def with_builtin(cfg):
    """cfg with the built-in entries added to the catalog where the file has no entry of that id."""
    entries, links, unlinked = section(cfg)
    extra = {i: e for i, e in builtin().items() if i not in entries}
    if not extra:
        return cfg
    out = dict(cfg)
    raw = dict(cfg.get(KEY)) if isinstance(cfg.get(KEY), dict) else {}
    raw["entries"] = {**entries, **extra}
    out[KEY] = raw
    return out


def cost_of(model):
    """The cost profile that prices a model (costs.lookup), as plain data, or None: {name, context_window, tiers, sources}."""
    from . import costs

    if not model:
        return None
    name, prof = costs.lookup(model) if not GLOB.search(model) else (model, costs.profiles()[0].get(model))
    if not prof:
        return None
    return {"name": name, "context_window": prof.get("context_window"),
            "tiers": [dict(rates, upto=upto) for upto, rates in prof["tiers"]],
            "sources": [dict(x) for x in prof.get("sources", ())]}


def section(cfg):
    """The catalog of one file (a dict), tolerant of what is missing or wrong: (entries, links, unlinked)."""
    raw = cfg.get(KEY) if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
    entries = {k: v for k, v in entries.items() if isinstance(v, dict) and _ok_id(k)}

    def pairs(name):
        got = raw.get(name)
        return [tuple(p) for p in got if isinstance(p, (list, tuple)) and len(p) == 2
                and all(isinstance(x, str) for x in p)] if isinstance(got, list) else []

    return entries, pairs("links"), pairs("unlinked")


def _ok_id(ident):
    try:
        split_id(ident)
        return True
    except ValueError:
        return False


def now():
    """The stamp of a change: local time, as the logs of breadcrumbs write it."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def stamp(entry):
    """The implementation date of an entry, as text that sorts by time: its "since", else its "updated", else ""."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("since") or entry.get("updated") or "")


def _by_date(entries, ids):
    """ids oldest first: by implementation date, then by id."""
    return sorted(ids, key=lambda i: (stamp(entries.get(i)), i))


def master(cfg, ident):
    """The id of the newest implementation among the entry, its links and its family (itself when it is alone)."""
    entries, links, unlinked = section(cfg)
    if ident not in entries:
        raise KeyError("no entry %s" % ident)
    veto = _vetoed(unlinked)
    group = set(_groups(links).get(ident, ())) | {ident}
    group |= {i for i in family_of(cfg, split_id(ident)[1], (ident,)) if frozenset((i, ident)) not in veto
              and not GLOB.search(split_id(i)[1])} if not GLOB.search(split_id(ident)[1]) else set()
    return _by_date(entries, [i for i in group if i in entries])[-1]


def _settings(entry):
    s = entry.get("settings") if isinstance(entry, dict) else None
    return s if isinstance(s, dict) else {}


def _vendor_ok(vendor, provider):
    return vendor == "*" or not provider or vendor == str(provider).strip().lower()


def own(cfg, provider, model):
    """The ids of the entries that are the model's own on this provider: the vendor fits, the name matches. Globs
    come first and an exact name after them, so the exact one is the last to set a value."""
    entries, _, _ = section(cfg)
    name = str(model or "").lower()
    globs, exact = [], []
    for ident in entries:
        vendor, pattern = split_id(ident)
        if not _vendor_ok(vendor, provider):
            continue
        if GLOB.search(pattern):
            if fnmatch.fnmatchcase(name, pattern.lower()):
                globs.append(ident)
        elif pattern.lower() == name:
            exact.append(ident)
    return globs + exact


def _groups(links):
    """The groups of ids that the links tie together, as {id: frozenset(group)} (a link holds through a third)."""
    group = {}
    for a, b in links:
        merged = group.get(a, frozenset((a,))) | group.get(b, frozenset((b,)))
        for member in merged:
            group[member] = merged
    return group


def _vetoed(unlinked):
    return {frozenset(p) for p in unlinked}


def family_of(cfg, model, exclude=()):
    """The ids of the entries that are of the family of model (not in exclude): the same words, or a glob that
    matches the name whatever its vendor."""
    entries, _, _ = section(cfg)
    key, name, out = family_key(model), str(model or "").lower(), []
    for ident in sorted(entries):
        if ident in exclude:
            continue
        pattern = split_id(ident)[1]
        if GLOB.search(pattern):
            hit = fnmatch.fnmatchcase(name, pattern.lower())
        else:
            hit = key is not None and family_key(pattern) == key
        if hit:
            out.append(ident)
    return out


def bonds(cfg, provider, model):
    """[(ident, how)], the lowest layer first: how is "family", "linked" or "own"."""
    entries, links, unlinked = section(cfg)
    mine = own(cfg, provider, model)
    veto = _vetoed(unlinked)
    fam = [i for i in family_of(cfg, model, mine) if not any(frozenset((i, m)) in veto for m in mine)]
    group = _groups(links)
    seeds = set(mine) | set(fam)
    mates = _by_date(entries, {m for s in seeds for m in group.get(s, ()) if m in entries and m not in seeds})
    fam = _by_date(entries, fam)
    return [(i, "family") for i in fam] + [(i, "linked") for i in mates] + [(i, "own") for i in mine]


def layers(cfg, provider, model):
    """[(label, settings)] for dials.resolve and dials.options: the bonds of the model, the lowest first."""
    entries, _, _ = section(cfg)
    return [('%s["%s"]%s' % (KEY, ident, "" if how == "own" else " (%s)" % how), _settings(entries[ident]))
            for ident, how in bonds(cfg, provider, model)]


def check_settings(settings):
    """The settings with the problems named: ValueError for a dial out of range, a typing mistake in an ace_ key, or
    a value that JSON cannot hold."""
    from . import dials

    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    for key, value in settings.items():
        check_setting(key, value)
    return settings


def check_setting(key, value):
    from . import dials

    key = str(key)
    if key in dials.BY_KEY:
        if dials.valid(dials.BY_KEY[key], value) is None:
            raise ValueError("%s must be %s" % (key, dials._want(dials.BY_KEY[key])))
    elif key.startswith("ace_"):
        raise ValueError("%s: no such dial" % key)
    elif not _json_ok(value):
        raise ValueError("%s: the value must be JSON (text, number, true/false, null, list, object)" % key)


def _json_ok(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return True
    if isinstance(v, (list, tuple)):
        return all(_json_ok(x) for x in v)
    return isinstance(v, dict) and all(isinstance(k, str) and _json_ok(x) for k, x in v.items())


def problems(cfg):
    """What is wrong in the catalog of one file, for `dials` (the same notes as dials.unknown)."""
    raw = cfg.get(KEY) if isinstance(cfg, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["catalog: not an object; ignored"]
    out = []
    entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
    for ident, entry in entries.items():
        if not _ok_id(ident):
            out.append('catalog["%s"]: not VENDOR/MODEL; ignored' % ident)
        elif not isinstance(entry, dict):
            out.append('catalog["%s"]: not an object; ignored' % ident)
        else:
            try:
                check_settings(_settings(entry))
            except ValueError as e:
                out.append('catalog["%s"] %s' % (ident, e))
    for name in ("links", "unlinked"):
        for pair in raw.get(name) or []:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(p in entries for p in pair)):
                out.append("catalog %s %r: both ids need an entry; ignored" % (name, pair))
    return out


# --- reading -----------------------------------------------------------------------------------------------------

def view(cfg, ident, provider=None):
    """One entry as the widget shows it: settings, note, source, the links, the family, the vetoes, and what a
    request for its own model reads from the other entries."""
    shown = set(builtin())
    filed = set(section(cfg)[0])
    cfg = with_builtin(cfg)
    entries, links, unlinked = section(cfg)
    if ident not in entries:
        raise KeyError("no entry %s" % ident)
    vendor, model = split_id(ident)
    entry = entries[ident]
    group = _groups(links).get(ident, frozenset((ident,)))
    veto = _vetoed(unlinked)
    fam = [i for i in family_of(cfg, model if not GLOB.search(model) else "", (ident,))
           if frozenset((i, ident)) not in veto and not GLOB.search(split_id(i)[1])]
    inherits = [{"id": i, "how": how} for i, how in bonds(cfg, vendor if vendor != "*" else None, model)
                if i != ident] if not GLOB.search(model) else []
    return {"id": ident, "vendor": vendor, "model": model, "glob": bool(GLOB.search(model)),
            "family_key": "-".join(family_key(model) or ()) or None, "settings": copy.deepcopy(_settings(entry)),
            "note": entry.get("note") or "", "cloned_from": entry.get("cloned_from"),
            "since": entry.get("since") or "", "updated": entry.get("updated") or "", "date": stamp(entry),
            "master": master(cfg, ident),
            "links": sorted(group - {ident}), "family": fam,
            "unlinked": sorted(p for pair in unlinked if ident in pair for p in pair if p != ident),
            "inherits": inherits, "builtin": ident in shown and ident not in filed,
            "overrides_builtin": ident in shown and ident in filed, "cost": cost_of(model)}


def rows(cfg):
    """Every entry, by vendor then model: the list that the widget shows."""
    entries, _, _ = section(with_builtin(cfg))
    return [view(cfg, i) for i in sorted(entries, key=lambda i: (split_id(i)[0], split_id(i)[1].lower()))]


def suggest(cfg, names=(), floor=0.6):
    """What could be tied that is not: [{"a", "b", "kind", "score"}]. kind "family" is a pair that already shares by
    the automagic (shown so you can see it, or veto it); "near" is a pair that shares most of its words (a
    suggestion: link it, or let it be). names are (provider, model) pairs seen elsewhere (seen()): a name with no
    entry is matched to the entries as if it were one."""
    entries, links, unlinked = section(cfg)
    pool = dict.fromkeys(entries)
    for provider, model in names:
        if provider and model:
            pool.setdefault(make_id(provider, model), None)
    group, veto, out, ids = _groups(links), _vetoed(unlinked), [], sorted(pool)
    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            (va, ma), (vb, mb) = split_id(a), split_id(b)
            if GLOB.search(ma) or GLOB.search(mb) or va == vb and va != "*" and ma == mb:
                continue
            if b in group.get(a, ()) or frozenset((a, b)) in veto:
                continue
            fa, fb = family_key(ma), family_key(mb)
            if fa and fa == fb:
                out.append({"a": a, "b": b, "kind": "family", "score": 1.0})
            else:
                score = _near(ma, mb)
                if score >= floor:
                    out.append({"a": a, "b": b, "kind": "near", "score": round(score, 2)})
    return sorted(out, key=lambda r: (-r["score"], r["a"], r["b"]))


def seen(root):
    """{(provider, model): requests} read from usage.log of the folder (and usage.log.1): the models that ran here."""
    counts = {}
    for name in ("usage.log.1", "usage.log"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = SEEN_LINE.search(line)
                    if m and m.group(1) != "-":
                        key = ((m.group(2) or "").lower() if m.group(2) not in (None, "-") else "", m.group(1))
                        counts[key] = counts.get(key, 0) + 1
        except OSError:
            continue
    return counts


def unlisted(cfg, counts):
    """The models of seen() that no entry covers: [{"id", "requests", "inherits"}]; inherits is what a request for
    it would read (the bonds), so the automagic is visible before an entry exists."""
    out = []
    for (provider, model), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if own(cfg, provider or None, model):
            continue
        out.append({"id": make_id(provider or "*", model), "requests": n,
                    "inherits": [{"id": i, "how": how} for i, how in bonds(cfg, provider or None, model)]})
    return out


# --- writing -----------------------------------------------------------------------------------------------------

def _edit(root, change):
    """Read the file of root (refusing a bad one), let change(cfg, catalog) alter it, write it, give back the config."""
    from . import dials

    cfg = dials._config_for_write(root)
    raw = cfg.get(KEY)
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("cannot edit breadcrumbs config.json: catalog must be an object")
    raw = cfg.setdefault(KEY, {})
    raw.setdefault("entries", {})
    raw.setdefault("links", [])
    raw.setdefault("unlinked", [])
    for name in ("entries",):
        if not isinstance(raw[name], dict):
            raise ValueError("cannot edit breadcrumbs config.json: catalog %s must be an object" % name)
    for name in ("links", "unlinked"):
        if not isinstance(raw[name], list):
            raise ValueError("cannot edit breadcrumbs config.json: catalog %s must be a list" % name)
    change(cfg, raw)
    for name in ("links", "unlinked"):
        raw[name] = sorted({tuple(sorted(p)) for p in raw[name] if isinstance(p, (list, tuple)) and len(p) == 2})
        raw[name] = [list(p) for p in raw[name]]
        if not raw[name]:
            del raw[name]
    dials._write_config(root, cfg)
    return cfg


def _need(raw, ident):
    if ident not in raw["entries"] and ident in builtin():  # a built-in entry is copied into the file, then changed
        entry = copy.deepcopy(builtin()[ident])
        entry.pop("builtin", None)
        entry["updated"] = now()
        raw["entries"][ident] = entry
    if ident not in raw["entries"]:
        raise KeyError("no entry %s" % ident)


def check_date(since):
    if since and not DATE.match(str(since)):
        raise ValueError("a date is YYYY-MM-DD, or YYYY-MM-DDTHH:MM[:SS]: %r" % since)
    return since


def add(root, ident, settings=None, note=None, cloned_from=None, since=None):
    """Create an entry. Refuses a second one of the same id. Returns the view. since: the implementation date."""
    split_id(ident)
    check_settings(settings or {})
    check_date(since)

    def change(cfg, raw):
        if ident in raw["entries"]:
            raise ValueError("%s exists (set changes it, rm removes it)" % ident)
        entry = {"settings": copy.deepcopy(settings or {}), "updated": now()}
        if since:
            entry["since"] = str(since)
        if note:
            entry["note"] = str(note)
        if cloned_from:
            entry["cloned_from"] = cloned_from
        raw["entries"][ident] = entry

    return view(_edit(root, change), ident)


def set_value(root, ident, key, value):
    """Update one setting of an entry (the entry must exist). Returns the view."""
    check_setting(key, value)

    def change(cfg, raw):
        _need(raw, ident)
        entry = raw["entries"][ident]
        if not isinstance(entry.get("settings"), dict):
            entry["settings"] = {}
        entry["settings"][key] = value
        entry["updated"] = now()

    return view(_edit(root, change), ident)


def unset(root, ident, key):
    """Remove one setting of an entry: the value falls back to what the entry reads from its bonds."""
    def change(cfg, raw):
        _need(raw, ident)
        _settings(raw["entries"][ident]).pop(key, None)
        raw["entries"][ident]["updated"] = now()

    return view(_edit(root, change), ident)


def set_since(root, ident, since):
    """Set the implementation date of an entry (YYYY-MM-DD, or with the time); empty clears it, and the entry then
    dates by its last change. This is what decides the master of a family or a link group."""
    check_date(since)

    def change(cfg, raw):
        _need(raw, ident)
        if since:
            raw["entries"][ident]["since"] = str(since)
        else:
            raw["entries"][ident].pop("since", None)

    return view(_edit(root, change), ident)


def set_note(root, ident, note):
    def change(cfg, raw):
        _need(raw, ident)
        if note:
            raw["entries"][ident]["note"] = str(note)
        else:
            raw["entries"][ident].pop("note", None)

    return view(_edit(root, change), ident)


def remove(root, ident):
    """Delete an entry with the links and vetoes that name it. Returns the ids that it was bound to."""
    bound = []

    def change(cfg, raw):
        if ident not in raw["entries"] and ident in builtin():
            raise ValueError("%s is built in: nothing of it is in the file (set a value to override it)" % ident)
        _need(raw, ident)
        del raw["entries"][ident]
        for name in ("links", "unlinked"):
            keep = []
            for p in raw[name]:
                if ident in p:
                    bound.extend(x for x in p if x != ident)
                else:
                    keep.append(p)
            raw[name] = keep

    _edit(root, change)
    return sorted(set(bound))


def clone(root, src, dst, link=False, force=False):
    """Copy the settings of src to a new entry dst. dst may be a vendor alone ("nous": the same model on that
    vendor) or an id. link=True also ties the two. Returns the view of the copy."""
    vendor, model = split_id(src)
    if "/" not in dst:
        dst = make_id(dst, model)
    split_id(dst)
    if dst == src:
        raise ValueError("clone needs another id than %s" % src)

    def change(cfg, raw):
        if src not in raw["entries"] and src not in builtin():
            raise KeyError("no entry %s" % src)
        if dst in raw["entries"] and not force:
            raise ValueError("%s exists (--force replaces it)" % dst)
        entry = copy.deepcopy(raw["entries"].get(src) or builtin()[src])  # a built-in source stays out of the file
        if entry.pop("builtin", None):
            entry["note"] = "cloned from %s (a built-in entry)" % src
        entry["cloned_from"] = src
        entry.pop("since", None)  # a copy is a new implementation: it dates from now
        entry["updated"] = now()
        raw["entries"][dst] = entry
        if link:
            raw["links"].append([src, dst])
            raw["unlinked"] = [p for p in raw["unlinked"] if set(p) != {src, dst}]

    return view(_edit(root, change), dst)


def link(root, a, b):
    """Tie two entries (the family veto of the pair, if any, is lifted). Returns the view of a."""
    if a == b:
        raise ValueError("link needs two different entries")

    def change(cfg, raw):
        _need(raw, a)
        _need(raw, b)
        raw["links"].append([a, b])
        raw["unlinked"] = [p for p in raw["unlinked"] if set(p) != {a, b}]

    return view(_edit(root, change), a)


def unlink(root, a, b):
    """Cut the bond of two entries: the link is removed, and when the two are of one family, the pair is vetoed so
    that the automagic leaves it. A link through a third entry stays: cut that one too."""
    def change(cfg, raw):
        _need(raw, a)
        _need(raw, b)
        raw["links"] = [p for p in raw["links"] if set(p) != {a, b}]
        raw["unlinked"].append([a, b])

    return view(_edit(root, change), a)


def relate(root, a, b):
    """Lift the veto of a pair, so that the automagic applies to it again."""
    def change(cfg, raw):
        raw["unlinked"] = [p for p in raw["unlinked"] if set(p) != {a, b}]

    return view(_edit(root, change), a)


# --- the command line and the widget ----------------------------------------------------------------------------

FORMAT = "breadcrumbs-model-dictionary"


def export(cfg):
    """The dictionary as one document that stands alone: no file name is part of it. What is shown, copied or
    written is the caller's choice (`models export` prints it; `models export FILE` writes the file you name). Only
    the valid parts go out; the dates (since, updated) go with each entry, so the masters survive the move."""
    entries, links, unlinked = section(cfg)
    good = {i: copy.deepcopy(e) for i, e in sorted(entries.items()) if _ok_id(i) and isinstance(e, dict)}
    for entry in good.values():
        try:
            check_settings(_settings(entry))
        except ValueError:
            entry["settings"] = {}
    pairs = lambda ps: sorted(sorted(p) for p in ps if all(i in good for i in p))  # noqa: E731
    doc = {"format": FORMAT, "version": 1, "entries": good}
    if links:
        doc["links"] = pairs(links)
    if unlinked:
        doc["unlinked"] = pairs(unlinked)
    return doc


def check_document(doc):
    """The document that export() makes, or a ValueError that names the first thing wrong. Nothing is changed."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("not a model dictionary (its format is %r)" % FORMAT)
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("entries must be an object")
    for ident, entry in entries.items():
        if not _ok_id(ident):
            raise ValueError("%r: not VENDOR/MODEL" % ident)
        if not isinstance(entry, dict):
            raise ValueError("%s: not an object" % ident)
        try:
            check_settings(_settings(entry))
            check_date(entry.get("since"))
        except ValueError as e:
            raise ValueError("%s: %s" % (ident, e))
    for name in ("links", "unlinked"):
        for pair in doc.get(name) or []:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(i in entries for i in pair)):
                raise ValueError("%s %r: both ids need an entry of the document" % (name, pair))
    return doc


def import_document(root, doc, force=False):
    """Add the entries of an exported document to the dictionary of root, with their dates. An id that exists is
    refused (nothing is written) unless force, which replaces it. Links and vetoes of the document are added.
    Returns the ids that were written."""
    check_document(doc)

    def change(cfg, raw):
        clash = sorted(i for i in doc["entries"] if i in raw["entries"])
        if clash and not force:
            raise ValueError("already here: %s (force replaces them)" % ", ".join(clash))
        for ident, entry in doc["entries"].items():
            entry = copy.deepcopy(entry)
            entry.setdefault("settings", {})
            entry.setdefault("updated", now())
            raw["entries"][ident] = entry
        raw["links"].extend(list(p) for p in doc.get("links") or [])
        raw["unlinked"].extend(list(p) for p in doc.get("unlinked") or [])

    _edit(root, change)
    return sorted(doc["entries"])


def _value(text):
    """A value from the command line: JSON when it parses (0.7, true, null, "x", [1]), else the text."""
    import json

    try:
        return json.loads(text)
    except ValueError:
        return text


def lines(v):
    """The lines of `models show`."""
    out = ["%s%s" % (v["id"], "   (a glob)" if v["glob"] else "")]
    if v["note"]:
        out.append("  note        %s" % v["note"])
    if v["cloned_from"]:
        out.append("  cloned from %s" % v["cloned_from"])
    out.append("  date        %s%s" % (v["date"] or "(undated)", "  (since)" if v["since"] else "  (updated)" if v["updated"] else ""))
    if v["master"] != v["id"]:
        out.append("  master      %s (the newer implementation of this family)" % v["master"])
    for key, value in sorted(v["settings"].items()):
        out.append("  %-18s %r" % (key, value))
    if not v["settings"]:
        out.append("  (no settings of its own)")
    for label, items in (("linked to", v["links"]), ("family", v["family"]), ("vetoed", v["unlinked"])):
        if items:
            out.append("  %-11s %s" % (label, ", ".join(items)))
    for item in v["inherits"]:
        out.append("  reads from  %s (%s)" % (item["id"], item["how"]))
    if v.get("builtin"):
        out.append("  built in    (a copy in the file overrides it)")
    cost = v.get("cost")
    if cost:
        out.append("  cost model  %s" % cost["name"])
        for t in cost["tiers"]:
            out.append("    %-14s input %g  output %g  cache read %g  cache write %g (USD per 1M)"
                       % ("up to %s" % format(t["upto"], ",") if t["upto"] else "above", t["input"], t["output"],
                          t["cache_read"], t["cache_write"]))
        if cost.get("context_window"):
            out.append("    context window %s tokens" % format(cost["context_window"], ","))
    return out


def command(args, home):
    """`python -m breadcrumbs models list|show|add|set|unset|note|since|rm|clone|link|unlink|relate|suggest|seen|export|import`."""
    import sys

    from . import dials

    act, items = args.action, list(args.items or [])
    cfg = dials.config(home)
    try:
        if act == "list":
            r = rows(cfg)
            if not r:
                print("no entries (add VENDOR/MODEL [KEY=VALUE ...])")
            for v in r:
                print("%-44s %-19s %s%s" % (v["id"], v["date"] or "-",
                                            ", ".join("%s=%s" % kv for kv in sorted(v["settings"].items())) or "-",
                                      "   <- " + ", ".join(v["links"] + v["family"]) if v["links"] or v["family"] else ""))
            return 0
        if act == "suggest":
            got = suggest(cfg, seen(home))
            for s in got:
                print("%-7s %.2f  %s  <->  %s" % (s["kind"], s["score"], s["a"], s["b"]))
            if not got:
                print("nothing to tie")
            return 0
        if act == "seen":
            got = unlisted(cfg, seen(home))
            for s in got:
                print("%-44s %6d requests   reads %s" % (s["id"], s["requests"],
                      ", ".join("%s (%s)" % (i["id"], i["how"]) for i in s["inherits"]) or "nothing"))
            if not got:
                print("every model that ran here has an entry")
            return 0
        if act == "export":  # a document on stdout; a file only where one is named
            text = json.dumps(export(cfg), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            if items and items[0] != "-":
                with open(items[0], "w", encoding="utf-8") as f:
                    f.write(text)
                print("wrote %s (%d entries)" % (items[0], len(export(cfg)["entries"])))
            else:
                sys.stdout.write(text)
            return 0
        if act == "import":
            if not items:
                print("models import needs FILE or - (stdin)", file=sys.stderr)
                return 2
            try:
                text = sys.stdin.read() if items[0] == "-" else open(items[0], encoding="utf-8").read()
                doc = json.loads(text)
            except (OSError, ValueError) as e:
                raise ValueError("cannot read %s: %s" % (items[0], e))
            got = import_document(home, doc, force=getattr(args, "force", False))
            print("imported %d entries: %s" % (len(got), ", ".join(got)))
            return 0
        if not items:
            print("models %s needs an id (VENDOR/MODEL)" % act, file=sys.stderr)
            return 2
        ident = items[0]
        if act == "show":
            print("\n".join(lines(view(cfg, ident))))
        elif act == "add":
            settings = {}
            for pair in items[1:]:
                key, sep, text = pair.partition("=")
                if not sep:
                    raise ValueError("a setting is KEY=VALUE: %r" % pair)
                settings[key] = _value(text)
            print("\n".join(lines(add(home, ident, settings, args.note, since=getattr(args, "since", None)))))
        elif act == "set":
            if len(items) < 2 or "=" not in items[1]:
                raise ValueError("set ID KEY=VALUE")
            key, _, text = items[1].partition("=")
            print("\n".join(lines(set_value(home, ident, key, _value(text)))))
        elif act == "unset":
            print("\n".join(lines(unset(home, ident, items[1]))))
        elif act == "since":
            if len(items) < 2:
                raise ValueError("since ID DATE (DATE: YYYY-MM-DD, or - to clear)")
            print("\n".join(lines(set_since(home, ident, "" if items[1] == "-" else items[1]))))
        elif act == "note":
            print("\n".join(lines(set_note(home, ident, " ".join(items[1:])))))
        elif act == "rm":
            print("removed %s (was bound to %s)" % (ident, ", ".join(remove(home, ident)) or "nothing"))
        elif act == "clone":
            if len(items) < 2:
                raise ValueError("clone SRC DST (DST: an id, or a vendor for the same model there)")
            print("\n".join(lines(clone(home, ident, items[1], args.link, args.force))))
        elif act in ("link", "unlink", "relate"):
            if len(items) < 2:
                raise ValueError("%s A B" % act)
            fn = {"link": link, "unlink": unlink, "relate": relate}[act]
            print("\n".join(lines(fn(home, ident, items[1]))))
        else:
            print("models: unknown action %s" % act, file=sys.stderr)
            return 2
    except (KeyError, ValueError) as e:
        print("models %s: %s" % (act, e.args[0] if isinstance(e, KeyError) and e.args else e), file=sys.stderr)
        return 1
    return 0


def apply(root, action, payload=None):
    """The widget's call: one action on the folder's file. Returns {"entries": rows, "suggest": [...]} after it.
    Actions: get, export, import, add, set, unset, note, since, rm, clone, link, unlink, relate."""
    from . import dials

    p = dict(payload or {})
    ident = p.get("id")
    if action == "add":
        add(root, ident, p.get("settings"), p.get("note"))
    elif action == "set":
        set_value(root, ident, p.get("key"), p.get("value"))
    elif action == "unset":
        unset(root, ident, p.get("key"))
    elif action == "note":
        set_note(root, ident, p.get("note"))
    elif action == "since":
        set_since(root, ident, p.get("since"))
    elif action == "rm":
        remove(root, ident)
    elif action == "clone":
        clone(root, ident, p.get("to"), bool(p.get("link")), bool(p.get("force")))
    elif action == "link":
        link(root, ident, p.get("other"))
    elif action == "unlink":
        unlink(root, ident, p.get("other"))
    elif action == "relate":
        relate(root, ident, p.get("other"))
    elif action == "import":
        import_document(root, p.get("document"), bool(p.get("force")))
    elif action not in ("get", "export"):
        raise ValueError("unknown catalog action: %s" % action)
    cfg = dials.config(root)
    out = {"entries": rows(cfg), "suggest": suggest(cfg, seen(root)), "unlisted": unlisted(cfg, seen(root))}
    if action == "export":
        out["export"] = export(cfg)  # the widget offers it as a download: no file is made here
    return out
