"""Task scheduler: one-off and recurring Finch jobs.

A job is a prompt, a working directory and a schedule. When it is due, Finch runs it headless
(`finch -p <prompt> -C <workdir>`), writes the output to a log and records the result.

Schedules (parse_when):
  one-off    in 10m · in 1h30m · in 2 days · at 17:30 · 5pm · tomorrow 9am · friday at 08:15 ·
             oct 20 at 9am · 2026-10-20 09:00
  recurring  every 30m · every 2 hours · hourly · daily at 9am · every day at 18:00 ·
             weekdays at 8:30 · weekends at 10am · every monday at 9am · every mon, wed and fri at 7pm ·
             monthly on the 1st at 9am · cron 0 9 * * 1-5  (or a bare 5-field cron expression)

Who runs jobs: every open Finch REPL checks every 15 s, and `finch schedule daemon` (or `start`)
runs them with no window open. Several runners may be active at once: a job is claimed under a
file lock and only one job runs at a time across all of them (one GPU). Each run is watched by
its own supervisor process (`finch schedule exec <id>`), so closing the window that started it
does not orphan the record.

Missed runs: a recurring job that was missed runs once on catch-up, then continues from now (no
backfill storm). A one-off job runs late if it is less than 24 h overdue, otherwise it is
marked missed. Jobs wait while you are running a task in a Finch window (they would compete for
the model), for at most 30 minutes.

Scheduled runs are unattended: risky actions are denied unless the job has auto-approve on
(the hard safety ring always stays on).
"""
import calendar
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time

from . import config

TICK = 15                       # seconds between runner checks
ONCE_GRACE = 24 * 3600          # a one-off job later than this is marked missed
BUSY_DEFER_MAX = 30 * 60        # max wait for an interactive task to finish
DEFAULT_TIMEOUT = 2 * 3600      # hard kill
DEFAULT_TIMEBOX = 30.0          # soft budget (minutes) passed as --timebox
KEEP_LOGS = 30                  # per job
MIN_INTERVAL = 60

_CHILDREN = []                  # supervisor Popen objects started by this process (reaped in tick)


# ---------------- paths ----------------
def home():
    return os.path.join(config.FINCH_HOME, "schedule")


def jobs_file():
    return os.path.join(home(), "jobs.json")


def logs_dir(jid):
    return os.path.join(home(), "logs", jid)


def pid_file():
    return os.path.join(home(), "daemon.pid")


def daemon_log():
    return os.path.join(home(), "daemon.log")


def busy_dir():
    return os.path.join(home(), "busy")


# ---------------- time helpers ----------------
def _ts(d):
    return time.mktime(d.timetuple())


def _dt(ts):
    return dt.datetime.fromtimestamp(ts)


def fmt_time(ts, now=None):
    """'in 14m' / 'in 3h 5m' / 'tomorrow 09:00' / 'Tue 13 Oct 09:00'."""
    if not ts:
        return "-"
    now = now or time.time()
    d, n = _dt(ts), _dt(now)
    delta = ts - now
    if 0 <= delta < 3600:
        return f"in {max(1, round(delta / 60))}m"
    if 0 <= delta < 6 * 3600:
        h, m = divmod(round(delta / 60), 60)
        return f"in {h}h {m}m" if m else f"in {h}h"
    if d.date() == n.date():
        return f"today {d:%H:%M}"
    if d.date() == n.date() + dt.timedelta(days=1):
        return f"tomorrow {d:%H:%M}"
    return f"{d:%a} {d.day} {d:%b} {d:%H:%M}" + (f" {d.year}" if d.year != n.year else "")


def fmt_ago(ts, now=None):
    if not ts:
        return "never"
    s = (now or time.time()) - ts
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{round(s / 60)}m ago"
    if s < 86400:
        return f"{round(s / 3600)}h ago"
    return f"{round(s / 86400)}d ago"


def fmt_dur(sec):
    sec = int(sec)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n and sec % n == 0:
            return f"{sec // n}{unit}"
    if sec >= 3600:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    return f"{sec // 60}m" if sec >= 60 else f"{sec}s"


# ---------------- cron ----------------
_MONTHS = {calendar.month_abbr[i].lower(): i for i in range(1, 13)}
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _cval(tok, names):
    tok = tok.strip()
    if names and tok[:3] in names:
        return names[tok[:3]]
    return int(tok)


def _cfield(spec, lo, hi, names=None):
    out = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            step = int(st)
            if step < 1:
                raise ValueError("step must be >= 1")
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            a, b = (_cval(x, names) for x in part.split("-", 1))
        else:
            a = _cval(part, names)
            b = hi if step > 1 else a           # "5/10" = from 5, every 10
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise ValueError(f"'{spec}' is out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    """5-field cron: minute hour day-of-month month day-of-week (names, ranges, lists, steps).
    Like cron, a restricted day-of-month and day-of-week match if EITHER matches."""

    def __init__(self, expr):
        f = expr.split()
        if len(f) != 5:
            raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
        try:
            self.minutes = sorted(_cfield(f[0], 0, 59))
            self.hours = sorted(_cfield(f[1], 0, 23))
            self.dom = _cfield(f[2], 1, 31)
            self.months = _cfield(f[3], 1, 12, _MONTHS)
            self.dow = {d % 7 for d in _cfield(f[4], 0, 7, _DAYS)}
        except ValueError as e:
            raise ValueError(f"bad cron expression '{expr}': {e}") from None
        self.expr = " ".join(f)
        self.dom_any, self.dow_any = f[2] in ("*", "?"), f[4] in ("*", "?")

    def _day_ok(self, d):
        if d.month not in self.months:
            return False
        dom_ok, dow_ok = d.day in self.dom, (d.weekday() + 1) % 7 in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, t):
        """First matching local (naive) datetime strictly after t."""
        t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        day = t.date()
        for _ in range(366 * 8):
            if self._day_ok(day):
                for h in self.hours:
                    if day == t.date() and h < t.hour:
                        continue
                    for m in self.minutes:
                        if day == t.date() and h == t.hour and m < t.minute:
                            continue
                        return dt.datetime.combine(day, dt.time(h, m))
            day += dt.timedelta(days=1)
        raise ValueError(f"cron expression '{self.expr}' never matches")


# ---------------- schedule parsing ----------------
_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
_TOD_RE = r"(?:noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2})"
_FULL_DAYS = {"sun": "sunday", "mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday",
              "fri": "friday", "sat": "saturday"}


def parse_duration(text):
    """'10m' / '1h30m' / '2 days' / 'an hour' / '90 seconds' -> seconds (int) or None."""
    s = re.sub(r"^(an?|one)\s+", "1 ", text.strip().lower())
    s = re.sub(r"\band\b|,", " ", s)
    if re.fullmatch(r"(an?\s+)?half\s+(an\s+)?hour", text.strip().lower()):
        return 1800
    pos, total = 0, 0
    for m in _DUR_RE.finditer(s):
        if s[pos:m.start()].strip():
            return None
        if m.group(2) not in _UNITS:
            return None
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos == 0 or s[pos:].strip():
        return None
    return int(total)


def parse_tod(text):
    """'9am' / '9:30' / '17:05' / '7.30pm' / 'noon' -> (hour, minute) or None."""
    s = text.strip().lower().replace(".", ":")
    if s == "noon":
        return 12, 0
    if s == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m or (m.group(2) is None and m.group(3) is None):
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m.group(3) == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    return h, mi


_DAY_LOOKUP = {}
for _k, _full in _FULL_DAYS.items():
    for _f in (_k, _full, _full + "s"):
        _DAY_LOOKUP[_f] = _DAYS[_k]
_DAY_LOOKUP.update(tues=2, weds=3, thur=4, thurs=4)
_PLURAL_DAYS = {f + "s" for f in _FULL_DAYS.values()}


def _day_index(word):
    return _DAY_LOOKUP.get(word.lower().strip("."))


def _days_list(text):
    """'mon, wed and fri' / 'monday' / 'tuesdays' -> sorted cron weekday numbers, or None."""
    parts = [p for p in re.split(r"\s*(?:,|\band\b|&|\+)\s*|\s+", text.strip()) if p]
    out = []
    for p in parts:
        i = _day_index(p)
        if i is None:
            return None
        out.append(i)
    return sorted(set(out)) or None


def _ordinal(text):
    m = re.fullmatch(r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?", text.strip())
    if m and 1 <= int(m.group(1)) <= 31:
        return int(m.group(1))
    return None


def _split_tod(s):
    """Pull a time of day out of a phrase: 'every day at 9am' -> ('every day', (9, 0))."""
    m = re.search(r"(?:^|\s)at\s+(" + _TOD_RE + r"|\d{1,2})(?=\s|$)", s)
    if m:
        h = parse_tod(m.group(1)) or (parse_tod(m.group(1) + ":00") if m.group(1).isdigit() else None)
        if h is None:
            raise ValueError(f"'{m.group(1)}' is not a time of day")
        return (s[:m.start()] + s[m.end():]).strip(), h
    m = re.search(r"(?:^|\s)(" + _TOD_RE + r")$", s)
    if m:
        h = parse_tod(m.group(1))
        if h is not None:
            return s[:m.start()].strip(), h
    m = re.match(r"^(" + _TOD_RE + r")(?=\s|$)", s)
    if m:
        h = parse_tod(m.group(1))
        if h is not None:
            return s[m.end():].strip(), h
    return s, None


def _hm(h):
    return f"{h[0]:02d}:{h[1]:02d}"


def _days_desc(days):
    if days == [1, 2, 3, 4, 5]:
        return "weekdays"
    if days == [0, 6]:
        return "weekends"
    names = [_FULL_DAYS[_DAY_NAMES[d].lower()].capitalize() for d in days]
    return "every " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def _cron_desc(expr):
    """Readable description for the cron shapes parse_when generates (falls back to the expr)."""
    f = expr.split()
    if len(f) == 5 and f[0].isdigit() and f[1].isdigit():
        at = f"{int(f[1]):02d}:{int(f[0]):02d}"
        if f[2:] == ["*", "*", "*"]:
            return f"every day at {at}"
        if f[2:4] == ["*", "*"] and re.fullmatch(r"[\d,\-]+", f[4]):
            try:
                return f"{_days_desc(sorted({d % 7 for d in _cfield(f[4], 0, 7)}))} at {at}"
            except ValueError:
                pass
        if f[2].isdigit() and f[3:] == ["*", "*"]:
            n = int(f[2])
            suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
            return f"monthly on the {n}{suf} at {at}"
    if len(f) == 5 and f[0].isdigit() and f[1:] == ["*", "*", "*", "*"]:
        return f"hourly at :{int(f[0]):02d}"
    return f"cron {expr}"


def parse_when(text, now=None):
    """Schedule phrase -> {"kind": "once", "at": ts} | {"kind": "interval", "every": s} |
    {"kind": "cron", "cron": expr}, plus "desc". Raises ValueError with a hint."""
    raw = (text or "").strip()
    s = re.sub(r"\s+", " ", raw.lower()).strip().strip("\"'")
    now = now if now is not None else time.time()
    n = _dt(now)
    if not s:
        raise ValueError("empty schedule")

    # ---- raw cron ----
    m = re.fullmatch(r"(?:cron\s+)?([\d*/,\-]+ [\d*/,\-]+ [\w*/,\-?]+ [\w*/,\-?]+ [\w*/,\-?]+)", s)
    if m and (s.startswith("cron") or re.match(r"[\d*/,\-]+ [\d*/,\-]+ ", s)):
        c = Cron(m.group(1))
        c.next_after(n)
        return {"kind": "cron", "cron": c.expr, "desc": _cron_desc(c.expr)}
    if s.startswith("cron"):
        raise ValueError("cron needs 5 fields, e.g. 'cron 0 9 * * 1-5'")

    # ---- relative one-off ----
    if s.startswith("in "):
        d = parse_duration(s[3:])
        if not d:
            raise ValueError(f"cannot read the duration in '{raw}' (try 'in 10m', 'in 2 hours')")
        at = _dt(now + d)
        return {"kind": "once", "at": now + d, "desc": f"once at {at:%a} {at.day} {at:%b %H:%M}"}

    # ---- recurring ----
    rec = re.match(r"^(every|each)\s+(.*)$", s)
    first = s.split()[0].rstrip(",")
    if rec or first in ("hourly", "daily", "weekly", "monthly", "weekdays", "weekends") or first in _PLURAL_DAYS:
        body = rec.group(2) if rec else s
        if body in ("minute", "min"):
            return {"kind": "interval", "every": 60, "desc": "every minute"}
        if body in ("hour", "hourly") or s == "hourly":
            return {"kind": "cron", "cron": "0 * * * *", "desc": "hourly at :00"}
        d = parse_duration(body)
        if d:
            if d < MIN_INTERVAL:
                raise ValueError("the shortest interval is 1 minute")
            return {"kind": "interval", "every": d, "desc": "every " + fmt_dur(d)}
        rest, tod = _split_tod(body)
        rest = re.sub(r"^(on\s+)", "", rest).strip()
        if tod is None:
            tod = (9, 0)
        hm = f"{tod[1]} {tod[0]}"
        if rest in ("day", "days", "daily"):
            return {"kind": "cron", "cron": f"{hm} * * *", "desc": f"every day at {_hm(tod)}"}
        if rest in ("weekday", "weekdays", "workday", "workdays"):
            return {"kind": "cron", "cron": f"{hm} * * 1-5", "desc": f"weekdays at {_hm(tod)}"}
        if rest in ("weekend", "weekends"):
            return {"kind": "cron", "cron": f"{hm} * * 0,6", "desc": f"weekends at {_hm(tod)}"}
        mm = re.fullmatch(r"(?:month|monthly)(?:\s+on)?(?:\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?)?", rest)
        if mm:
            day = int(mm.group(1)) if mm and mm.group(1) else 1
            if not 1 <= day <= 31:
                raise ValueError("day of month must be 1-31")
            expr = f"{hm} {day} * *"
            return {"kind": "cron", "cron": expr, "desc": _cron_desc(expr)}
        wk = re.fullmatch(r"(?:week|weekly)(?:\s+on)?\s+(.+)", rest)
        days = _days_list(wk.group(1) if wk else rest)
        if days:
            expr = f"{hm} * * {','.join(map(str, days))}"
            return {"kind": "cron", "cron": expr, "desc": f"{_days_desc(days)} at {_hm(tod)}"}
        raise ValueError(f"cannot read '{raw}' (try 'every day at 9am', 'every 30m', "
                         "'weekdays at 8:30', 'every monday at 9am', 'cron 0 9 * * 1-5')")

    # ---- absolute one-off ----
    rest, tod = _split_tod(s)
    rest = re.sub(r"^(on|next|this)\s+", "", rest).strip()
    day = None
    if rest in ("", "today"):
        day = n.date()
        if tod is None:
            raise ValueError(f"cannot read '{raw}' (try 'at 17:30', 'tomorrow 9am', 'in 10m')")
        if rest == "" and dt.datetime.combine(day, dt.time(*tod)) <= n:
            day += dt.timedelta(days=1)          # '17:30' already passed -> tomorrow
    elif rest == "tomorrow":
        day = n.date() + dt.timedelta(days=1)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}(t\d{2}:\d{2}(:\d{2})?)?", rest):
        if "t" in rest:
            d = dt.datetime.fromisoformat(rest.upper().replace("T", " "))
            day, tod = d.date(), (d.hour, d.minute)
        else:
            day = dt.date.fromisoformat(rest)
    else:
        days = _days_list(rest)
        if days and len(days) == 1:
            ahead = (days[0] - (n.weekday() + 1) % 7) % 7
            day = n.date() + dt.timedelta(days=ahead)
            if tod and ahead == 0 and dt.datetime.combine(day, dt.time(*tod)) <= n:
                day += dt.timedelta(days=7)
            elif not tod and ahead == 0:
                day += dt.timedelta(days=7)
        else:
            mo = re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*", rest)
            if mo:
                mon = _MONTHS.get(mo.group(1) or mo.group(4))
                dd = int(mo.group(2) or mo.group(3))
                if mon:
                    try:
                        day = dt.date(n.year, mon, dd)
                    except ValueError:
                        raise ValueError(f"no such date: '{rest}'") from None
                    if dt.datetime.combine(day, dt.time(*(tod or (9, 0)))) <= n:
                        day = day.replace(year=n.year + 1)
    if day is None:
        raise ValueError(f"cannot read '{raw}' (try 'in 10m', 'tomorrow 9am', 'friday at 17:00', "
                         "'2026-10-20 09:00', 'every day at 9am', 'cron 0 9 * * 1-5')")
    tod = tod or (9, 0)
    at = dt.datetime.combine(day, dt.time(*tod))
    if at <= n:
        raise ValueError(f"{at:%Y-%m-%d %H:%M} is in the past")
    return {"kind": "once", "at": _ts(at), "desc": f"once at {at:%a} {at.day} {at:%b %H:%M}"}


def split_when(text, now=None):
    """'every day at 9am check the news' -> ('every day at 9am', 'check the news').
    An explicit ' -- ' or a quoted schedule wins; otherwise the longest leading phrase that parses."""
    text = text.strip()
    if " -- " in text:
        w, p = text.split(" -- ", 1)
        parse_when(w, now)
        return w.strip(), p.strip()
    m = re.match(r"^([\"'])(.+?)\1\s+(.+)$", text, re.S)
    if m:
        parse_when(m.group(2), now)
        return m.group(2), m.group(3).strip()
    words = text.split(" ")
    err = None
    for k in range(min(len(words) - 1, 12), 0, -1):
        try:
            parse_when(" ".join(words[:k]), now)
            return " ".join(words[:k]), " ".join(words[k:]).strip()
        except ValueError as e:
            err = err or e
    raise ValueError("could not find a schedule at the start (e.g. 'every day at 9am <task>', "
                     "'in 10m <task>', or '<schedule> -- <task>')")


def first_run(spec, now=None):
    now = now if now is not None else time.time()
    if spec["kind"] == "once":
        return spec["at"]
    if spec["kind"] == "interval":
        return now + spec["every"]
    return _ts(Cron(spec["cron"]).next_after(_dt(now)))


def upcoming(job, n=3, now=None):
    """The next n run times of a job (or parsed spec)."""
    now = now if now is not None else time.time()
    t = job.get("next_run") if "next_run" in job else first_run(job, now)
    out = []
    while t and len(out) < n:
        out.append(t)
        if job["kind"] == "once":
            break
        t = t + job["every"] if job["kind"] == "interval" else _ts(Cron(job["cron"]).next_after(_dt(t)))
    return out


# ---------------- store ----------------
def _read():
    try:
        with open(jobs_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


@contextlib.contextmanager
def _locked():
    """Exclusive read-modify-write of jobs.json across processes. Saved only if the block succeeds."""
    os.makedirs(home(), exist_ok=True)
    with open(os.path.join(home(), ".lock"), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            jobs = _read()
            yield jobs
            config.atomic_write_json(jobs_file(), jobs)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_jobs():
    return _read()


def find(ref, jobs=None):
    """By list number (1-based), id, id prefix, or name (case-insensitive)."""
    jobs = load_jobs() if jobs is None else jobs
    ref = str(ref).strip()
    if ref.isdigit() and 1 <= int(ref) <= len(jobs) and not any(j["id"] == ref for j in jobs):
        return jobs[int(ref) - 1]
    for j in jobs:
        if j["id"] == ref or j["name"].lower() == ref.lower():
            return j
    hits = [j for j in jobs if j["id"].startswith(ref) or j["name"].lower().startswith(ref.lower())]
    return hits[0] if len(hits) == 1 else None


def _slug(prompt, taken):
    stop = {"a", "an", "the", "in", "on", "at", "to", "of", "for", "and", "into", "my", "me", "please", "with"}
    words = [w for w in re.findall(r"[a-z0-9]+", prompt.lower()) if w not in stop]
    base = "-".join(words[:4])[:28].strip("-") or "job"
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


def add_job(when, prompt, workdir, name=None, model=None, soul=None, yes=False,
            timebox=DEFAULT_TIMEBOX, timeout=DEFAULT_TIMEOUT, now=None):
    now = now if now is not None else time.time()
    if not prompt or not prompt.strip():
        raise ValueError("the job needs a prompt (what Finch should do)")
    workdir = os.path.abspath(os.path.expanduser(workdir))
    if not os.path.isdir(workdir):
        raise ValueError(f"no such directory: {workdir}")
    spec = parse_when(when, now)
    with _locked() as jobs:
        taken = {j["name"] for j in jobs}
        if name and name in taken:
            raise ValueError(f"a job named '{name}' already exists")
        job = {"id": secrets.token_hex(3), "name": name or _slug(prompt, taken), "prompt": prompt.strip(),
               "workdir": workdir, "when": when.strip(), **spec, "enabled": True, "created": now,
               "next_run": first_run(spec, now), "model": model or None, "soul": soul,
               "yes": bool(yes), "timebox": timebox, "timeout": int(timeout),
               "last_run": None, "last_finished": None, "last_status": None, "last_exit": None,
               "last_log": None, "last_summary": "", "runs": 0, "running": None, "run_now": False}
        jobs.append(job)
    return job


def _mutate(ref, fn):
    with _locked() as jobs:
        j = find(ref, jobs)
        if not j:
            raise ValueError(f"no job '{ref}' (see /schedule list)")
        fn(j)
        return dict(j)


def remove(ref):
    with _locked() as jobs:
        j = find(ref, jobs)
        if not j:
            raise ValueError(f"no job '{ref}'")
        jobs.remove(j)
    import shutil
    shutil.rmtree(logs_dir(j["id"]), ignore_errors=True)
    return j


def set_enabled(ref, on, now=None):
    now = now if now is not None else time.time()

    def fn(j):
        if on and not j["enabled"]:
            if j["kind"] == "once":
                if not j.get("next_run") or j["next_run"] <= now:
                    raise ValueError("this one-off job's time has passed: give it a new time with "
                                     f"/schedule when {j['name']} <time>")
            else:
                j["next_run"] = first_run(j, now)
        j["enabled"] = bool(on)
    return _mutate(ref, fn)


def reschedule(ref, when, now=None):
    now = now if now is not None else time.time()
    spec = parse_when(when, now)

    def fn(j):
        for k in ("at", "every", "cron"):
            j.pop(k, None)
        j.update(spec, when=when.strip(), next_run=first_run(spec, now), enabled=True)
    return _mutate(ref, fn)


def update(ref, **fields):
    return _mutate(ref, lambda j: j.update(fields))


def request_run(ref):
    return _mutate(ref, lambda j: j.update(run_now=True))


# ---------------- process helpers ----------------
def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"     # zombies are dead
    except OSError:
        return True


@contextlib.contextmanager
def busy():
    """Mark an interactive task as running: scheduled jobs wait (up to BUSY_DEFER_MAX)."""
    os.makedirs(busy_dir(), exist_ok=True)
    p = os.path.join(busy_dir(), str(os.getpid()))
    try:
        open(p, "w").close()
    except OSError:
        p = None
    try:
        yield
    finally:
        if p:
            with contextlib.suppress(OSError):
                os.remove(p)


def someone_busy():
    try:
        names = os.listdir(busy_dir())
    except FileNotFoundError:
        return False
    live = False
    for nme in names:
        if nme.isdigit() and _alive(int(nme)):
            live = True
        else:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(busy_dir(), nme))
    return live


def daemon_pid():
    try:
        pid = int(open(pid_file()).read().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


# ---------------- running ----------------
def _due(j, now):
    if j.get("running"):
        return False
    if j.get("run_now"):
        return True
    return bool(j["enabled"] and j.get("next_run") and j["next_run"] <= now)


def _advance(j, now):
    """Set next_run after a scheduled (not run-now) start."""
    if j["kind"] == "once":
        j["next_run"] = None
    elif j["kind"] == "interval":
        nxt = j["next_run"] + j["every"]
        j["next_run"] = nxt if nxt > now else now + j["every"]
    else:
        j["next_run"] = _ts(Cron(j["cron"]).next_after(_dt(now)))


def _reap(jobs, now):
    for j in jobs:
        r = j.get("running")
        if r and not _alive(r.get("pid")):
            j["running"] = None
            j.update(last_status="interrupted", last_finished=now)
        if (j["kind"] == "once" and j["enabled"] and j.get("next_run") and not j.get("run_now")
                and now - j["next_run"] > ONCE_GRACE):
            j.update(next_run=None, last_status="missed")


def _poll_children():
    for p in list(_CHILDREN):
        if p.poll() is not None:
            _CHILDREN.remove(p)


def job_command(job):
    cmd = [sys.executable, "-I", "-m", "finch", "-p", job["prompt"], "-C", job["workdir"]]
    if job.get("model"):
        cmd += ["-m", job["model"]]
    if job.get("soul") is not None:
        cmd += ["--soul", job["soul"] or "none"]
    if job.get("timebox"):
        cmd += ["--timebox", f"{float(job['timebox']):g}m"]
    if job.get("yes"):
        cmd.append("-y")
    return cmd


def _launch(job):
    env = dict(os.environ, FINCH_HOME=config.FINCH_HOME)
    return subprocess.Popen([sys.executable, "-I", "-m", "finch", "schedule", "exec", job["id"]],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, env=env, cwd=home())


def tick(now=None, launcher=None):
    """Start the next due job if no job is running anywhere. Returns the started job or None."""
    _poll_children()
    now = now if now is not None else time.time()
    with _locked() as jobs:
        _reap(jobs, now)
        if any(j.get("running") for j in jobs):
            return None
        due = sorted((j for j in jobs if _due(j, now)), key=lambda j: (not j.get("run_now"), j.get("next_run") or 0))
        if not due:
            return None
        j = due[0]
        late = now - (j.get("next_run") or now)
        if not j.get("run_now") and late < BUSY_DEFER_MAX and someone_busy():
            return None
        if not j.get("run_now") or (j.get("next_run") and j["next_run"] <= now):
            _advance(j, now)
        run_id = _dt(now).strftime("%Y%m%d-%H%M%S")
        os.makedirs(logs_dir(j["id"]), exist_ok=True)
        log = os.path.join(logs_dir(j["id"]), run_id + ".log")
        j.update(run_now=False, last_run=now, last_status="running", last_log=log,
                 running={"pid": os.getpid(), "since": now, "log": log, "late": max(0, round(late))})
        claimed = dict(j)
    try:
        proc = (launcher or _launch)(claimed)
    except Exception as e:
        with _locked() as jobs:
            j = find(claimed["id"], jobs)
            if j:
                j.update(running=None, last_status="failed", last_finished=time.time(),
                         last_summary=f"[scheduler] could not start the job: {e}")
        raise
    _CHILDREN.append(proc)
    with _locked() as jobs:
        j = find(claimed["id"], jobs)
        if j and j.get("running"):
            j["running"]["pid"] = proc.pid
    return claimed


def _summary(text, n=8):
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.startswith("# ")]
    return "\n".join(lines[-n:])[-1200:]


def execute(jid):
    """Supervisor: run the claimed job, enforce the hard timeout, write the log, record the result."""
    j = find(jid, load_jobs())
    if not j or not j.get("running"):
        return 2
    log = j["running"]["log"]
    started = time.time()
    status, rc = "failed", None
    with open(log, "w", encoding="utf-8") as out:
        out.write(f"# finch job '{j['name']}' ({j['id']})\n# schedule: {j['desc']}\n# workdir:  {j['workdir']}\n"
                  f"# started:  {_dt(started):%Y-%m-%d %H:%M:%S}"
                  + (f" ({fmt_dur(j['running']['late'])} late)" if j["running"].get("late", 0) >= 60 else "")
                  + f"\n# prompt:   {j['prompt']}\n\n")
        out.flush()
        if not os.path.isdir(j["workdir"]):
            out.write(f"[scheduler] working directory no longer exists: {j['workdir']}\n")
        else:
            env = dict(os.environ, FINCH_SCHEDULED_JOB=j["id"], COLUMNS="100", TERM="dumb", NO_COLOR="1")
            p = subprocess.Popen(job_command(j), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 cwd=j["workdir"], env=env, start_new_session=True)
            try:
                rc = p.wait(timeout=j.get("timeout") or DEFAULT_TIMEOUT)
                status = "ok" if rc == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timeout"
                for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(p.pid, sig)
                    try:
                        rc = p.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        continue
        took = time.time() - started
        out.write(f"\n# finished: {_dt(time.time()):%Y-%m-%d %H:%M:%S} · {status} · exit {rc} · {took:.0f}s\n")
    with open(log, encoding="utf-8", errors="replace") as f:
        summ = _summary(f.read())
    with _locked() as jobs:
        jj = find(jid, jobs)
        if jj:
            jj.update(running=None, last_status=status, last_exit=rc, last_finished=time.time(),
                      last_summary=summ, runs=jj.get("runs", 0) + 1, last_duration=round(took))
    _prune(jid)
    return 0 if status == "ok" else 1


def _prune(jid):
    with contextlib.suppress(OSError):
        logs = sorted(os.listdir(logs_dir(jid)))
        for f in logs[:-KEEP_LOGS]:
            os.remove(os.path.join(logs_dir(jid), f))


def run_now(ref, wait=True, launcher=None):
    """Queue a job to run immediately and try to start it. Returns (started_job | None, message)."""
    j = request_run(ref)
    started = tick(launcher=launcher)
    if not started or started["id"] != j["id"]:
        return None, (f"'{j['name']}' is queued: it starts when the running job finishes "
                      "(a Finch window or the daemon must be running)")
    if wait:
        proc = _CHILDREN[-1]
        proc.wait()
        _poll_children()
    return started, f"started '{j['name']}'"


def daemon(out=print, stop=None):
    """Foreground runner loop (`finch schedule daemon`)."""
    os.makedirs(home(), exist_ok=True)
    other = daemon_pid()
    if other and other != os.getpid():
        out(f"a scheduler daemon is already running (pid {other})")
        return 1
    with open(pid_file(), "w") as f:
        f.write(str(os.getpid()))
    flag = {"stop": False}

    def _term(*_):
        flag["stop"] = True
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, _term)
    out(f"[{_dt(time.time()):%Y-%m-%d %H:%M:%S}] finch scheduler daemon started (pid {os.getpid()})")
    try:
        while not flag["stop"] and not (stop and stop.is_set()):
            try:
                j = tick()
                if j:
                    out(f"[{_dt(time.time()):%Y-%m-%d %H:%M:%S}] started '{j['name']}' ({j['id']}) -> {j['last_log']}")
            except Exception as e:     # keep the daemon alive through a bad job file etc.
                out(f"[{_dt(time.time()):%Y-%m-%d %H:%M:%S}] tick error: {e}")
            for _ in range(TICK * 2):
                if flag["stop"] or (stop and stop.is_set()):
                    break
                time.sleep(0.5)
                _poll_children()
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(OSError):
            if open(pid_file()).read().strip() == str(os.getpid()):
                os.remove(pid_file())
        out(f"[{_dt(time.time()):%Y-%m-%d %H:%M:%S}] daemon stopped")
    return 0


def start_daemon():
    pid = daemon_pid()
    if pid:
        return pid, f"daemon already running (pid {pid})"
    os.makedirs(home(), exist_ok=True)
    env = dict(os.environ, FINCH_HOME=config.FINCH_HOME)
    with open(daemon_log(), "a") as lf:
        subprocess.Popen([sys.executable, "-I", "-m", "finch", "schedule", "daemon"], stdin=subprocess.DEVNULL,
                         stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, env=env, cwd=home())
    for _ in range(40):
        time.sleep(0.1)
        pid = daemon_pid()
        if pid:
            return pid, f"daemon started (pid {pid}) · log: {daemon_log()}"
    return None, f"daemon did not start; see {daemon_log()}"


def stop_daemon():
    pid = daemon_pid()
    if not pid:
        return "daemon is not running"
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.1)
        if not _alive(pid):
            break
    return f"daemon stopped (pid {pid}); a job that was already running finishes on its own"


# ---------------- presentation ----------------
STATUS_MARK = {"ok": "✓", "failed": "✗", "timeout": "⏱", "missed": "–", "interrupted": "!", "running": "…"}


def state_text(j, now=None):
    """Short state for lists: 'running 2m' / 'next in 3h' / 'paused' / 'done'."""
    now = now or time.time()
    if j.get("running"):
        return f"running {fmt_dur(max(60, now - j['running']['since']))}"
    if j.get("run_now"):
        return "queued to run now"
    if not j["enabled"]:
        return "paused"
    if not j.get("next_run"):
        return "done" if j.get("last_status") != "missed" else "missed"
    return "next " + fmt_time(j["next_run"], now)


def last_text(j, now=None):
    if not j.get("last_finished") or j.get("running"):
        return "never run" if not j.get("runs") else ""
    return f"{STATUS_MARK.get(j['last_status'], '?')} {j['last_status']} {fmt_ago(j['last_finished'], now)}"


def tail(path, n=60):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError as e:
        return f"(cannot read {path}: {e})"


def runner_text():
    pid = daemon_pid()
    return f"daemon running (pid {pid})" if pid else "no daemon: jobs run only while a Finch window is open"


# ---------------- `finch schedule ...` ----------------
def cli_main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="finch schedule", description="One-off and recurring Finch jobs.")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add", help="add a job: finch schedule add 'every day at 9am' 'summarise ~/notes'")
    a.add_argument("when")
    a.add_argument("prompt")
    a.add_argument("-C", "--cwd", default=os.getcwd())
    a.add_argument("-m", "--model")
    a.add_argument("--soul")
    a.add_argument("--name")
    a.add_argument("-y", "--yes", action="store_true", help="auto-approve risky actions (hard ring stays on)")
    a.add_argument("--timebox", type=float, default=DEFAULT_TIMEBOX, help="soft budget in minutes (0 = none)")
    a.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT / 60, help="hard kill after N minutes")
    sub.add_parser("list", help="list jobs")
    for c, h in (("run", "run a job now"), ("pause", "pause a job"), ("resume", "resume a job"),
                 ("rm", "delete a job"), ("log", "show the last log of a job"), ("show", "show a job"),
                 ("exec", argparse.SUPPRESS)):
        p = sub.add_parser(c, help=h)
        p.add_argument("job")
        if c == "run":
            p.add_argument("--no-wait", action="store_true")
    w = sub.add_parser("when", help="change a job's schedule: finch schedule when <job> 'weekdays at 8'")
    w.add_argument("job")
    w.add_argument("schedule")
    t = sub.add_parser("test", help="check a schedule phrase and show the next runs")
    t.add_argument("schedule")
    for c, h in (("daemon", "run the scheduler in the foreground"), ("start", "start the daemon in the background"),
                 ("stop", "stop the background daemon"), ("status", "daemon status"),
                 ("tick", "start the next due job and exit (for system cron)")):
        sub.add_parser(c, help=h)
    args = ap.parse_args(argv)
    try:
        return _cli(args, ap)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _cli(args, ap):
    c = args.cmd
    if c is None:
        ap.print_help()
        return 0
    if c == "add":
        j = add_job(args.when, args.prompt, args.cwd, name=args.name, model=args.model, soul=args.soul,
                    yes=args.yes, timebox=args.timebox or None, timeout=args.timeout * 60)
        print(f"scheduled '{j['name']}' ({j['id']}) · {j['desc']} · next {fmt_time(j['next_run'])}")
        if not daemon_pid():
            print("note: " + runner_text() + " · start one with: finch schedule start")
        return 0
    if c == "list":
        jobs = load_jobs()
        if not jobs:
            print("no scheduled jobs")
        for i, j in enumerate(jobs, 1):
            print(f"{i:>2}. {j['name']:<24} {j['desc']:<28} {state_text(j):<22} {last_text(j):<18}"
                  + (" auto-approve" if j.get("yes") else ""))
        print("\n" + runner_text())
        return 0
    if c == "show":
        j = find(args.job)
        if not j:
            raise ValueError(f"no job '{args.job}'")
        print(json.dumps(j, indent=1))
        print("next runs: " + ", ".join(fmt_time(t) for t in upcoming(j)) if j.get("next_run") else "")
        return 0
    if c == "test":
        spec = parse_when(args.schedule)
        print(spec["desc"] + " · next: " + ", ".join(fmt_time(t) for t in upcoming(spec)))
        return 0
    if c == "run":
        j, msg = run_now(args.job, wait=not args.no_wait)
        print(msg)
        if j and not args.no_wait:
            jj = find(j["id"])
            print(f"{STATUS_MARK.get(jj['last_status'], '?')} {jj['last_status']} · log: {jj['last_log']}\n")
            print(jj.get("last_summary", ""))
        return 0
    if c in ("pause", "resume"):
        j = set_enabled(args.job, c == "resume")
        print(f"{'resumed' if j['enabled'] else 'paused'} '{j['name']}' · {state_text(j)}")
        return 0
    if c == "rm":
        print(f"deleted '{remove(args.job)['name']}'")
        return 0
    if c == "when":
        j = reschedule(args.job, args.schedule)
        print(f"'{j['name']}' · {j['desc']} · next {fmt_time(j['next_run'])}")
        return 0
    if c == "log":
        j = find(args.job)
        if not j:
            raise ValueError(f"no job '{args.job}'")
        print(tail(j["last_log"], 200) if j.get("last_log") else "no runs yet")
        return 0
    if c == "exec":
        return execute(args.job)
    if c == "daemon":
        return daemon(out=lambda m: print(m, flush=True))
    if c == "start":
        print(start_daemon()[1])
        return 0
    if c == "stop":
        print(stop_daemon())
        return 0
    if c == "status":
        print(runner_text())
        return 0
    if c == "tick":
        j = tick()
        print(f"started '{j['name']}'" if j else "nothing due")
        return 0
    return 0
