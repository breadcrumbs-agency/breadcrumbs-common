"""Disclosure: what a human-facing view may show of a store, and the gate to its private text.

A view (the page, the terminal viewer, the desktop page, a dashboard) shows a store at one level:

    0 hidden      nothing
    1 structure   times, counts, harness, model and token numbers; ids, kinds and sizes; no words
    2 titles      handles and titles, cut short; no body text. This is the default.
    3 public      and the body text of public crumbs
    4 private     and the body text of private crumbs, verbatim

The level is for what a person sees. The tools of the model do not pass through it: they keep the full text.

What a view calls for each thing that it shows (it reads the level once for a request, then uses that number):

    numbers(level)                may it show the numbers (from level 1 on)
    title(text, level)            a title: "" below level 2, else one line, cut short
    cues(words, level)            the cue words of a crumb: none below level 2
    handle(line, level)           a handle line of zoom.py: "" at 0; the id, the kind and the size at 1; from 2 on the
                                  line with its title cut short
    body(text, level, public)     the body text where allows(level, public); else one line that says what is not shown

The rules that this module keeps:

- A view starts at level 2, or lower when the launch flag or the profile says so (start_level). None starts higher.
- The launch flag (allowed) is the gate on the server side: the value of the option --disclosure of the command that
  starts a server, else the environment variable BREADCRUMBS_DISCLOSURE, else 3. It is read once, when the server
  starts. A page can not unlock what the server did not allow.
- Level 4 needs three things: the launch flag at 4, a token that the gate gave (challenge), and the proof of a drag
  (drag_ok): a finger or a mouse slid a bar from its start to its end. There is no typed phrase. A terminal has no
  pointer: there a key slides the same bar (key_trace).
- Each unlock writes one line to <home>/disclosure.log before the level changes (audit). When the line can not be
  written, nothing unlocks. A step up to level 3 is written the same way. A step down never waits for the log.
- Level 4 ends by itself after ttl seconds (15 minutes). While it is on, the state holds a banner text to show.
- A name can not add a line or a field to the log: each value of a line is one word.

A viewer server makes one Gate when it starts, and each request asks it:

    gate = Gate(home, store=name, allowed=allowed(flag))
    gate.level()                      the level now
    gate.state()                      what the page draws: the level, the banner, whether a bar may show
    gate.set(level, who)              to a level from 0 to 3
    gate.challenge()                  the token and the rule of the bar (only when the launch flag is 4)
    gate.unlock(token, trace, who)    to level 4
    gate.lock(who)                    back to the start level

A method that says no raises Refused: its code is one word, its text says the reason.

The proof of a drag (drag_ok, key_trace) knows nothing of levels: it is the confirmation of any action that must not
happen by a stray tap (a removal).
"""
import errno
import json
import math
import os
import re
import secrets
import stat
import threading
import time

from . import dials
from .zoom import DROP, GAP, HANDLE

try:
    import fcntl
except ImportError:  # pragma: no cover - no flock on this platform
    fcntl = None

HIDDEN, STRUCTURE, TITLES, PUBLIC, PRIVATE = 0, 1, 2, 3, 4
DEFAULT = TITLES
NAMES = {HIDDEN: "hidden", STRUCTURE: "structure", TITLES: "titles", PUBLIC: "public", PRIVATE: "private"}
_BY_NAME = {name: level for level, name in NAMES.items()}

ENV = "BREADCRUMBS_DISCLOSURE"  # the launch flag in the environment
KEY = "disclosure"  # the key of config.json by which a profile asks for a lower start level
TITLE_MAX = 40  # the characters of a title in a view; zoom.py cuts the title of a handle at the same place


# -- the levels ----------------------------------------------------------------------------------------------------------
def parse(value):
    """The level that `value` names: a whole number from 0 to 4, such a number as text ("3"), or a name of NAMES in any
    case ("Private"). Anything else raises ValueError: true and false, a number with a fraction, a level that is not
    there, an empty text."""
    level = None
    if isinstance(value, int) and not isinstance(value, bool):
        level = value
    elif isinstance(value, str):
        text = value.strip().lower()
        level = _BY_NAME.get(text)
        if level is None and text.isascii() and text.isdigit() and len(text) < 4:
            level = int(text)
    if level not in NAMES:
        raise ValueError("no disclosure level: %r (a number from 0 to 4, or one of: %s)"
                         % (value, ", ".join(NAMES.values())))
    return level


def _level(level):
    """A level as a view function takes it: parse(), with a short way for a number that is a level already. A value
    that is no level raises ValueError, so a view never shows on a guess."""
    return level if type(level) is int and HIDDEN <= level <= PRIVATE else parse(level)


def allows(level, public):
    """May a view at `level` show the BODY text of a crumb? At level 4 every text; at level 3 the text of a public
    crumb (Crumb.public); below level 3 none."""
    level = _level(level)
    return level >= PRIVATE or (level >= PUBLIC and bool(public))


def numbers(level):
    """May a view at `level` show the numbers: times, counts, harness, model and token numbers? From level 1 on."""
    return _level(level) >= STRUCTURE


_SPACE = re.compile(r"\s+")


def _printable(text):
    """The text without the characters that do not print: a control character, an escape, a mark that turns the
    direction of the text, half of a character."""
    return "".join(c for c in text if c.isprintable())


def _one_line(text):
    """A text as one line that is safe to draw: each run of white space (a line end too) becomes one space, and a
    character that does not print goes."""
    text = "" if text is None else str(text)
    return " ".join(_printable(_SPACE.sub(" ", text)).split())


def title(text, level, limit=TITLE_MAX):
    """What a view at `level` may show of a title: "" at levels 0 and 1 (a title holds words of the text); from level 2
    on the title as one line (white space runs are one space, control characters go) of at most `limit` characters. A
    longer one is cut, and its last character is then the mark of a cut."""
    if _level(level) < TITLES:
        return ""
    line, limit = _one_line(text), int(limit)
    if len(line) <= limit:
        return line
    return line[:limit - 1].rstrip() + DROP if limit > 0 else ""


def cues(words, level):
    """The cue words of a crumb for a view at `level`: as they are from level 2 on (a handle shows them), none below
    it (a cue word is a word of the text). A list gives a list, a text of words gives a text."""
    show = _level(level) >= TITLES
    if isinstance(words, str):
        return words if show else ""
    return list(words or ()) if show else []


_ID = r"[0-9A-Fa-f]{8}(?:\.[0-9]+)?"  # an id of the store, with the ordinal of a collision
_HANDLE = re.compile(
    r"(?P<head> *%s %s [A-Za-z][A-Za-z0-9_-]{0,23})"  # the indent, the mark, the id, the kind
    r"(?P<label> \"(?:[^\"\\]|\\.)*\")?"  # the title, as JSON
    r"(?P<size>(?: [0-9]+ lines)? \u00b7[0-9]+t)"  # the lines of a text, the tokens
    r"(?P<tail> \| .+| \u21d0 [A-Za-z0-9]+)?" % (HANDLE, _ID))  # the cue words, or the source of an opened crumb
_MARKER = re.compile(r" *%s %s:[0-9]+-[0-9]+" % (GAP, _ID))  # the lines of a text that a render left out


def handle(line, level, limit=TITLE_MAX):
    """A handle line of zoom.py for a view at `level`. The line is as Zoom.handle gives it, indented or not, with or
    without its line end: '\u25b8 ID KIND "TITLE" \u00b7Nt | cue words', the handle of a text ('\u25b8 ID doc N lines
    \u00b7Nt'), or the marker of left out lines ('\u22ef ID:a-b').

        level 0       ""
        level 1       the id, the kind and the size: no title and no cue words (no words). A marker stays as it is.
        level 2 to 4  the line as it is, its title cut by title().

    A line that is no handle line gives "" at every level: body text goes through body(). Give handle() only lines
    that Zoom.handle made. It can not tell a line of body text that reads like a handle from a handle, and from level
    2 on it would show the title and the cue words of such a line."""
    level = _level(level)
    text = "" if line is None else str(line)
    end = "\n" if text.endswith("\n") else ""
    text = text[:len(text) - len(end)]
    if level < STRUCTURE or "\n" in text:
        return ""
    if _MARKER.fullmatch(text):
        return text + end
    m = _HANDLE.fullmatch(text)
    if m is None:
        return ""
    if level < TITLES:
        return m.group("head") + m.group("size") + end
    label = m.group("label") or ""
    if label:
        try:
            shown = title(json.loads(label), level, limit)
        except ValueError:  # no JSON text: no handle that zoom.py wrote
            return ""
        label = " " + json.dumps(shown, ensure_ascii=False) if shown else ""
    return m.group("head") + label + m.group("size") + _printable(m.group("tail") or "") + end


def body(text, level, public, note=True):
    """The body text of a crumb for a view at `level`: the text as it is where allows(level, public). Where not: ""
    with note=False, at level 0 and for an empty text; else one line in its place. The line says what is not shown, how
    many lines it has and which level shows it, and it holds no word of the text. It ends with a line end when the text
    did, so it takes the place of the text in a render."""
    level = _level(level)
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    text = "" if text is None else str(text)
    if allows(level, public):
        return text
    if not note or level < STRUCTURE or not text:
        return ""
    n = text.count("\n") + (0 if text.endswith("\n") else 1)
    need = PUBLIC if public else PRIVATE
    return "(%s text, %d line%s: not shown at level %d (%s); level %d (%s) shows it)%s" % (
        "public" if public else "private", n, "" if n == 1 else "s", level, NAMES[level], need, NAMES[need],
        "\n" if text.endswith("\n") else "")


# -- the launch flag -----------------------------------------------------------------------------------------------------
def allowed(flag=None, environ=None):
    """The launch flag: the highest level that a server may give, fixed when it starts. `flag` is the value of a
    command line option (--disclosure 4) when one was given. With no flag the environment variable
    BREADCRUMBS_DISCLOSURE counts, and with neither the answer is 3 (public). So a server that nobody started for level
    4 can never give it, whatever a page asks.

    A value is a level as parse() reads it. A bad value raises ValueError: a server must not start on a guess. An
    environment variable that is empty counts as not set; an empty flag is a bad value."""
    if flag is None:
        flag = (os.environ if environ is None else environ).get(ENV)
        if flag is None or not str(flag).strip():
            return PUBLIC
    return parse(flag)


def start_level(root, allowed=PUBLIC):
    """The level that a view starts at: the default (2), or `allowed` (the launch flag) when that is lower. A profile
    may ask for less: the key "disclosure" at the top level of <root>/config.json, a level as parse() reads it, counts
    when it is below that. It never counts for more, and a value that is no level counts for nothing.

    root: the folder of the store; or a list of folders (the files that a chat reads: its profile's, its scope's), and
    then the lowest level that one of them asks for counts; or None (no file)."""
    level = min(DEFAULT, parse(allowed))
    for folder in ([root] if isinstance(root, (str, os.PathLike)) else list(root or ())):
        try:
            level = min(level, parse(dials.config(folder).get(KEY)))
        except ValueError:
            pass
    return level


# -- the proof of a drag -------------------------------------------------------------------------------------------------
# What makes a list of samples a drag, with the names that a page uses for its bar: at least `samples` samples; the
# first x at most `start`; the last x at least `end`; from `min_ms` to `max_ms` long; x goes back at most `back` in
# total; no step forward of more than `step`.
DRAG = {"samples": 5, "min_ms": 150, "max_ms": 60000, "start": 0.15, "end": 0.95, "back": 0.1, "step": 0.6}
SAMPLES_MAX = 20000  # a longer trace is not read: 60 seconds of a pointer give some thousand samples
_SLACK = 1e-9  # two sums of steps that are equal in the bar can differ by this much as numbers


def drag_ok(trace, need=None):
    """(ok, why): is `trace` the record of a finger or a mouse that slid a bar from its start to its end?

    trace: a list of [t_ms, x] samples as a page records them on pointermove: t_ms the time of the sample in
    milliseconds (from any zero), x the place of the knob from 0.0 (the start of the bar) to 1.0 (its end).
    A real drag has at least 5 samples. Its first x is at most 0.15 and its last x is at least 0.95. Its time never
    runs back, and it is from 150 ms to 60 s long. Its x goes back by at most 0.1 in total, and no step forward is more
    than 0.6: a jump is a tap at the end, not a slide. need: other numbers for these rules, by the keys of DRAG.

    why is "" for a drag, else a few words that say what is missing. Anything that is not such a list gives (False,
    why); nothing raises. This is no defence against a script of the user's own. It stops a stray tap, a stray click
    and a stray request."""
    try:
        return _drag(trace, need)
    except Exception:  # whatever it was, it was no trace
        return False, "the trace is not a list of [t_ms, x] samples"


def _drag(trace, need):
    want = dict(DRAG, **(need or {}))
    if not isinstance(trace, (list, tuple)):
        return False, "the trace is not a list of [t_ms, x] samples"
    if len(trace) < want["samples"]:
        return False, "%d samples: a slide has at least %d" % (len(trace), want["samples"])
    if len(trace) > SAMPLES_MAX:
        return False, "%d samples: more than %d are not read" % (len(trace), SAMPLES_MAX)
    points = []
    for sample in trace:
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            return False, "a sample is not [t_ms, x]"
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in sample):
            return False, "a sample is not two numbers"
        if not 0.0 <= sample[1] <= 1.0:
            return False, "x is not from 0 to 1"
        points.append((float(sample[0]), float(sample[1])))
    if points[0][1] > want["start"]:
        return False, "it does not begin at the start of the bar"
    if points[-1][1] < want["end"]:
        return False, "it does not reach the end of the bar"
    back = 0.0
    for (t0, x0), (t1, x1) in zip(points, points[1:]):
        if t1 < t0:
            return False, "its time runs back"
        if x1 - x0 > want["step"] + _SLACK:
            return False, "a jump, not a slide"
        back += max(0.0, x0 - x1)
    if back > want["back"] + _SLACK:
        return False, "it goes back too far"
    took = points[-1][0] - points[0][0]
    if took < want["min_ms"]:
        return False, "too fast: %d ms, a slide takes at least %d" % (took, want["min_ms"])
    if took > want["max_ms"]:
        return False, "too slow: more than %d ms" % want["max_ms"]
    return True, ""


def key_trace(times, n=8):
    """The trace of a slide by keys, for a viewer that has no pointer (a terminal). The viewer draws the same bar with
    n cells, and one key moves its knob one cell to the end at each press: the right arrow, pressed n times or held
    down. Any other key ends the bar. There is no phrase to type.

    times: the time in milliseconds of each press, in the order of the presses. -> the trace that drag_ok reads: the
    knob at 0 at the first press, then one cell further for each press. So n presses that take from 150 ms to 60 s are
    a drag, and fewer presses, or all of them in one moment, are none. What is no list of numbers gives []."""
    try:
        times, n = [float(t) for t in times], int(n)
    except (TypeError, ValueError, OverflowError):
        return []
    if n < 1 or not times:
        return []
    return [[times[0], 0.0]] + [[t, min(1.0, (i + 1) / n)] for i, t in enumerate(times)]


# -- the audit log -------------------------------------------------------------------------------------------------------
LOG_NAME = "disclosure.log"
WORD_MAX = 120  # a value of a line is cut here
LINE_MAX = 2048  # no line is longer than this in bytes: 4 values of 120 characters, the stamp, the keys, the numbers
_STAMP = "%Y-%m-%dT%H:%M:%S%z"
_LINE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{4}) event=(\S+) level=([0-4])"
                   r" store=(\S+) who=(\S+) pid=([0-9]{1,18}) why=(\S+)")


def word(value):
    """A value of a log line as ONE word: each white space character and each character that does not print (a line
    end, a control character) becomes "_", the word is cut at 120 characters, and an empty value is "-". So a name
    can never add a line or a field to the log."""
    text = "" if value is None else str(value).strip()
    return "".join("_" if c.isspace() or not c.isprintable() else c for c in text)[:WORD_MAX] or "-"


def audit(home, event, level, store="", who="", why="", clock=time.time):
    """Add one line to <home>/disclosure.log and return it (without its line end):

        2026-11-22T13:05:09-0600 event=unlock level=4 store=NAME who=WORD pid=123 why=-

    The local time with its UTC offset; the event (a Gate writes unlock, refused, expire, lock and set); the level
    after the event, or the level that was asked for when the event is refused; the store shown; who confirmed; the
    process; the reason (the code of a refusal). Each value is one word (word()), and the fields have this order.

    The line goes to the file in one os.write. The file is opened to append, it is made with mode 0600 when it is
    missing, and the write holds an exclusive flock on it where fcntl exists: the lines of two servers never mix.
    When the file ends in the middle of a line (a writer died there), this line starts a new one. The folder is not
    made: a home that is not there is an error. A failure raises OSError, and the caller must then not go on to what
    the line was to record."""
    line = "%s event=%s level=%d store=%s who=%s pid=%d why=%s" % (
        time.strftime(_STAMP, time.localtime(clock())), word(event), parse(level), word(store), word(who), os.getpid(),
        word(why))
    data = line.encode("utf-8", "replace") + b"\n"
    fd = os.open(os.path.join(home, LOG_NAME), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)  # it ends when the file closes
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", os.path.join(home, LOG_NAME))
        if st.st_size and hasattr(os, "pread") and os.pread(fd, 1, st.st_size - 1) != b"\n":
            data = b"\n" + data
        done = os.write(fd, data)
        if done != len(data):
            raise OSError(errno.EIO, "wrote %d of %d bytes" % (done, len(data)), os.path.join(home, LOG_NAME))
    finally:
        os.close(fd)
    return line


def read_audit(home, n=50):
    """The newest n lines of <home>/disclosure.log that parse, the oldest of them first: each a dict {ts (the time as
    written), event, level, store, who, pid, why}, level and pid as numbers, and "" for a value that was empty. A line
    that does not parse is skipped, and so is a last line with no line end (it is not whole yet). No file: []."""
    if n <= 0:
        return []
    want = n * LINE_MAX
    while True:
        try:
            with open(os.path.join(home, LOG_NAME), "rb") as f:
                start = max(0, f.seek(0, os.SEEK_END) - want)
                f.seek(start)
                data = f.read()
        except OSError:
            return []
        lines = data.decode("utf-8", "replace").split("\n")[:-1]
        out = []
        for line in reversed(lines[1:] if start else lines):  # read from inside the file, the first line is a part
            m = _LINE.fullmatch(line)
            if m is None:
                continue
            try:
                time.strptime(m.group(1), _STAMP)
            except ValueError:
                continue
            empty = lambda v: "" if v == "-" else v
            out.append({"ts": m.group(1), "event": m.group(2), "level": int(m.group(3)), "store": empty(m.group(4)),
                        "who": empty(m.group(5)), "pid": int(m.group(6)), "why": empty(m.group(7))})
            if len(out) == n:
                break
        if len(out) == n or not start:
            return out[::-1]
        want *= 4  # lines that do not parse took the room: read further back


# -- the gate ------------------------------------------------------------------------------------------------------------
REASONS = {  # the code of a refusal -> its words
    "not_allowed": "the server was not started for this level",
    "bad_token": "this is no token that the gate gave, or it was used before",
    "expired": "the token is too old",
    "no_drag": "this level needs a slide of the bar from its start to its end",
    "log_failed": "the audit line could not be written",
    "bad_level": "no such level",
}
BANNER = "Private text is shown (level 4) since %s. It locks again at %s."
TOKENS_MAX = 64  # the tokens of one gate that wait for their drag; the oldest goes for a new one


class Refused(Exception):
    """A gate said no. code: one word, a key of REASONS. str(): the reason in words."""

    def __init__(self, code, text=""):
        super().__init__(text or REASONS.get(code, code))
        self.code = code


def _hour(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


class Gate:
    """The disclosure state of one viewer server: its level now, and the way to level 4 and back. A server is one
    process with many requests at a time, so each method holds the lock of the gate.

    A request reads level() once and shows everything at that level. A page draws state(), asks challenge() for the
    token of its bar, and sends the token with the trace of the drag to unlock(). A terminal viewer does the same
    with key_trace(). `who` is a word for the log that says who confirmed: a web handler gives the address of the
    client, a terminal viewer the name of its user."""

    def __init__(self, home, store="", allowed=PUBLIC, level=None, clock=time.time, ttl=900.0, challenge_ttl=120.0):
        """home: the folder that gets disclosure.log (the breadcrumbs home). store: the name or the path of the store
        shown, for the log and the state. allowed: the launch flag (allowed()). level: the level that the view starts
        at (start_level()); None is the default, or `allowed` when that is lower. A start above that raises
        ValueError: a view gets there by set() and unlock(), which the log records. clock: seconds since 1970.
        ttl: the seconds that level 4 lasts. challenge_ttl: the seconds that a token waits for its drag."""
        self.home, self.store, self.clock = home, store, clock
        self.allowed = parse(allowed)
        top = min(DEFAULT, self.allowed)
        self.start = top if level is None else parse(level)
        if self.start > top:
            raise ValueError("a view starts at level %d or lower, not at %d" % (top, self.start))
        self.ttl, self.challenge_ttl = float(ttl), float(challenge_ttl)
        if not (0 < self.ttl < math.inf and 0 < self.challenge_ttl < math.inf):
            raise ValueError("ttl and challenge_ttl are seconds above 0")
        self._lock = threading.Lock()
        self._level, self._since, self._expires = self.start, None, None
        self._tokens = {}  # token -> the time that it ends

    def _log(self, event, level, who="", why=""):
        return audit(self.home, event, level, self.store, who, why, self.clock)

    def _down(self, event, level, who="", why=""):
        """Leave level 4 (the lock is held). The line is written when it can be: a step down never waits for the log."""
        self._level, self._since, self._expires = level, None, None
        try:
            self._log(event, level, who, why)
        except OSError:
            pass

    def _now(self):
        """The level now (the lock is held). A level 4 that is ttl seconds old ends here, and so does one whose clock
        ran back to before its start."""
        if self._level >= PRIVATE:
            now = self.clock()
            if now >= self._expires or now < self._since:
                self._down("expire", self.start, why="ttl" if now >= self._expires else "clock")
        return self._level

    def _state(self):
        level = self._now()
        private = level >= PRIVATE
        return {"level": level, "name": NAMES[level], "allowed": self.allowed, "can_unlock": self.allowed >= PRIVATE,
                "banner": BANNER % (_hour(self._since), _hour(self._expires)) if private else "",
                "since": self._since, "expires": self._expires, "now": self.clock(), "store": self.store}

    def level(self):
        """The level now. A level 4 that is older than ttl seconds falls back to the start level; the first call that
        sees it writes the audit line event=expire."""
        with self._lock:
            return self._now()

    def state(self):
        """What a view draws: {level, name, allowed, can_unlock (the launch flag is 4: the view may show its bar),
        banner (a text while the level is 4: private text is shown, since when, and when it locks again; else ""),
        since and expires (the times of level 4, else None), now (the clock of the gate, so a page can count down
        without its own), store}."""
        with self._lock:
            return self._state()

    def set(self, level, who=""):
        """Go to a level from 0 to 3; no drag is needed. -> the state. Refused: bad_level (no level), not_allowed (above
        the launch flag), no_drag (level 4: that is unlock()), log_failed. A step up to level 3 and a step down from
        level 4 write an audit line (event=set, the new level). A step up to 3 whose line can not be written does not
        happen; a step down happens with or without its line."""
        try:
            want = parse(level)
        except ValueError:
            raise Refused("bad_level", "no such level: %r" % (level,)) from None
        with self._lock:
            have = self._now()
            if want > self.allowed:
                raise Refused("not_allowed")
            if want >= PRIVATE:
                raise Refused("no_drag")
            if have >= PRIVATE:
                self._down("set", want, who)
            elif want >= PUBLIC > have:
                try:
                    self._log("set", want, who)
                except OSError as e:
                    raise Refused("log_failed", "%s: %s" % (REASONS["log_failed"], e)) from None
            self._level = want
            return self._state()

    def challenge(self):
        """What a view needs to show its bar: {token (random, for one unlock), expires (the time that the token ends),
        ttl (the same in seconds from now), and the rule of the drag by the keys of DRAG: samples, min_ms, max_ms,
        start, end, back, step}. Refused (not_allowed) when the launch flag is below 4: the view then shows no bar."""
        with self._lock:
            if self.allowed < PRIVATE:
                raise Refused("not_allowed")
            now = self.clock()
            for token in [t for t, ends in self._tokens.items() if ends <= now]:
                del self._tokens[token]
            while len(self._tokens) >= TOKENS_MAX:
                del self._tokens[next(iter(self._tokens))]
            token = secrets.token_urlsafe(24)
            self._tokens[token] = now + self.challenge_ttl
            return dict(DRAG, token=token, expires=now + self.challenge_ttl, ttl=self.challenge_ttl)

    def unlock(self, token, trace, who):
        """Go to level 4. It needs all of: the launch flag at 4; a token that this gate gave (challenge), not used and
        not too old; a trace that is a drag (drag_ok). A token counts for one call, whatever the answer.

        Then the audit line is written (event=unlock level=4, the store, who), and after it the level is 4 for ttl
        seconds. -> the state. Any failure raises Refused (not_allowed, bad_token, expired, no_drag, log_failed) and
        leaves the level as it was; the log gets a line event=refused with that code as its reason, when it can be
        written. When the line of the unlock itself can not be written, the answer is Refused (log_failed)."""
        with self._lock:
            self._now()
            code, text = self._check(token, trace)
            if not code:
                try:
                    self._log("unlock", PRIVATE, who)
                except OSError as e:
                    code, text = "log_failed", "%s: %s" % (REASONS["log_failed"], e)
            if code:
                try:
                    self._log("refused", PRIVATE, who, code)
                except OSError:
                    pass
                raise Refused(code, text)
            now = self.clock()
            self._level, self._since, self._expires = PRIVATE, now, now + self.ttl
            return self._state()

    def _check(self, token, trace):
        """(code, text) of what stands against an unlock, or ("", ""). The token is used up here."""
        if self.allowed < PRIVATE:
            return "not_allowed", ""
        ends = self._tokens.pop(token, None) if isinstance(token, str) else None
        if ends is None:
            return "bad_token", ""
        if self.clock() >= ends:
            return "expired", ""
        ok, why = drag_ok(trace)
        return ("", "") if ok else ("no_drag", "%s (%s)" % (REASONS["no_drag"], why))

    def lock(self, who=""):
        """Back to the start level. -> the state. When the level was 4 the log gets a line event=lock."""
        with self._lock:
            if self._now() >= PRIVATE:
                self._down("lock", self.start, who)
            self._level = self.start
            return self._state()
