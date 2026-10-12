#!/usr/bin/env python3
"""
Builds a day's tasks and DRAFT timesheet on a Frappe / ERPNext site from GitHub
activity and Claude Code conversations, and edits them from the terminal.

Uses the Frappe REST API (no browser), signing in with your site email and the
password kept in the OS credential store (Windows Credential Manager / macOS
Keychain). GitHub is read and written through the `gh` CLI. Every write is shown
for confirmation first, and a timesheet is submitted only when you ask for it
(`submit`, or `create --submit`).

    timesheet config                      guided setup
    timesheet create [--date D]           build tasks + draft timesheet (default: yesterday)
    timesheet show   [--date D]           list the day's rows, numbered
    timesheet update --row N              change a row (prompts, or --from/--to/--description/--type/--task)
    timesheet start "text" / finish       timed work with a tracking issue

Run `timesheet <command> -h` for the flags of each command; README.md has the rest.
"""
from __future__ import annotations  # keeps `str | None` hints working on Python 3.9 (macOS default)

import argparse
import collections
import datetime as dt
import getpass
import html
import http.cookiejar
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
STATE_FILE = HERE / ".state.json"
LOG_FILE = HERE / "timesheet.log"
KEYCHAIN_SERVICE = "timesheet-autofill"
JOB_NAME = "Timesheet Autofill"

# In an issue body: marks an issue this tool opened to track a piece of work.
MARKER = "<!-- timesheet-autofill -->"
# First words of every prompt sent to `claude -p`, so those sessions are not read back as work.
PROMPT_MARK = "[timesheet-autofill]"
# In a row description: the row has no tracking issue yet (`timesheet issues` creates it).
NO_ISSUE = "[no issue yet]"
TRACK = "Tracking issue: "
MIN_ROW = dt.timedelta(minutes=5)


class TSError(Exception):
    """str(e) is the human-readable message (shown in the popup); `detail` is the
    technical context (request, HTTP status) that only goes to the log."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class NetworkNotReady(TSError):
    pass


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- settings

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: tolerate a Windows BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(ENV_FILE)

# key: (default, kind, what it is). `config` validates by kind; .env holds only what differs.
SETTINGS = {
    "TS_URL": ("", "url", "Site address, e.g. https://projects.yourcompany.com"),
    "TS_USER": ("", "email", "Site login email"),
    "TASK_PRIORITY": ("Low", "text", "Priority of created tasks"),
    "TASK_MODULE": ("Customised", "text", "Module of created tasks"),
    "DEFAULT_ACTIVITY": ("Customization / Development", "text", "Activity type when the kind of work is unknown"),
    "WORK_DAYS": ("Mon,Tue,Wed,Thu,Fri", "days", "Days you work"),
    "DAY_START": ("09:30", "time", "When the working day starts, 24h"),
    "LUNCH": ("13:00-14:00", "window", "Lunch window, 24h"),
    "IDLE_MINUTES": ("15", "int", "A pause this long ends a block of conversation work"),
    "UTC_OFFSET": ("+05:30", "offset", "Time zone of the site and your day (IST is +05:30)"),
    "POLISH": ("false", "bool", "Polish typed text through Claude Code by default"),
    "POLISH_HINT": ("true", "bool", "Suggest --polish when you type text without it"),
    "NOTIFY": ("true", "bool", "Popups from the scheduled job"),
    "SCHEDULE": ("off", "schedule", "Daily job: off, remind (popup) or auto (creates yesterday's draft)"),
}
EXPECT = {
    "url": "a site address like https://projects.yourcompany.com",
    "email": "an email address",
    "text": "some text",
    "days": "day names like Mon,Tue or numbers 1-7",
    "time": "24h HH:MM, e.g. 09:30",
    "window": "two 24h times, e.g. 13:00-14:00",
    "int": "a whole number above 0",
    "offset": "an offset from UTC like +05:30",
    "bool": "true or false",
    "schedule": "off, remind or auto",
}
GUIDED = ("TS_URL", "TS_USER", "WORK_DAYS", "DAY_START", "LUNCH", "SCHEDULE")
DAY_NAMES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


def cfg(key: str) -> str:
    return os.environ.get(key, "").strip() or SETTINGS[key][0]


def flag(key: str) -> bool:
    return cfg(key).lower() == "true"


def site_url(value: str) -> str:
    """'projects.example.com' or a pasted page URL -> 'https://projects.example.com'."""
    value = value.strip()
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value if "://" in value else f"https://{value}")
    return f"{parsed.scheme}://{parsed.netloc}"


def parse_days(value: str) -> set[int]:
    """'Mon,Tue' or '1,2' (ISO weekday numbers, 1=Mon) -> {1, 2}."""
    days = set()
    for part in value.split(","):
        part = part.strip().lower()
        if part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
        elif part[:3] in DAY_NAMES:
            days.add(DAY_NAMES[part[:3]])
        elif part:
            raise ValueError(part)
    if not days:
        raise ValueError("no days")
    return days


def parse_hm(value: str) -> dt.time:
    """'10:30' / '9.30' / '11' -> time (24h)."""
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?(?::(\d{2}))?", value.strip())
    if not m or int(m[1]) > 23 or int(m[2] or 0) > 59 or int(m[3] or 0) > 59:
        raise TSError(f"'{value}' is not a time - use 24h HH:MM, e.g. 09:30 or 19:30")
    return dt.time(int(m[1]), int(m[2] or 0), int(m[3] or 0))


def parse_window(value: str) -> tuple[dt.time, dt.time]:
    a, b = (parse_hm(part) for part in value.split("-"))
    if a >= b:
        raise ValueError(value)
    return a, b


def parse_offset(value: str) -> dt.timedelta:
    m = re.fullmatch(r"([+-])(\d{1,2}):(\d{2})", value.strip())
    if not m:
        raise ValueError(value)
    return (-1 if m[1] == "-" else 1) * dt.timedelta(hours=int(m[2]), minutes=int(m[3]))


def clean(key: str, value: str) -> str:
    """Validated, normalised value for a setting; ValueError says what is expected."""
    if key not in SETTINGS:
        raise ValueError(f"unknown setting {key} - see: timesheet config list")
    kind, v = SETTINGS[key][1], value.strip()
    try:
        if kind == "url":
            if not re.match(r"^(https?://)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?(/.*)?$", v):
                raise ValueError(v)
            return site_url(v)
        if kind == "email" and not re.match(r"^[^@\s]+@[^@\s]+$", v):
            raise ValueError(v)
        if kind == "time":
            return f"{parse_hm(v):%H:%M}"
        if kind == "window":
            a, b = parse_window(v)
            return f"{a:%H:%M}-{b:%H:%M}"
        if kind == "days":
            parse_days(v)
        if kind == "offset":
            parse_offset(v)
        if kind == "int" and not (v.isdigit() and int(v) > 0):
            raise ValueError(v)
        if kind in ("bool", "schedule"):
            v = v.lower()
            if v not in (("true", "false") if kind == "bool" else ("off", "remind", "auto")):
                raise ValueError(v)
    except (ValueError, TSError):
        raise ValueError(f"{key}: expected {EXPECT[kind]}") from None
    return v


# ---------------------------------------------------------------- repos and their projects
#
# Nothing here is a setting. Repos are found in the day's GitHub activity and in the folders
# your conversations ran in; each repo's project and task wording are learned from the tasks
# you already have on the site (see learn), or asked once. Kept in .state.json:
#   "repos":   {"owner/name": {"project": "Strategic Gears", "head": "SG Network - Internal Console"}}
#   "folders": {"c:/some/folder": "Project"}   for conversations in a folder that is no repo
# A repo is always its lower-case "owner/name".

def known() -> dict:
    return load_state().get("repos", {})


def project_of(repo: str) -> str:
    return known().get(repo or "", {}).get("project", "")


def head_of(repo: str) -> str:
    """Start of the repo's task subjects: 'SG Network - Internal Console'."""
    return known().get(repo or "", {}).get("head", "")


def label_of(repo: str) -> str:
    """'Internal Console' - how task subjects and rows name the repo."""
    return head_of(repo).split(" - ", 1)[-1] or (repo or "").split("/")[-1]


def short(repo: str) -> str:
    """'console' - the repo in running text ("closed the console issues")."""
    return label_of(repo).split()[-1].lower() if repo else ""


def remember(repo: str, project: str, head: str = "") -> None:
    state = load_state()
    state.setdefault("repos", {})[repo] = {"project": project, "head": head or f"{project} - {repo.split('/')[-1]}"}
    save_state(state)


def repo_arg(name: str | None) -> str:
    """--repo value -> repo: 'owner/name', or the short name of one already seen ('console')."""
    name = (name or "").strip().lower()
    if not name or "/" in name:
        return name
    hits = [r for r in known() if name in (short(r), r.split("/")[-1])]
    if len(hits) != 1:
        raise TSError(f"--repo {name}: use owner/name" + (f", or one of: {', '.join(sorted(map(short, known())))}"
                                                           if known() else ""))
    return hits[0]


# ---------------------------------------------------------------- time

def offset() -> dt.timedelta:
    return parse_offset(cfg("UTC_OFFSET"))


def now() -> dt.datetime:
    """Current time in the site's zone, naive, to the second."""
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + offset()).replace(microsecond=0)


def local(iso: str) -> dt.datetime:
    """'2026-10-06T07:04:42.400Z' (UTC, as GitHub and Claude Code write it) -> naive site time."""
    return dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S") + offset()


def utc_iso(t: dt.datetime) -> str:
    return (t - offset()).strftime("%Y-%m-%dT%H:%M:%SZ")


def ptime(value: str) -> dt.datetime:
    return dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """A timesheet holds 00:00:00 to 23:59:59 of its date and nothing after."""
    lo = dt.datetime.combine(day, dt.time.min)
    return lo, lo + dt.timedelta(days=1, seconds=-1)


def at(day: dt.date, value: str) -> dt.datetime:
    return dt.datetime.combine(day, parse_hm(value))


def hm(t: dt.datetime) -> str:
    return f"{t:%H:%M}"


def fmt_day(day: dt.date) -> str:
    return f"{day.day} {day:%b %Y}"


def hours(row: dict) -> float:
    return (row["to"] - row["frm"]).total_seconds() / 3600


# ---------------------------------------------------------------- site API

def plain_text(value: str) -> str:
    """Frappe messages/descriptions are HTML: '<b>24-09-2026</b><br>' -> '24-09-2026'."""
    value = " ".join(value.split())  # raw newlines in HTML are just spaces
    value = re.sub(r"<br\s*/?>|</p>|</div>", "\n", value, flags=re.I)
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    lines = (" ".join(line.split()) for line in value.splitlines())
    return "\n".join(line for line in lines if line)


# Session cookies (sid) are kept here after the password login.
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def api(method: str, path: str, params: dict | None = None, body: dict | None = None):
    url = cfg("TS_URL") + urllib.parse.quote(path, safe="/")  # doctype/doc names contain spaces
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
        )
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise TSError(frappe_error(e.read()), f"{method} {path} -> HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise TSError(f"Could not connect to the site ({e.reason})", f"{method} {path}") from None


def frappe_error(raw: bytes) -> str:
    """Pull the human-readable message out of a Frappe error response."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw[:300].decode(errors="replace")
    msgs = []
    for m in json.loads(payload.get("_server_messages", "[]") or "[]"):
        try:
            msgs.append(json.loads(m).get("message", m))
        except (ValueError, AttributeError):
            msgs.append(str(m))
    if msgs:
        return plain_text("\n".join(map(str, msgs)))
    exc = payload.get("exception") or payload.get("exc_type") or payload.get("message")
    return plain_text(str(exc))[:300]


def stored_password() -> str:
    """Password from the OS credential store: `keyring` if installed (Windows Credential
    Manager / macOS Keychain / Linux Secret Service), else the macOS `security` tool."""
    user = cfg("TS_USER")
    try:
        import keyring
        if pwd := keyring.get_password(KEYCHAIN_SERVICE, user):
            return pwd
    except ImportError:
        pass
    if sys.platform == "darwin":
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.rstrip("\n")
    raise TSError(f"no saved password for {user} - save it with: timesheet config set-secret")


def save_password(pwd: str) -> None:
    user = cfg("TS_USER")
    try:
        import keyring
        keyring.set_password(KEYCHAIN_SERVICE, user, pwd)
    except ImportError:
        if sys.platform != "darwin":
            raise TSError("the 'keyring' package is needed to store the password: "
                          f"{sys.executable} -m pip install --user keyring") from None
        # ponytail: the password is briefly visible in the process list here; install keyring to avoid it
        subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w", pwd],
                       check=True)


def login() -> None:
    """Password login; the session cookie is stored in OPENER for later calls."""
    api("POST", "/api/method/login", body={"usr": cfg("TS_USER"), "pwd": stored_password()})


def get_list(doctype: str, filters: list, fields: list, parent: str | None = None,
             order_by: str | None = None, limit: int = 50) -> list:
    params = {"doctype": doctype, "filters": filters, "fields": fields, "limit_page_length": limit}
    if parent:
        params["parent"] = parent
    if order_by:
        params["order_by"] = order_by
    return api("GET", "/api/method/frappe.client.get_list", params=params).get("message", [])


def get_employee() -> dict:
    user = api("GET", "/api/method/frappe.auth.get_logged_user")["message"]
    rows = get_list(
        "Employee",
        [["user_id", "=", user], ["status", "=", "Active"]],
        ["name", "employee_name", "company", "holiday_list"],
    )
    if not rows:
        raise TSError(f"No active Employee linked to user {user}")
    return rows[0]


def wait_for_network(timeout: int = 600, interval: int = 15) -> None:
    """Right after the laptop wakes, Wi-Fi may not be back yet - wait for DNS instead of failing."""
    host = urllib.parse.urlparse(cfg("TS_URL")).hostname
    deadline = time.monotonic() + timeout
    logged = False
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise NetworkNotReady(f"No network: could not reach {host}") from None
            if not logged:
                log("network not ready, waiting...")
                logged = True
            time.sleep(interval)


def connect(network_wait: int = 30) -> dict:
    """Checks the settings, signs in and returns your Employee record."""
    for key in ("TS_URL", "TS_USER"):
        if not cfg(key):
            raise TSError(f"{key} is not set - run: timesheet config")
    wait_for_network(timeout=network_wait)
    login()
    return get_employee()


def skip_reason(emp: dict, day: dt.date) -> str | None:
    """Why no timesheet is due for `day` (non-work day, holiday, leave), else None."""
    if day.isoweekday() not in parse_days(cfg("WORK_DAYS")):
        return f"{day:%A} is not in WORK_DAYS"
    d = day.isoformat()
    try:
        holiday_list = emp.get("holiday_list")
        if not holiday_list:
            company = api("GET", f"/api/resource/Company/{emp['company']}")
            holiday_list = company["data"].get("default_holiday_list")
        if holiday_list:
            rows = get_list("Holiday", [["parent", "=", holiday_list], ["holiday_date", "=", d]],
                            ["description"], parent="Holiday List")
            if rows:
                return f"holiday: {plain_text(rows[0].get('description') or '') or holiday_list}"
        rows = get_list("Leave Application",
                        [["employee", "=", emp["name"]], ["from_date", "<=", d], ["to_date", ">=", d],
                         ["docstatus", "!=", 2], ["status", "not in", ["Rejected", "Cancelled"]]], ["name"])
        if rows:
            return f"on leave ({rows[0]['name']})"
    except TSError as e:
        # Employees sometimes can't read the Holiday List or leave records; fall back to WORK_DAYS only.
        log(f"warning: could not check holidays and leave ({e}); using WORK_DAYS only")
    return None


# ---------------------------------------------------------------- state, popups

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except OSError as e:
        log(f"warning: could not save {STATE_FILE.name}: {e}")


def popup(title: str, body: str, url: str, error: bool = False) -> None:
    """Popup with an "Open" button. Unlike notifications, it can't be silently hidden by
    notification settings / Focus; it stays until dismissed (max 8h).
    Runs as a separate process so this script (and the scheduler's next run) isn't blocked."""
    if sys.platform == "darwin":
        show = (f"set r to display alert {json.dumps(title)} message {json.dumps(body)}"
                f"{' as critical' if error else ''} buttons {{\"OK\", \"Open\"}} "
                f"default button \"Open\" giving up after 28800")
        open_url = f"if button returned of r is \"Open\" then open location {json.dumps(url)}"
        subprocess.Popen(["osascript", "-e", show, "-e", open_url], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "win32":
        icon = 0x10 if error else 0x40  # error / information
        code = (
            "import ctypes, webbrowser\n"
            # MB_YESNO | icon | MB_TOPMOST; 6 = "Yes"
            f"if ctypes.windll.user32.MessageBoxW(0, {body + chr(10) * 2 + 'Open the site?'!r}, {title!r}, "
            f"{0x4 | icon | 0x40000}) == 6:\n"
            f"    webbrowser.open({url!r})\n"
        )
        subprocess.Popen([sys.executable, "-c", code],
                         creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------- confirmation, polishing

TYPED = []  # set when the user typed free text that was stored unpolished (drives the --polish hint)


def need_tty() -> None:
    if not sys.stdin or not sys.stdin.isatty():
        raise TSError("this needs a terminal to ask you - pass the values as flags, and --yes to skip the y/N")


def confirm(question: str, args) -> bool:
    if args.dry_run:
        print("dry-run: nothing written.")
        return False
    if args.yes:
        return True
    need_tty()
    if TYPED and flag("POLISH_HINT") and not args.raw and shutil.which("claude"):
        print(f"Tip: to tidy the wording first, answer n and run: timesheet {shlex.join(sys.argv[1:])} --polish")
    return input(f"{question} [y/N]: ").strip().lower().startswith("y")


def run_claude(prompt: str, timeout: int = 60, model: str = "haiku") -> str:
    """Output of `claude -p` (Claude Code, using your own login); '' if missing, slow or failing."""
    exe = shutil.which("claude")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "-p", "--model", model], input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(HERE))
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


FACT = re.compile(r"#?\d[\d:.,]*|\S*[/_]\S*")  # numbers, references, times, branch-like names


def polish(text: str) -> str:
    """Rewrites typed text into timesheet style; keeps your text if Claude adds any fact."""
    out = run_claude(
        f"{PROMPT_MARK} Rewrite this timesheet note in plain past tense, starting with the action "
        "(for example 'Reviewed and merged...', 'Tested...'). Say what was done, where, and the outcome. "
        "No 'I', no filler, at most two sentences. Do not add any number, reference, time or name that is "
        f"not in the note. Reply with the rewritten note only.\n\nNote: {text}", timeout=30)
    out = " ".join(out.split())
    if not out:
        print("polish: Claude Code did not answer - keeping your text.")
        return text
    added = [f for f in FACT.findall(out) if f.strip(".,") not in text]
    if added:
        print(f"polish: the rewrite added {', '.join(added)} - keeping your text.")
        return text
    return out


def typed(text: str, args) -> str:
    """Free text you typed: polished when asked for, otherwise stored exactly as typed."""
    text = text.strip()
    if args.raw or not (args.polish or flag("POLISH")):
        TYPED.append(text)
        return text
    new = polish(text)
    if new != text:
        print(f"polished: {text}\n      ->  {new}")
    return new


_LEARNED, _PROJECTS = False, None


def learn() -> None:
    """Repo -> project and task wording, read from the tasks you already have on the site: a task
    'SG Network - Internal Console - PR Review & Merge - 7 Oct 2026' in project Strategic Gears whose
    description links PRs of one repo says where that repo's work goes and how it is named."""
    global _LEARNED
    if _LEARNED:
        return
    _LEARNED = True
    suffixes = "|".join(re.escape(s) for s in {s for s, _ in KINDS.values()})
    try:
        tasks = get_list("Task", [["owner", "=", cfg("TS_USER")], ["description", "like", "%github.com/%"]],
                         ["subject", "project", "description"], order_by="creation desc", limit=300)
    except TSError as e:
        log(f"warning: could not read your earlier tasks to learn each repo's project ({e})")
        return
    state = load_state()
    for t in reversed(tasks):  # oldest first, so the latest wording wins
        names = {n.lower() for n in re.findall(r"github\.com/([\w.-]+/[\w.-]+)/(?:pull|issues)/",
                                               t.get("description") or "")}
        m = re.match(rf"(.+? - .+?) - (?:{suffixes}) - \d", t["subject"])
        if len(names) == 1 and m and t.get("project"):
            state.setdefault("repos", {})[names.pop()] = {"project": t["project"], "head": m[1]}
    save_state(state)


_ASKED = {}  # what -> answer, so one run never asks the same thing twice


def ask_project(what: str, args, guess: str = "") -> str:
    """Asks which site project something belongs to; '' when it can't ask or you skip."""
    global _PROJECTS
    if args is None or args.yes or args.scheduled or not sys.stdin or not sys.stdin.isatty():
        return ""
    if what in _ASKED:
        return _ASKED[what]
    if _PROJECTS is None:
        _PROJECTS = [p["name"] for p in get_list("Project", [["status", "=", "Open"]], ["name"],
                                                 order_by="name asc", limit=500)]
    print(f"\nWhich project is {what} for?")
    cells = [f"{i:>3}. {name[:32]:<32}" for i, name in enumerate(_PROJECTS, 1)]
    across = max(1, (shutil.get_terminal_size().columns - 1) // 38)
    for i in range(0, len(cells), across):
        print(" ".join(cells[i:i + across]).rstrip())
    hint = f"Enter = {guess}, - leaves it out" if guess else "Enter leaves it out"
    try:
        answer = input(f"number or part of the name ({hint}): ").strip().lower()
    except EOFError:
        answer = "-"
    hits = [p for p in _PROJECTS if answer and answer in p.lower()]
    exact = [p for p in hits if p.lower() == answer]
    if answer.isdigit() and 0 < int(answer) <= len(_PROJECTS):
        _ASKED[what] = _PROJECTS[int(answer) - 1]
    elif not answer:
        _ASKED[what] = guess
    else:
        _ASKED[what] = exact[0] if exact else hits[0] if len(hits) == 1 else ""
        if len(hits) > 1 and not exact:
            print(f"  '{answer}' matches several ({', '.join(hits[:5])}) - left out for now.")
    return _ASKED[what]


def resolve(repo: str, args) -> str:
    """Project of a repo: already known, else learned from your tasks, else asked once and remembered
    (suggesting the project of your other repos in the same GitHub organisation)."""
    if not repo:
        return ""
    if not project_of(repo):
        learn()
    if not project_of(repo):
        same_org = {seen["project"] for name, seen in known().items() if name.split("/")[0] == repo.split("/")[0]}
        if project := ask_project(f"the repo {repo}", args, same_org.pop() if len(same_org) == 1 else ""):
            remember(repo, project)
    return project_of(repo)


def resolve_folder(cwd: str, args) -> str:
    """Project for work in a folder that is no repo: asked once and remembered."""
    key = (cwd or "").replace("\\", "/").lower()
    state = load_state()
    if key and key not in state.get("folders", {}) and (project := ask_project(f"work in the folder {cwd}", args)):
        state = load_state()
        state.setdefault("folders", {})[key] = project
        save_state(state)
    return state.get("folders", {}).get(key, "")


# ---------------------------------------------------------------- kinds of work

# kind: (task suffix, activity type). The first four come from GitHub events; the rest
# from `start`, notes and conversations.
KINDS = {
    "own": ("Own fixes raised and merged", "Customization / Development"),
    "review": ("PR Review & Merge", "Code Review"),
    "closed": ("GitHub Issues Resolved", "Issue handling"),
    "raised": ("UAT Testing", "Testing"),
    "development": ("Development", "Customization / Development"),
    "testing": ("UAT Testing", "Testing"),
    "research": ("Research", "Research"),
    "issues": ("GitHub Issues Resolved", "Issue handling"),
}
WORK_KINDS = ("development", "testing", "research", "review", "issues")
_ACTIVITIES = None


def activity(kind: str) -> str:
    """Activity type for a kind of work, or for a name typed as it is on the site."""
    global _ACTIVITIES
    if _ACTIVITIES is None:
        try:
            _ACTIVITIES = {r["name"].lower(): r["name"] for r in get_list("Activity Type", [], ["name"], limit=500)}
        except TSError:
            _ACTIVITIES = {}  # not readable: trust the names and let the site validate
    name = KINDS[kind.lower()][1] if kind.lower() in KINDS else kind
    if not _ACTIVITIES or name.lower() in _ACTIVITIES:
        return _ACTIVITIES.get(name.lower(), name) if _ACTIVITIES else name
    if kind.lower() in KINDS:
        return cfg("DEFAULT_ACTIVITY")  # e.g. no "Research" activity type on this site
    raise TSError(f"'{kind}' is not an activity type on the site - use one of: "
                  f"{', '.join(sorted(_ACTIVITIES.values()))}, or {', '.join(WORK_KINDS)}")


def guess_kind(text: str) -> str:
    t = text.lower()
    for kind, words in (("testing", ("test", "uat", "smoke", "qa")), ("review", ("review",)),
                        ("research", ("research", "investigat", "explor", "spike", "learn", "study")),
                        ("issues", ("issue", "triage"))):
        if any(w in t for w in words):
            return kind
    return "development"


def subject(kind: str, day: dt.date, repo: str = "", title: str | None = None,
            window: str | None = None, label: bool = True, project: str = "") -> str:
    """'SG Network - Internal Console - PR Review & Merge - 7 Oct 2026' for `repo`, with a
    '(09:30-11:00)' window for timed and conversation work. Without `label` (testing is one task
    per project) the repo's name is left out; work in no repo starts with its project's name."""
    suffix = title or (KINDS[kind][0] if kind in KINDS else kind)
    head = head_of(repo) if label else head_of(repo).split(" - ")[0]
    if not head and (project := project or project_of(repo)):  # no repo: named like that project's other tasks
        head = next((seen["head"].split(" - ")[0] for seen in known().values() if seen["project"] == project), project)
    # The site allows a task subject 140 characters; a long summary gives way, never the date or window.
    room = 140 - len(" - ".join(p for p in (head, "", fmt_day(day)) if p is not None)) - len(f" ({window})" if window else "")
    parts = [head, suffix if len(suffix) <= room else suffix[:max(room - 3, 10)].rstrip() + "...", fmt_day(day)]
    text = " - ".join(p for p in parts if p)
    return f"{text} ({window})" if window else text


# ---------------------------------------------------------------- row building (pure)

def subtract(start: dt.datetime, end: dt.datetime, busy: list) -> list:
    """The free pieces of [start, end) once the busy intervals are taken out."""
    out = []
    for b0, b1 in sorted(busy):
        if b1 <= start or b0 >= end:
            continue
        if b0 > start:
            out.append((start, b0))
        start = max(start, b1)
    if start < end:
        out.append((start, end))
    return out


def split_blocks(stamps: list, idle: dt.timedelta) -> list:
    """Sorted timestamps -> [(start, end)] blocks; a pause longer than `idle` ends a block."""
    blocks = []
    for t in stamps:
        if blocks and t - blocks[-1][1] <= idle:
            blocks[-1][1] = t
        else:
            blocks.append([t, t])
    return [tuple(b) for b in blocks]


def group_key(e: dict) -> tuple:
    # Testing is one task per project for the day, so issues raised on its repos share a row.
    return (e.get("project", ""), "raised") if e["kind"] == "raised" else (e["repo"], e["kind"])


def build_rows(events: list, busy: list, day: dt.date, day_start: dt.timedelta, lunch_end: dt.timedelta,
               min_row: dt.timedelta = MIN_ROW) -> list:
    """Point-in-time events (a PR merged, an issue closed) -> [(start, end, [events])].

    Consecutive events of the same repo and kind share a row. A row runs from the previous
    row's end - or day start, or lunch end, when the work began after them - to its last
    event, is trimmed around the busy (fixed) intervals, and never leaves `day`."""
    lo, hi = day_bounds(day)
    anchors = [lo + day_start, lo + lunch_end]
    cuts = anchors + [b[0] for b in busy]
    groups = []
    for e in sorted(events, key=lambda e: e["t"]):
        g = groups[-1] if groups else None
        if g and group_key(g[-1]) == group_key(e) and not any(g[-1]["t"] < c <= e["t"] for c in cuts):
            g.append(e)
        else:
            groups.append([e])
    rows, cursor = [], lo
    for g in groups:
        first, last = g[0]["t"], g[-1]["t"]
        start = cursor
        for a in anchors:
            if start < a <= first:
                start = a
        free = subtract(start, max(last, start + min_row), busy)
        if free:
            start, end = free[-1]  # the piece that ends at the event
        else:  # the event happened inside a fixed row: put it straight after that row
            start = last
            while True:
                inside = [b1 for b0, b1 in busy if b0 <= start < b1]
                if not inside:
                    break
                start = max(inside)
            start, end = subtract(start, start + min_row, busy)[0]
        end = min(end, hi)
        if start >= end:
            continue
        rows.append((start, end, g))
        cursor = max(cursor, end)
    return rows


# ---------------------------------------------------------------- GitHub

def gh(*args: str) -> str:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise TSError("the GitHub CLI (gh) is not installed - get it from https://cli.github.com, "
                      "then run: gh auth login") from None
    if r.returncode:
        raise TSError(f"gh {args[0]} failed: {r.stderr.strip()[:300]}", "gh " + " ".join(args)[:200])
    return r.stdout.strip()


_ME = None


def gh_user() -> str:
    global _ME
    _ME = _ME or gh("api", "user", "--jq", ".login")
    return _ME


GQL = """query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){
pageInfo{hasNextPage endCursor} nodes{__typename
... on Issue{number title url body createdAt closedAt author{login} repository{nameWithOwner}
  timelineItems(last:1,itemTypes:[CLOSED_EVENT]){nodes{... on ClosedEvent{actor{login}}}}}
... on PullRequest{number title url mergedAt headRefName baseRefName additions deletions
  author{login} mergedBy{login} repository{nameWithOwner}}}}}"""


def gh_search(query: str) -> list:
    nodes, after = [], None
    while True:
        args = ["api", "graphql", "-f", f"query={GQL}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        page = json.loads(gh(*args))["data"]["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def who(node: dict | None) -> str:
    return (node or {}).get("login") or ""


def body_field(body: str, name: str) -> str:
    m = re.search(rf"^{name}: *(.+)$", body or "", flags=re.M)
    return m[1].strip() if m else ""


def issue_body(kind: str, text: str, start: dt.datetime, end: dt.datetime | None = None, task: str = "") -> str:
    lines = [MARKER, "Tracking issue for work logged in the timesheet.", "",
             f"Work: {text}", f"Kind: {kind}", f"Started: {start:%Y-%m-%d %H:%M}"]
    if end:
        lines.append(f"Finished: {end:%Y-%m-%d %H:%M}")
    if task:
        lines.append(f"Task: {task}")
    return "\n".join(lines)


def open_issue(repo: str, title: str, body: str, label: str, close: bool = False) -> str:
    args = ["issue", "create", "-R", repo, "--title", title, "--body", body]
    try:
        out = gh(*args, "--label", label)
    except TSError:
        out = gh(*args)  # the repo has no such label
    url = out.splitlines()[-1].strip()
    if close:
        gh("issue", "close", url)
    return url


def collect_github(day: dt.date, args, also: set = frozenset()) -> tuple[list, list, list]:
    """The day's GitHub activity, on every repo you touched: (point events, tracked issues opened
    by this tool, notes for the report). No repo list is configured: the repos are whatever shows
    up in your activity that day, plus `also` (the day's conversation folders) and the ones seen before."""
    lo, hi = day_bounds(day)
    span, me = f"{utc_iso(lo)}..{utc_iso(hi)}", gh_user()
    merged = gh_search(f"is:pr is:merged merged:{span} involves:{me}")
    closed = gh_search(f"is:issue closed:{span} involves:{me}")
    raised = gh_search(f"is:issue author:{me} created:{span}")
    seen = {n["repository"]["nameWithOwner"].lower() for n in merged + closed + raised} | set(also) | set(known())
    if seen:  # a PR you merged or an issue you closed without otherwise taking part isn't in "involves"
        scope = " ".join(f"repo:{name}" for name in sorted(seen))
        try:
            merged += gh_search(f"{scope} is:pr is:merged merged:{span}")
            closed += gh_search(f"{scope} is:issue closed:{span}")
        except TSError as e:  # e.g. a repo seen before that is gone or no longer yours to read
            log(f"warning: could not search the known repos, using only what GitHub links to you ({e})")
    merged, closed = ({n["url"]: n for n in found}.values() for found in (merged, closed))
    events, tracked, skipped = [], {}, set()

    def event(kind: str, n: dict, when: str) -> None:
        key = n["repository"]["nameWithOwner"].lower()
        if not resolve(key, args):
            skipped.add(key)
        else:
            events.append({"kind": kind, "repo": key, "project": project_of(key),
                           "t": local(when).replace(second=0), "n": n["number"],
                           "title": n["title"], "url": n["url"], "base": n.get("baseRefName", ""),
                           "head": n.get("headRefName", ""), "add": n.get("additions"), "del": n.get("deletions")})

    def track(n: dict) -> bool:
        body = n.get("body") or ""
        if MARKER not in body or who(n["author"]) != me:
            return False
        started, finished = body_field(body, "Started"), body_field(body, "Finished")
        tracked[n["url"]] = {
            "repo": n["repository"]["nameWithOwner"].lower(), "n": n["number"], "url": n["url"],
            "text": body_field(body, "Work") or n["title"], "kind": body_field(body, "Kind") or "development",
            "task": body_field(body, "Task"),
            "start": dt.datetime.strptime(started, "%Y-%m-%d %H:%M") if started else local(n["createdAt"]),
            "end": (dt.datetime.strptime(finished, "%Y-%m-%d %H:%M") if finished
                    else local(n["closedAt"]) if n.get("closedAt") else None),
        }
        return True

    for n in merged:
        if n["headRefName"] in TRUNKS:
            continue  # a release PR (develop into main) is not a piece of work
        if who(n["author"]) == me:
            event("own", n, n["mergedAt"])
        elif who(n["mergedBy"]) == me:
            event("review", n, n["mergedAt"])
    for n in closed:
        closer = who((n["timelineItems"]["nodes"] or [{}])[-1].get("actor"))
        if not track(n) and closer == me:
            event("closed", n, n["closedAt"])
    for n in raised:
        if not track(n):
            event("raised", n, n["createdAt"])
    notes = [f"GitHub activity on {key} is left out: no project chosen for it yet - run this in a terminal "
             "to be asked once" for key in sorted(skipped)]
    return events, list(tracked.values()), notes


def ref(e: dict) -> str:
    return f"PR #{e['n']}" if e["kind"] in ("own", "review") else f"Issue #{e['n']}"


def describe(group: list) -> str:
    """Row description for a group of events, in the wording of the hand-written sheets."""
    kind, key = group[0]["kind"], short(group[0]["repo"])
    count = f" ({len(group)} {'PRs' if kind in ('own', 'review') else 'issues'})" if len(group) > 3 else ""
    if kind == "raised":
        keys = list(dict.fromkeys(e["repo"] for e in group))
        lines = [f"UAT test run on the {' and '.join(map(short, keys))}; raised the findings as GitHub issues."]
        for k in keys:
            nums = [f"#{e['n']}" for e in group if e["repo"] == k]
            lines.append(f"Issues raised - {label_of(k)} ({len(nums)}): {', '.join(nums)}")
        return "\n".join(lines)
    head = {"own": f"Fixed, tested and merged into {group[0]['base']}{count}:",
            "review": f"Reviewed and merged into {group[0]['base']}{count}:",
            "closed": f"Checked and closed the {key} issues that are now resolved{count}:"}[kind]
    return "\n".join([head] + [f"- {ref(e)} - {e['title']}" for e in group])


def describe_tasks(events: list, day: dt.date) -> dict:
    """Task description (HTML) by task subject, listing every PR or issue of the day behind it."""
    groups = collections.defaultdict(list)
    for e in sorted(events, key=lambda e: e["t"]):
        groups[subject(e["kind"], day, e["repo"], label=e["kind"] != "raised")].append(e)
    intro = {"own": "Own changes finished, tested and merged on {}.",
             "review": "Pull requests reviewed and merged on {}.",
             "closed": "GitHub issues checked against the merged changes and closed on {}.",
             "raised": "UAT test run on {}, with the findings raised as GitHub issues."}
    listed = {"own": "Pull requests", "review": "Pull requests", "closed": "Issues closed", "raised": "Issues raised"}
    out = {}
    for subj, group in groups.items():
        kind, items = group[0]["kind"], []
        for e in group:
            name = f"{short(e['repo'])} #{e['n']}" if kind == "raised" else ref(e)
            size = f" (+{e['add']} / -{e['del']})" if e.get("add") is not None else ""
            items.append(f'<li><a href="{html.escape(e["url"])}">{name}</a> - {html.escape(e["title"])}{size}</li>')
        out[subj] = (f"<p><strong>{html.escape(subj)}</strong></p><p>{intro[kind].format(fmt_day(day))}</p>"
                     f"<p>{listed[kind]} ({len(group)}):</p><ul>{''.join(items)}</ul>")
    return out


# ---------------------------------------------------------------- Claude Code conversations

SECRETISH = re.compile(r"[A-Za-z0-9+/_\-]{24,}|\b[0-9a-f]{15,}\b|gh[pousr]_\w+|sk-\w+")
TRUNKS = ("", "HEAD", "main", "master", "develop", "staging")
# How a "user" message starts when the app wrote it, not you.
NOT_TYPED = ("<local-command", "<command-", "<task-notification", "<user-prompt-submit-hook", "<bash-",
             "[Request interrupted", "Caveat:", "[SYSTEM NOTIFICATION")


def repo_for(cwd: str) -> str:
    """The GitHub repo ('owner/name') of the folder a session ran in; '' when it is no repo."""
    if cwd and os.path.isdir(cwd):
        try:
            remote = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                                    capture_output=True, text=True).stdout.strip().lower()
        except OSError:
            remote = ""
        if m := re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote):
            return m[1]
    # The folder is gone (a removed worktree): fall back to a repo seen before whose name is in the path.
    path = (cwd or "").replace("\\", "/").lower()
    hits = [r for r in known() if r.split("/")[1] in path]
    return max(hits, key=len) if hits else ""  # longest: 'sg-network' is inside 'sg-network-console'


def read_sessions(day: dt.date) -> list:
    """Claude Code work on `day` (CLI and the desktop app's Code tab), one entry per sitting.

    A conversation is cut wherever you paused longer than IDLE_MINUTES, and each sitting stands on
    its own: its own times, folder, branch, and what was asked *during it* - one long conversation
    often moves from one subject to another, so its sittings must not share a description."""
    lo, hi = day_bounds(day)
    since = (lo - offset()).replace(tzinfo=dt.timezone.utc).timestamp()
    utc_days = {(lo - offset()).date().isoformat(), (hi - offset()).date().isoformat()}
    idle, sessions, repo_of = dt.timedelta(minutes=int(cfg("IDLE_MINUTES"))), [], {}
    for path in (Path.home() / ".claude" / "projects").glob("*/*.jsonl"):
        entries, cwd, branch = [], "", ""  # (time, "user" / "assistant" / "", text, folder, branch)
        try:
            if path.stat().st_mtime < since:
                continue
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not any(f'"timestamp":"{d}' in line for d in utc_days):
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("type") not in ("user", "assistant") or o.get("isSidechain") or not o.get("timestamp"):
                        continue
                    t = local(o["timestamp"])
                    if not lo <= t <= hi:
                        continue
                    cwd, branch = o.get("cwd") or cwd, o.get("gitBranch") or branch
                    content = (o.get("message") or {}).get("content")
                    parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
                    text = " ".join(p.get("text", "") for p in parts
                                    if isinstance(p, dict) and p.get("type") == "text").strip()
                    # Only what you typed counts as "asked": not tool output, not notes the app adds.
                    # (Text you pasted also starts with a "<tag>", so only the app's own tags are dropped.)
                    if o["type"] == "user":
                        text = re.sub(r"<system-reminder>.*?</system-reminder>\s*", "", text, flags=re.S).strip()
                    spoken = text and (o["type"] == "assistant" or not (o.get("isMeta") or text.startswith(NOT_TYPED)))
                    entries.append((t, o["type"] if spoken else "", text, cwd, branch))
        except OSError:
            continue
        if any(role == "user" and text.startswith(PROMPT_MARK) for _, role, text, _, _ in entries):
            continue  # this tool's own `claude -p` calls
        entries.sort(key=lambda e: e[0])
        for b0, b1 in split_blocks([e[0] for e in entries], idle):
            if b1 - b0 < MIN_ROW:
                continue
            inside = [e for e in entries if b0 <= e[0] <= b1]
            # A long message keeps both ends: pasted material usually comes first, the actual request last.
            trim = lambda text: text if len(text) <= 700 else f"{text[:300]} [...] {text[-400:]}"  # noqa: E731
            asked = [trim(text) for _, role, text, _, _ in inside if role == "user"]
            if not asked:  # the assistant was still working on the request from before the pause
                asked = [trim(text) for t, role, text, _, _ in entries if role == "user" and t < b0][-1:]
            said = [text for _, role, text, _, _ in inside if role == "assistant"]
            folder, on_branch = inside[-1][3], inside[-1][4]
            if folder not in repo_of:
                repo_of[folder] = repo_for(folder)
            joined = "\n- ".join(asked)
            sessions.append({
                "id": f"{path.stem}:{b0:%H:%M}", "repo": repo_of[folder], "cwd": folder, "branch": on_branch,
                "blocks": [(b0, b1)],
                # a long sitting: keep how it began and how it ended
                "prompts": joined if len(joined) <= 3000 else f"{joined[:1500]}\n[...]\n{joined[-1500:]}",
                "closing": said[-1][:800] if said else "",
                "mentions": {int(n) for n in re.findall(r"(?:#|pull/|issues/)(\d{1,6})\b", " ".join(asked + said))},
            })
    return sorted(sessions, key=lambda s: s["blocks"][0][0])


def summarise(sessions: list, day: dt.date) -> None:
    """Gives each sitting a one-line 'summary' and a 'kind' through `claude -p`.
    Only that line ever leaves the machine; raw conversation text never does."""
    # A summary is written once per sitting and then reused: tasks are found by their subject, so
    # a re-run (after a dry-run, or a failed save) must word them the same way. A sitting that is
    # still going is not kept, because what it is about can still change.
    state = load_state()
    keep_from = (day - dt.timedelta(days=30)).isoformat()
    cache = {k: v for k, v in state.get("summaries", {}).items() if k[:10] >= keep_from}
    settled = now() - dt.timedelta(minutes=int(cfg("IDLE_MINUTES")))
    everyone, sessions = sessions, []
    for s in everyone:
        where = label_of(s["repo"]) if s["repo"] else s["project"]
        s["summary"], s["kind"] = cache.get(f"{day}:{s['id']}") or (f"Worked with Claude Code on {where}", "development")
        if f"{day}:{s['id']}" not in cache:
            sessions.append(s)
    if not sessions:
        return
    parts = [f"{PROMPT_MARK} Below are numbered sittings of work. For each one, write a single line for a "
             "timesheet saying what the person worked on in that sitting. Go by what they ASKED FOR in that "
             "sitting: name the actual subject (the feature, bug, screen, document or tool) and what was done to "
             "it. A message may begin with pasted material; the request itself is usually its last sentence. If "
             "they asked for several different things, cover them together in order of weight ('Built X, added "
             "Y and documented Z') instead of picking one, and never just the last small request. The "
             "assistant's last reply is only there to tell you how it turned out. "
             "Past tense, start with the action, at most 18 words, plain words. No names of people, no file "
             f"paths, no keys or passwords. Also give the kind of work, one of: {', '.join(WORK_KINDS)}. Reply "
             'with only a JSON array like [{"n": 1, "summary": "...", "kind": "development"}].']
    for i, s in enumerate(sessions, 1):
        b0, b1 = s["blocks"][0]
        parts.append(f"\n--- Sitting {i}: {hm(b0)}-{hm(b1)}, repo {s['repo'] or 'none'}, branch {s['branch'] or 'none'}\n"
                     f"What the person asked for, in order:\n- {s['prompts'] or '(nothing typed; the assistant kept working)'}"
                     f"\n\nThe assistant's last reply in this sitting:\n{s['closing']}")
    out = run_claude("\n".join(parts), timeout=180, model="sonnet")  # the small model kept missing the point
    try:
        for item in json.loads(out[out.index("["):out.rindex("]") + 1]):
            s = sessions[int(item["n"]) - 1]
            text = " ".join(str(item["summary"]).split()).rstrip(".")[:160]
            if text and not SECRETISH.search(text):
                s["summary"] = text
            if item.get("kind") in WORK_KINDS:
                s["kind"] = item["kind"]
            if s["blocks"][0][1] <= settled:
                cache[f"{day}:{s['id']}"] = [s["summary"], s["kind"]]
    except (ValueError, KeyError, IndexError, TypeError):
        log("warning: could not summarise the conversations through Claude Code; using generic descriptions")
    state = load_state()
    state["summaries"] = cache
    save_state(state)


_PRS = {}


def branch_pr(repo: str, branch: str) -> dict | None:
    """The PR (any state) of a feature branch, if it has one."""
    if not repo or branch in TRUNKS:
        return None
    if (repo, branch) not in _PRS:
        try:
            found = json.loads(gh("pr", "list", "-R", repo, "--head", branch, "--state", "all",
                                  "--json", "number,title,url", "--limit", "1"))
        except TSError:
            found = []  # a local-only repo, or one you can no longer read
        _PRS[(repo, branch)] = found[0] if found else None
    return _PRS[(repo, branch)]


# ---------------------------------------------------------------- building a day

def build_day(day: dt.date, fixed: list, args, use_conversations: bool = True) -> tuple[list, list, dict]:
    """New rows for `day`, built around the `fixed` rows that nothing overwrites.
    Returns (rows, report lines, task descriptions by subject)."""
    lo, hi = day_bounds(day)
    new = []
    sessions = read_sessions(day) if use_conversations and shutil.which("claude") else []
    events, tracked, report = collect_github(day, args, {s["repo"] for s in sessions if s["repo"]})
    busy = [(r["frm"], r["to"]) for r in fixed]

    # Events the sheet already mentions stay as they are. A row speaks for a repo when its task is
    # one of that repo's, or belongs to no repo in particular (the day's testing task).
    subj = {name: s.lower() for name, s in task_subjects({r["task"] for r in fixed}).items()}
    heads = [info["head"].lower() for info in known().values()]

    def said(repo: str) -> str:
        mine = head_of(repo).lower()
        return "\n".join(r["description"] for r in fixed
                         if subj.get(r["task"], "").startswith(mine)
                         or not any(subj.get(r["task"], "").startswith(head) for head in heads))

    said_by = {repo: said(repo) for repo in {e["repo"] for e in events}}
    fresh = [e for e in events if not re.search(rf"#{e['n']}\b", said_by[e["repo"]])]
    if len(fresh) < len(events):
        report.append(f"{len(events) - len(fresh)} GitHub events are already in the sheet")

    def place(start: dt.datetime, end: dt.datetime, row: dict, what: str) -> None:
        """Adds an interval row in the largest piece of its window that is still free."""
        pieces = [p for p in subtract(max(start, lo), min(end, hi), busy) if p[1] - p[0] >= MIN_ROW]
        if not pieces:
            report.append(f"{what}: already covered by other rows")
            return
        row["frm"], row["to"] = max(pieces, key=lambda p: p[1] - p[0])
        busy.append((row["frm"], row["to"]))
        new.append(row)

    # Tracked issues from start / finish (also ones closed by hand on GitHub).
    everything = "\n".join(r["description"] for r in fixed)
    for t in sorted(tracked, key=lambda t: t["start"]):
        what = f"tracked issue #{t['n']}"
        if t["url"] in everything:
            continue
        if t["end"] is None:
            report.append(f"{what} is still open, not added - run: timesheet finish (or close it on GitHub)")
            continue
        if t["end"] > hi:
            report.append(f"{what} runs past midnight: the rest is left for {day + dt.timedelta(days=1)}")
        project = resolve(t["repo"], args)
        if not project and not t["task"]:
            report.append(f"{what} is left out: no project chosen for {t['repo']} yet")
            continue
        place(t["start"], t["end"],
              {"activity": activity(t["kind"]), "description": f"{t['text']}\n{TRACK}{t['url']}", "task": t["task"],
               "project": project,
               "subject": subject(t["kind"], day, t["repo"], title=t["text"][:60],
                                  window=f"{hm(max(t['start'], lo))}-{hm(min(t['end'], hi))}")}, what)

    # Claude Code conversations: part of a GitHub row, work on an open PR, or a new tracking issue.
    numbers = {(e["repo"], e["n"]) for e in events} | {(t["repo"], t["n"]) for t in tracked}
    branches = {(e["repo"], e["head"]) for e in events if e["head"]}
    loose = []
    for s in sessions:
        window = f"{hm(s['blocks'][0][0])}-{hm(s['blocks'][-1][1])}"
        where = s["repo"] or s["cwd"] or "an unknown folder"
        if (s["repo"], s["branch"]) in branches or any((s["repo"], n) in numbers for n in s["mentions"]):
            report.append(f"conversation {window} ({where}) is part of the day's GitHub rows")
            continue
        # The project depends on the folder: its repo's project, or one chosen once for a folder with no repo.
        s["project"] = resolve(s["repo"], args) if s["repo"] else resolve_folder(s["cwd"], args)
        if not s["project"]:
            report.append(f"conversation {window} ({where}) is left out: no project chosen for it yet - "
                          "run this in a terminal to be asked once")
            continue
        pr = branch_pr(s["repo"], s["branch"])
        if not pr:
            loose.append(s)
            continue
        for b0, b1 in s["blocks"]:
            place(b0, b1, {"activity": activity("own"), "task": "", "project": s["project"],
                           "description": f"Worked on PR #{pr['number']} - {pr['title']} (not merged yet).",
                           "subject": subject("own", day, s["repo"])}, f"conversation {hm(b0)}-{hm(b1)}")
    summarise(loose, day)
    for s in loose:
        # A session in a folder with no repo has nowhere to track an issue, so it gets none.
        issue = {"repo": s["repo"], "title": s["summary"], "kind": s["kind"],
                 "start": s["blocks"][0][0], "end": s["blocks"][-1][1], "url": ""} if s["repo"] else None
        subj_ = subject(s["kind"], day, s["repo"], title=s["summary"], project=s["project"],
                        window=f"{hm(s['blocks'][0][0])}-{hm(s['blocks'][-1][1])}")
        for b0, b1 in s["blocks"]:
            place(b0, b1, {"activity": activity(s["kind"]), "task": "", "subject": subj_, "issue": issue,
                           "project": s["project"],
                           "description": f"{s['summary']}." + (f"\n{NO_ISSUE}" if issue else "")},
                  f"conversation {hm(b0)}-{hm(b1)}")

    # GitHub events fill the time around everything above.
    start, lunch = parse_hm(cfg("DAY_START")), parse_window(cfg("LUNCH"))
    since_midnight = lambda t: dt.datetime.combine(day, t) - lo  # noqa: E731
    for frm, to, group in build_rows(fresh, busy, day, since_midnight(start), since_midnight(lunch[1])):
        kind = group[0]["kind"]
        new.append({"frm": frm, "to": to, "activity": activity(kind), "description": describe(group), "task": "",
                    "project": group[0]["project"],
                    "subject": subject(kind, day, group[0]["repo"], label=kind != "raised")})

    rows = fixed + new
    if rows:
        taken = [(r["frm"], r["to"]) for r in rows] + [(lo + since_midnight(lunch[0]), lo + since_midnight(lunch[1]))]
        for g0, g1 in subtract(lo + since_midnight(start), max(r["to"] for r in rows), taken):
            if g1 - g0 >= MIN_ROW:
                report.append(f"gap with no source: {hm(g0)}-{hm(g1)} - fill it with: "
                              f"timesheet add-row --date {day} --from {hm(g0)} --to {hm(g1)}")
    return new, report, describe_tasks(events, day)


def post_issues(rows: list, args) -> None:
    """Opens (and closes straight away) the tracking issues proposed for `rows`, after you confirm,
    and writes each link into its rows. Rows left without one keep the "no issue yet" mark."""
    issues = list({id(r["issue"]): r["issue"] for r in rows if r.get("issue") and not r["issue"]["url"]}.values())
    if not issues:
        return
    print("\nTracking issues to open (each is closed straight away, with its times in the body):")
    for i, p in enumerate(issues, 1):
        print(f"  {i}. {p['repo'] or 'no repo':<45} {hm(p['start'])}-{hm(p['end'])}  {p['title']}")
    chosen = [p for p in issues if p["repo"]]
    if args.dry_run or args.scheduled or not chosen:
        chosen = []
    elif not args.yes:
        if not sys.stdin or not sys.stdin.isatty():
            chosen = []
        else:
            answer = input("Open them? [y] all / [n] none / [p] pick one by one: ").strip().lower()[:1]
            if answer == "p":
                chosen = [p for p in chosen if input(f"  open '{p['title']}'? [y/N]: ").strip().lower().startswith("y")]
            elif answer != "y":
                chosen = []
    for p in chosen:
        p["url"] = open_issue(p["repo"], p["title"], issue_body(p["kind"], p["title"], p["start"], p["end"],
                                                                p.get("task", "")), p["kind"], close=True)
        log(f"opened and closed tracking issue {p['url']}")
    for r in rows:
        p = r.get("issue")
        if p and p["url"]:
            r["description"] = r["description"].replace(NO_ISSUE, TRACK + p["url"])
            r["task_html"] = (f"<p><strong>{html.escape(r.get('subject') or p['title'])}</strong></p>"
                              f"<p>{html.escape(p['title'])}.</p>"
                              f'<p><a href="{html.escape(p["url"])}">Tracking issue</a></p>')


# ---------------------------------------------------------------- timesheets and tasks on the site

def find_sheet(emp: dict, day: dt.date) -> dict | None:
    """The day's timesheet (draft or submitted), or None."""
    rows = get_list("Timesheet", [["employee", "=", emp["name"]], ["start_date", "=", day.isoformat()],
                                  ["docstatus", "!=", 2]], ["name"], order_by="creation desc")
    return api("GET", f"/api/resource/Timesheet/{rows[0]['name']}")["data"] if rows else None


def need_sheet(emp: dict, day: dt.date) -> dict:
    sheet = find_sheet(emp, day)
    if not sheet:
        raise TSError(f"no timesheet for {day} - build it with: timesheet create --date {day}")
    return sheet


def core(r: dict) -> tuple:
    return r["frm"], r["to"], r["activity"], r["description"], r.get("task") or r.get("subject") or ""


def rows_of(sheet: dict | None) -> list:
    rows = []
    for raw in (sheet or {}).get("time_logs", []):
        r = {"name": raw["name"], "frm": ptime(raw["from_time"]), "to": ptime(raw["to_time"]),
             "activity": raw.get("activity_type") or "", "description": raw.get("description") or "",
             "task": raw.get("task") or "", "raw": raw}
        r["orig"] = core(r)
        rows.append(r)
    return sorted(rows, key=lambda r: r["frm"])


def pick(rows: list, spec: str) -> list:
    """'3' or '3,5,7' -> those rows, numbered as `show` prints them."""
    try:
        return [rows[int(n) - 1] for n in spec.split(",") if int(n) > 0]
    except (ValueError, IndexError):
        raise TSError(f"--row {spec}: the sheet has rows 1 to {len(rows)}") from None


def show_rows(rows: list, before: list | None = None, full: bool = False) -> None:
    """Numbered rows. With `before`, a diff: + added, ~ changed (old -> new), - removed."""
    old = {r["name"]: r for r in before or []}
    names = ("from", "to", "activity type", "description", "task")
    for i, r in enumerate(sorted(rows, key=lambda r: r["frm"]), 1):
        was = old.get(r.get("name"))
        mark = " " if before is None or (was and core(was) == core(r)) else "~" if was else "+"
        task = r.get("task") or f"(new) {r.get('subject', '')}"
        print(f"{mark}{i:>3}  {hm(r['frm'])}-{hm(r['to'])}  {hours(r):5.2f}h  {r['activity']:<28.28}  {task}")
        if mark == "~":
            for name, a, b in zip(names, core(was), core(r)):
                if a != b:
                    a, b = (hm(v) if isinstance(v, dt.datetime) else v for v in (a, b))
                    print(f"       {name}: {a!r} -> {b!r}")
        elif full or mark == "+":
            for line in r["description"].splitlines():
                print(f"       {line}")
    kept = {r.get("name") for r in rows}
    for r in before or []:
        if r["name"] not in kept:
            print(f"-     {hm(r['frm'])}-{hm(r['to'])}  {hours(r):5.2f}h  {r['activity']:<28.28}  {r['task']}")


def check_rows(rows: list, day: dt.date) -> None:
    lo, hi = day_bounds(day)
    rows = sorted(rows, key=lambda r: r["frm"])
    for i, r in enumerate(rows):
        window = f"{hm(r['frm'])}-{hm(r['to'])}"
        if r["frm"] >= r["to"]:
            raise TSError(f"row {window}: the end must be after the start")
        if r["frm"] < lo or r["to"] > hi:
            raise TSError(f"row {window}: a timesheet for {day} only holds 00:00:00 to 23:59:59 of that date - "
                          "work past midnight goes on the next day's sheet")
        if i and rows[i - 1]["to"] > r["frm"]:
            raise TSError(f"row {window} overlaps row {hm(rows[i - 1]['frm'])}-{hm(rows[i - 1]['to'])}")


def task_subjects(ids) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {t["name"]: t["subject"] for t in get_list("Task", [["name", "in", ids]], ["name", "subject"], limit=200)}


def ensure_task(subj: str, day: dt.date, project: str, desc: str = "", status: str = "Completed",
                expected: float = 0.0, parent: str = "") -> str:
    """The task with exactly this subject in `project` - looked up first, so re-runs never duplicate."""
    if not project:
        raise TSError(f"no project for the task '{subj}' - pass --project, --repo or --task")
    found = get_list("Task", [["subject", "=", subj], ["project", "=", project]], ["name", "description"])
    if found:
        if desc and found[0].get("description") != desc:
            api("PUT", f"/api/resource/Task/{found[0]['name']}", body={"description": desc})
        return found[0]["name"]
    body = {
        "subject": subj, "project": project, "status": status, "priority": cfg("TASK_PRIORITY"),
        "exp_start_date": day.isoformat(), "exp_end_date": day.isoformat(), "expected_time": round(expected, 6),
        "description": desc or f"<p><strong>{html.escape(subj)}</strong></p>",
        # Custom fields of the 8848 projects site; a site without them ignores them.
        "custom_assigned_to": cfg("TS_USER"), "custom_module_name": cfg("TASK_MODULE"),
        "custom_naming_field": f"TASK-{now().year}-",
    }
    if parent:
        body["parent_task"] = parent
    doc = api("POST", "/api/resource/Task", body=body)["data"]
    log(f"created task {doc['name']}: {subj}")
    return doc["name"]


def sync_tasks(day: dt.date, rows: list, ids) -> None:
    """Expected time of each one-day task = the sum of its rows."""
    total = collections.defaultdict(float)
    for r in rows:
        total[r["task"]] += hours(r)
    for name in {i for i in ids if i}:
        try:
            task = api("GET", f"/api/resource/Task/{name}")["data"]
            if (task.get("exp_start_date") == task.get("exp_end_date") == day.isoformat()
                    and abs((task.get("expected_time") or 0) - total[name]) > 0.001):
                api("PUT", f"/api/resource/Task/{name}", body={"expected_time": round(total[name], 6)})
        except TSError as e:
            log(f"warning: could not update the expected time of {name}: {e}")


def payload(r: dict, fresh: bool) -> dict:
    """Row as sent to the site. An untouched row goes back exactly as it came."""
    raw = r.get("raw") or {}
    if raw and not fresh and r.get("orig") == core(r):
        return raw
    body = {} if fresh else dict(raw)
    body.update(activity_type=r["activity"], from_time=f"{r['frm']:%Y-%m-%d %H:%M:%S}",
                to_time=f"{r['to']:%Y-%m-%d %H:%M:%S}", hours=round(hours(r), 6),
                description=r["description"], project=r.get("project") or raw.get("project"), task=r["task"])
    return body


def write_sheet(emp: dict, day: dt.date, sheet: dict | None, rows: list, args, task_html: dict | None = None,
                shown: bool = False) -> dict | None:
    """Shows the change, asks, creates any missing tasks, saves the DRAFT, then re-reads it to
    confirm that no other row moved. Returns the saved sheet, or None if nothing was written."""
    rows = sorted(rows, key=lambda r: r["frm"])
    check_rows(rows, day)
    before = rows_of(sheet)
    submitted = bool(sheet) and sheet["docstatus"] == 1
    if submitted and not args.amend:
        raise TSError(f"{sheet['name']} is submitted. Add --amend to cancel it and create the amended copy "
                      "as a draft under a new number.")
    if not rows:
        raise TSError("that would leave the timesheet empty - use: timesheet delete")
    if not shown:
        show_rows(rows, before)
    question = (f"Cancel submitted {sheet['name']} and create the amended copy as a draft" if submitted
                else f"Update draft {sheet['name']}" if sheet else f"Create a draft timesheet for {day}")
    if not confirm(question + "?", args):
        return None

    made = {}
    for r in rows:  # tasks first, so every row has one to link to
        if not r.get("task"):
            if r["subject"] not in made:
                made[r["subject"]] = ensure_task(r["subject"], day, r.get("project", ""),
                                                 (task_html or {}).get(r["subject"]) or r.get("task_html", ""))
            r["task"] = made[r["subject"]]
        elif not r.get("raw") or r["task"] != r["raw"].get("task"):  # the row follows its task's project
            r["project"] = api("GET", f"/api/resource/Task/{r['task']}")["data"].get("project")
    logs = [payload(r, fresh=submitted) for r in rows]
    if submitted:
        api("POST", "/api/method/frappe.client.cancel", body={"doctype": "Timesheet", "name": sheet["name"]})
        try:
            doc = api("POST", "/api/resource/Timesheet", body={
                "employee": emp["name"], "company": sheet.get("company") or emp["company"],
                "amended_from": sheet["name"], "time_logs": logs})["data"]
        except TSError as e:
            raise TSError(f"{sheet['name']} is now cancelled, but creating the amended copy failed: {e}\n"
                          "Its rows are still on the cancelled sheet: open it on the site and press Amend.",
                          e.detail) from None
    elif sheet:
        doc = api("PUT", f"/api/resource/Timesheet/{sheet['name']}", body={"time_logs": logs})["data"]
    else:
        doc = api("POST", "/api/resource/Timesheet", body={
            "employee": emp["name"], "company": emp["company"], "time_logs": logs})["data"]
    log(f"saved draft {doc['name']} for {day}: {len(rows)} rows, {sum(map(hours, rows)):.2f}h - "
        f"{cfg('TS_URL')}/app/timesheet/{doc['name']}")

    after = rows_of(doc)
    now_there = {core(r) for r in after}
    moved = [r for r in rows if r.get("orig") == core(r) and r["orig"] not in now_there]
    for r in moved:
        log(f"warning: row {hm(r['frm'])}-{hm(r['to'])} was not touched here but changed on save - check it")
    if not moved:
        print(f"checked: the {sum(1 for r in rows if r.get('orig') == core(r))} untouched rows are unchanged.")
    sync_tasks(day, after, {r["task"] for r in before + after})
    return doc


# ---------------------------------------------------------------- commands: the day's sheet

def manual_row(day: dt.date, frm: str, to: str, kind: str, text: str, args, task: str = "", repo: str = "",
               project: str = "", sheet: dict | None = None) -> dict:
    """A row you describe yourself. With --task it follows that task. Otherwise it goes on the day's
    task for that activity, in the first project that is known: --project, the repo's (--repo),
    the one all other rows of the sheet share, else the one you pick when asked."""
    learn()
    act, repo = activity(kind or cfg("DEFAULT_ACTIVITY")), repo_arg(repo)
    if not task:
        shared = {r.get("project") for r in (sheet or {}).get("time_logs", [])} - {None, ""}
        project = (project or (resolve(repo, args) if repo else "") or (shared.pop() if len(shared) == 1 else "")
                   or ask_project("this row", args))
        if not project:
            raise TSError("which project is this row for? Pass --project, --repo or --task")
    suffix = {"Testing": "UAT Testing", "Code Review": "PR Review & Merge",
              "Issue handling": "GitHub Issues Resolved"}.get(act, act.split(" / ")[-1])
    return {"frm": at(day, frm), "to": at(day, to), "activity": act, "description": typed(text, args),
            "task": task, "project": project,
            "subject": subject(suffix, day, repo, label=act != "Testing", project=project)}


def cmd_create(args) -> int:
    emp, day = connect(), args.date
    if not args.force and (why := skip_reason(emp, day)):
        log(f"skip {day}: {why} (--force builds it anyway)")
        return 0
    sheet = find_sheet(emp, day)
    if sheet and sheet["docstatus"] == 1:
        log(f"{sheet['name']} for {day} is submitted - left alone (to change it: timesheet update --amend)")
        return 0
    rows = rows_of(sheet)
    for note in args.note or []:
        parts = [p.strip() for p in note.split("|")]
        window = parts[0].split("-")
        if len(parts) not in (3, 4) or len(window) != 2:
            raise TSError(f"--note '{note}': use \"13:20-16:11 | Testing | what you did\", "
                          "with \"| repo\" at the end to file it under that repo's project")
        rows.append(manual_row(day, window[0], window[1], parts[1], parts[2], args, repo=(parts + [""])[3],
                               sheet=sheet))
    check_rows(rows, day)
    new, report, task_html = build_day(day, rows, args, use_conversations=not args.no_conversations)
    added = len(rows) - len(sheet["time_logs"] if sheet else []) + len(new)
    rows += new

    print(f"\nTimesheet for {day:%A} {fmt_day(day)}" + (f" - draft {sheet['name']}" if sheet else " - new draft"))
    show_rows(rows, rows_of(sheet))
    for line in report:
        print(f"note: {line}")
    submit = args.submit and not args.scheduled  # the daily job never submits
    if not added:
        log(f"{day}: nothing new to add")
        return submit_sheet(sheet, day, args) if submit and sheet else 0
    post_issues(new, args)
    doc = write_sheet(emp, day, sheet, rows, args, task_html, shown=True)
    if doc and submit:
        return submit_sheet(doc, day, args)
    if doc and args.scheduled and flag("NOTIFY"):
        popup("Timesheet draft created", f"Draft {doc['name']} for {fmt_day(day)} was created with {len(rows)} rows."
              "\n\nReview and submit it on the site.", f"{cfg('TS_URL')}/app/timesheet/{doc['name']}")
    return 0


def submit_sheet(sheet: dict, day: dt.date, args) -> int:
    """Submits the day's draft - only ever on your word (`submit`, or `create --submit`), after a y/N."""
    if sheet["docstatus"] == 1:
        print(f"{sheet['name']} for {day} is already submitted.")
        return 0
    rows = rows_of(sheet)
    print(f"\n{sheet['name']}  {day:%A} {fmt_day(day)}  {len(rows)} rows, {sum(map(hours, rows)):.2f}h")
    show_rows(rows)
    waiting = sum(NO_ISSUE in r["description"] for r in rows)
    if waiting:
        print(f"note: {waiting} rows still say {NO_ISSUE} - `timesheet issues --date {day}` opens them first")
    if not confirm(f"Submit {sheet['name']}? After that it can only be changed with --amend", args):
        return 0
    api("POST", "/api/method/frappe.client.submit", body={"doc": sheet})
    log(f"submitted {sheet['name']} for {day} - {cfg('TS_URL')}/app/timesheet/{sheet['name']}")
    return 0


def cmd_submit(args) -> int:
    return submit_sheet(need_sheet(connect(), args.date), args.date, args)


def cmd_show(args) -> int:
    sheet = need_sheet(connect(), args.date)
    rows = rows_of(sheet)
    print(f"{sheet['name']}  {args.date:%A} {fmt_day(args.date)}  {sheet['status']}  "
          f"{sum(map(hours, rows)):.2f}h  {cfg('TS_URL')}/app/timesheet/{sheet['name']}")
    show_rows(rows, full=True)
    return 0


UPDATE_FIELDS = (("frm", "from"), ("to", "to"), ("description", "description"),
                 ("type", "activity type"), ("task", "task"))


def cmd_update(args) -> int:
    emp, day = connect(), args.date
    sheet = need_sheet(emp, day)
    rows = rows_of(sheet)
    if args.regen:
        return regen(emp, day, sheet, rows, args)
    if not args.row:
        raise TSError(f"say which row: --row N (numbers are in: timesheet show --date {day})")
    chosen = pick(rows, args.row)
    if len(chosen) != 1:
        raise TSError("update changes one row at a time; --regen takes several")
    r = chosen[0]
    given = {key: getattr(args, key) for key, _ in UPDATE_FIELDS if getattr(args, key) is not None}
    if not given:  # no flags: ask field by field, Enter keeps the value in [brackets]
        need_tty()
        current = {"frm": hm(r["frm"]), "to": hm(r["to"]), "description": r["description"],
                   "type": r["activity"], "task": r["task"]}
        for key, label in UPDATE_FIELDS:
            first = current[key].split("\n")[0] + (" ..." if "\n" in current[key] else "")
            if answer := input(f"{label} [{first}]: ").strip():
                given[key] = answer
    if "frm" in given:
        r["frm"] = at(day, given["frm"])
    if "to" in given:
        r["to"] = at(day, given["to"])
    if "description" in given:
        r["description"] = typed(given["description"], args)
    if "type" in given:
        r["activity"] = activity(given["type"])
    if "task" in given:
        r["task"] = api("GET", f"/api/resource/Task/{given['task']}")["data"]["name"]
    if core(r) == r["orig"]:
        print("nothing to change.")
        return 0
    write_sheet(emp, day, sheet, rows, args)
    return 0


def regen(emp: dict, day: dt.date, sheet: dict, rows: list, args) -> int:
    """Re-derives the chosen rows (all, without --row) from the sources; --only limits the fields."""
    fields = {"from", "to", "description", "activity_type", "task"}
    only = {f.strip() for f in args.only.split(",")} if args.only else fields
    if only - fields:
        raise TSError(f"--only takes: {', '.join(sorted(fields))}")
    chosen = pick(rows, args.row) if args.row else list(rows)
    keep = [r for r in rows if not any(r is c for c in chosen)]
    new, report, task_html = build_day(day, keep, args)
    for r in chosen:  # each row takes the re-derived row that shares the most time with it
        best = max(new, key=lambda n: min(n["to"], r["to"]) - max(n["frm"], r["frm"]), default=None)
        if not best or min(best["to"], r["to"]) <= max(best["frm"], r["frm"]):
            print(f"note: row {hm(r['frm'])}-{hm(r['to'])} has nothing behind it in the sources - left as it is")
            continue
        new.remove(best)
        if "from" in only:
            r["frm"] = best["frm"]
        if "to" in only:
            r["to"] = best["to"]
        if "description" in only:
            r["description"] = best["description"]
        if "activity_type" in only:
            r["activity"] = best["activity"]
        if "task" in only and best["subject"] != task_subjects([r["task"]]).get(r["task"]):
            r["task"], r["subject"], r["project"] = "", best["subject"], best["project"]
    for line in report:
        print(f"note: {line}")
    if all(core(r) == r["orig"] for r in rows):
        print("the rows already match the sources.")
        return 0
    write_sheet(emp, day, sheet, rows, args, task_html)
    return 0


def cmd_add_row(args) -> int:
    emp, day = connect(), args.date
    asked = (("frm", "from, 24h"), ("to", "to, 24h"), ("description", "description"),
             ("type", f"activity type [{cfg('DEFAULT_ACTIVITY')}]"), ("task", "task [the day's task for that activity]"))
    if any(getattr(args, key) is None for key in ("frm", "to", "description")):  # not all given: ask
        need_tty()
        for key, label in asked:
            if getattr(args, key) is None:
                setattr(args, key, input(f"{label}: ").strip())
    if not args.description:
        raise TSError("a row needs a description")
    sheet = find_sheet(emp, day)
    row = manual_row(day, args.frm, args.to, args.type or "", args.description, args, args.task or "",
                     args.repo or "", args.project or "", sheet)
    write_sheet(emp, day, sheet, rows_of(sheet) + [row], args)
    return 0


def cmd_delete_row(args) -> int:
    emp, day = connect(), args.date
    sheet = need_sheet(emp, day)
    rows = rows_of(sheet)
    gone = pick(rows, args.row)
    write_sheet(emp, day, sheet, [r for r in rows if not any(r is g for g in gone)], args)
    return 0


def cmd_delete(args) -> int:
    sheet = need_sheet(connect(), args.date)
    if sheet["docstatus"] != 0:
        raise TSError(f"{sheet['name']} is submitted - only a draft can be deleted (cancel it on the site)")
    rows = rows_of(sheet)
    show_rows(rows)
    if confirm(f"Delete draft {sheet['name']} with these {len(rows)} rows?", args):
        api("DELETE", f"/api/resource/Timesheet/{sheet['name']}")
        log(f"deleted draft {sheet['name']} for {args.date}")
        sync_tasks(args.date, [], {r["task"] for r in rows})
    return 0


def cmd_issues(args) -> int:
    """Opens the tracking issues for rows marked "no issue yet" (left by `auto` runs, or declined)."""
    emp, day = connect(), args.date
    sheet = need_sheet(emp, day)
    rows = rows_of(sheet)
    marked = [r for r in rows if NO_ISSUE in r["description"]]
    if not marked:
        print(f"no rows on {day} are waiting for an issue.")
        return 0
    subj, by_task = task_subjects({r["task"] for r in marked}), {}
    learn()
    for r in marked:  # rows of one task share one issue
        p = by_task.get(r["task"])
        if not p:
            name = subj.get(r["task"], "").lower()  # the task's subject starts with its repo's wording
            home = max((x for x in known() if name.startswith(head_of(x).lower())),
                       key=lambda x: len(head_of(x)), default="")
            kind = next((k for k in WORK_KINDS if KINDS[k][1] == r["activity"]), "development")
            p = by_task[r["task"]] = {"repo": home, "kind": kind, "url": "", "task": r["task"],
                                      "title": r["description"].split("\n")[0].rstrip("."),
                                      "start": r["frm"], "end": r["to"]}
        p["start"], p["end"] = min(p["start"], r["frm"]), max(p["end"], r["to"])
        r["issue"], r["subject"] = p, subj.get(r["task"], "")
    post_issues(marked, args)
    if not any(p["url"] for p in by_task.values()):
        return 0
    if write_sheet(emp, day, sheet, rows, args):
        for r in marked:
            if r["issue"]["url"]:
                api("PUT", f"/api/resource/Task/{r['task']}", body={"description": r["task_html"]})
    return 0


# ---------------------------------------------------------------- commands: start / finish

def cmd_start(args) -> int:
    state = load_state()
    if cur := state.get("started"):
        print(f"already started at {cur['started'][11:16]}: {cur['text']}")
        if not confirm("Start another anyway? Their rows would overlap", args):
            return 1
    connect()
    learn()
    repo = repo_arg(args.repo) or repo_for(os.getcwd())  # the folder you are in decides, unless you say
    if not repo:
        raise TSError("which repo is this for? Run it inside the repo's folder, or pass --repo owner/name")
    project = resolve(repo, args)
    if not project:
        raise TSError(f"no project chosen for {repo} yet - run this in a terminal (without --yes) to be asked once")
    start = at(now().date(), args.at) if args.at else now().replace(second=0)
    kind, text = (args.type or guess_kind(args.text)).lower(), typed(args.text, args)
    activity(kind)  # refuse an unknown type before anything is created
    label = kind.title() if kind in KINDS else kind
    subj = subject(kind, start.date(), repo, title=text[:1].upper() + text[1:60], window=f"from {hm(start)}")
    title = f"[{label}] {text} - {fmt_day(start.date())}"
    print(f"task:   {subj}  (Working, project {project})\nissue:  {repo}: {title}\n"
          f"start:  {start:%Y-%m-%d %H:%M}")
    if not confirm("Create the task, open the issue and start the clock?", args):
        return 0
    task = ensure_task(subj, start.date(), project, status="Working")
    state = load_state()  # re-read: learning the repo's project wrote to it meanwhile
    state["started"] = {"task": task, "subject": subj, "text": text, "kind": kind, "repo": repo,
                        "label": label_of(repo), "started": f"{start:%Y-%m-%d %H:%M:%S}", "issue": ""}
    save_state(state)  # saved before the issue, so a failure there still leaves something to finish
    url = open_issue(repo, title, issue_body(kind, text, start, task=task), kind)
    state["started"]["issue"] = url
    save_state(state)
    api("PUT", f"/api/resource/Task/{task}", body={"description": (
        f"<p><strong>{html.escape(subj)}</strong></p><p>{html.escape(text)}</p>"
        f'<p><a href="{html.escape(url)}">Tracking issue</a></p>')})
    log(f"started {task} / {url} at {hm(start)} - end it with: timesheet finish")
    return 0


def cmd_finish(args) -> int:
    state = load_state()
    cur = state.get("started")
    if not cur:
        raise TSError('nothing is started - begin with: timesheet start "what you are doing"')
    start = ptime(cur["started"])
    end = at(now().date(), args.at) if args.at else now().replace(second=0)
    if end <= start:
        raise TSError(f"the end ({end:%Y-%m-%d %H:%M}) must be after the start ({start:%Y-%m-%d %H:%M})")
    day = start.date()
    cut = min(end, day_bounds(day)[1])  # nothing spills into the next day's sheet
    emp = connect()
    sheet = find_sheet(emp, day)
    raised = []
    if cur["issue"]:
        found = gh_search(f"repo:{cur['repo']} is:issue author:{gh_user()} created:{utc_iso(start)}..{utc_iso(cut)}")
        raised = [f"#{n['number']}" for n in found if MARKER not in (n.get("body") or "")]
    lines = [cur["text"]]
    if raised:
        lines.append(f"Issues raised - {cur['label'] or cur['repo']} ({len(raised)}): {', '.join(raised)}")
    if cur["issue"]:
        lines.append(TRACK + cur["issue"])
    row = {"frm": start, "to": cut, "activity": activity(cur["kind"]), "description": "\n".join(lines),
           "task": cur["task"]}
    if end > cut:
        print(f"note: this ran past midnight. The row stops at 23:59:59; the rest appears when you run: "
              f"timesheet create --date {day + dt.timedelta(days=1)}")
    if not write_sheet(emp, day, sheet, rows_of(sheet) + [row], args):
        return 0
    try:  # the row is saved; the rest is tidying up, so a failure here must not strand the clock
        if cur["issue"]:
            gh("issue", "edit", cur["issue"], "--body", issue_body(cur["kind"], cur["text"], start, end, cur["task"]))
            gh("issue", "close", cur["issue"])
        done = cur["subject"].replace(f"(from {hm(start)})", f"({hm(start)}-{hm(cut)})")
        body = {"status": "Completed", "subject": done, "expected_time": round(hours(row), 6)}
        if raised:
            desc = api("GET", f"/api/resource/Task/{cur['task']}")["data"].get("description") or ""
            body["description"] = desc + f"<p>Issues raised: {html.escape(', '.join(raised))}</p>"
        api("PUT", f"/api/resource/Task/{cur['task']}", body=body)
        log(f"finished {cur['task']} ({hm(start)}-{hm(cut)}) and closed {cur['issue'] or 'no issue'}")
    except TSError as e:
        log(f"warning: the row is saved, but closing the issue or completing the task failed: {e}")
    state["started"] = None
    save_state(state)
    return 0


def cmd_status(args) -> int:
    cur = load_state().get("started")
    if not cur:
        print("nothing is started.")
        return 0
    minutes = int((now() - ptime(cur["started"])).total_seconds() // 60)
    print(f"started {cur['started'][:16]} ({minutes // 60}h {minutes % 60:02d}m ago): {cur['text']}\n"
          f"task:  {cur['task']}  {cur['subject']}\nissue: {cur['issue'] or 'none'}")
    return 0


# ---------------------------------------------------------------- commands: tasks alone

TASK_TYPES = {"fixes": "own", "own": "own", "review": "review", "issues": "closed", "testing": "raised"}


def cmd_task(args) -> int:
    connect()
    learn()
    if args.action == "list":
        filters = [["owner", "=", cfg("TS_USER")]]
        if args.repo or args.project:
            filters.append(["project", "=", args.project or resolve(repo_arg(args.repo), args)])
        if args.date_given:
            filters.append(["exp_start_date", "=", args.date.isoformat()])
        for t in get_list("Task", filters, ["name", "subject", "status", "project", "exp_start_date", "expected_time"],
                          order_by="creation desc", limit=20):
            print(f"{t['name']}  {t['status']:<10}  {t['exp_start_date']}  {t['expected_time'] or 0:5.2f}h  "
                  f"{t['project']}: {t['subject']}")
        return 0
    if args.action == "update":
        if not args.name:
            raise TSError("say which task: timesheet task update TASK-... --status Completed")
        body = {"status": args.status, "subject": args.subject, "expected_time": args.hours,
                "description": typed(args.desc, args) if args.desc else None}
        body = {k: v for k, v in body.items() if v is not None}
        if not body:
            raise TSError("nothing to change - pass --status, --subject, --desc or --hours")
        task = api("GET", f"/api/resource/Task/{args.name}")["data"]
        for key, value in body.items():
            print(f"  {key}: {task.get(key)!r} -> {value!r}")
        if confirm(f"Update {args.name}?", args):
            api("PUT", f"/api/resource/Task/{args.name}", body=body)
            log(f"updated task {args.name}: {', '.join(body)}")
        return 0
    # create: free-form with --subject, or from the day's GitHub activity with --repo and --type
    day, desc, key = args.date, "", repo_arg(args.repo)
    project = (args.project or (resolve(key, args) if key else "")
               or ask_project("this task", args))
    if not project:
        raise TSError("which project is this task for? Pass --project, or --repo")
    if args.subject:
        subj = args.subject
        desc = f"<p><strong>{html.escape(subj)}</strong></p><p>{html.escape(typed(args.desc, args))}</p>" if args.desc else ""
    else:
        kind = TASK_TYPES.get((args.type or "").lower())
        if not kind or not key:
            raise TSError(f"pass --subject, or --type ({', '.join(TASK_TYPES)}) with --repo owner/name")
        subj = subject(kind, day, key, label=kind != "raised")
        desc = describe_tasks(collect_github(day, args, {key})[0], day).get(subj, "")
        if not desc:
            print(f"note: no matching GitHub activity on {day}; the task will have no list.")
    print(f"task: {subj}\n      project {project or '(none)'}, {fmt_day(day)}, {args.hours or 0}h"
          + (f", under {args.parent}" if args.parent else ""))
    if desc:
        print("      " + plain_text(desc).replace("\n", "\n      "))
    if confirm("Create this task (or reuse it if it exists)?", args):
        name = ensure_task(subj, day, project, desc, expected=args.hours or 0.0, parent=args.parent or "")
        print(f"{name}  {cfg('TS_URL')}/app/task/{name}")
    return 0


# ---------------------------------------------------------------- commands: config and schedule

def read_env() -> dict:
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip()
    return values


def write_env(values: dict) -> None:
    lines = ["# Written by `timesheet config` - change values with: timesheet config set KEY=value"]
    lines += [f"{key}={val}" for key, val in values.items() if val != "" and key in SETTINGS]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        ENV_FILE.chmod(0o600)


def apply_settings(changes: dict) -> int:
    """Validates everything first, then writes .env; re-tests the login or re-registers the job if needed."""
    cleaned = {}
    for key, value in changes.items():
        key = key.strip().upper()
        if any(word in key for word in ("PASSWORD", "PWD", "SECRET")):
            raise TSError("passwords never go on the command line (they would land in your shell history) - "
                          "use: timesheet config set-secret")
        if key not in SETTINGS:
            raise TSError(f"unknown setting {key} - see: timesheet config list. Nothing was changed.")
        try:
            cleaned[key] = clean(key, value) if value is not None else ""
        except ValueError as e:
            raise TSError(f"{e}. Nothing was changed.") from None
    values = read_env()
    values.update(cleaned)
    write_env(values)
    for key, value in cleaned.items():
        os.environ[key] = value
        print(f"{key}={value or SETTINGS[key][0]}" + ("" if value else "  (default)"))
    code = 0
    if "SCHEDULE" in cleaned:
        register_schedule(cfg("SCHEDULE"))
    if {"TS_URL", "TS_USER"} & set(cleaned):
        code = config_test()
    return code


def config_test() -> int:
    """Checks the login, the project and `gh` access. Creates nothing."""
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            print(f"  ok    {label}: {fn()}")
        except (TSError, subprocess.SubprocessError, OSError) as e:
            ok = False
            print(f"  FAIL  {label}: {e}")

    emp = {}
    check("site login", lambda: emp.update(connect()) or f"{emp['employee_name']} ({emp['name']})")
    check("GitHub login", gh_user)
    if emp:  # nothing to configure here: this is what your earlier tasks say about each repo
        learn()
        for repo, seen in sorted(known().items()):
            print(f"  ok    {repo}: project {seen['project']}, tasks named \"{seen['head']} - ...\"")
        if not known():
            print("  note  no repos learned yet - a repo's project is asked the first time its work shows up")
        for folder, project in sorted(load_state().get("folders", {}).items()):
            print(f"  ok    folder {folder}: project {project}  (change: timesheet config forget {folder})")
    print(f"  {'ok  ' if shutil.which('claude') else 'note'}  Claude Code: "
          + ("found - conversations and --polish are available" if shutil.which("claude")
             else "not installed - GitHub is the only source, and --polish is off"))
    return 0 if ok else 1


def set_secret() -> int:
    if not cfg("TS_USER"):
        raise TSError("set your login email first: timesheet config set TS_USER=you@yourcompany.com")
    need_tty()
    pwd = getpass.getpass(f"Site password for {cfg('TS_USER')} (typing is hidden): ")
    if not pwd:
        raise TSError("no password entered; nothing was changed")
    save_password(pwd)
    print("Saved in the OS credential store (never in .env).")
    return config_test()


def guided_setup() -> int:
    need_tty()
    # Anything that would stop the password being saved is checked first, so setup never asks
    # for your details and then fails.
    try:
        __import__("keyring")
    except ImportError:
        if sys.platform != "darwin":
            raise TSError("the 'keyring' package is missing, so the password could not be stored. Install it with:\n"
                          f"  {sys.executable} -m pip install --user keyring\nthen run this again. "
                          "Nothing was asked or saved.") from None
    if not shutil.which("gh"):
        raise TSError("the GitHub CLI (gh) is not installed - get it from https://cli.github.com, run "
                      "'gh auth login', then run this again. Nothing was asked or saved.")
    print("timesheet setup - press Enter to keep the value in [brackets].\n")
    values = {}
    for key in GUIDED:
        while True:
            answer = input(f"{SETTINGS[key][2]}\n  {key} [{cfg(key)}]: ").strip() or cfg(key)
            try:
                values[key] = clean(key, answer) if answer else ""
                break
            except ValueError as e:
                print(f"  {e}")
    env = read_env()
    env.update(values)
    write_env(env)
    os.environ.update(values)
    try:
        stored_password()
        replace = input(f"\nA saved password exists for {cfg('TS_USER')}. Replace it? [y/N]: ").lower().startswith("y")
    except TSError:
        replace = True
    if replace:
        save_password(getpass.getpass(f"\nSite password for {cfg('TS_USER')} (typing is hidden): "))
    print("\nTesting (nothing is created)...")
    code = config_test()
    register_schedule(cfg("SCHEDULE"))
    print("\nDone. Try: timesheet create --dry-run" if code == 0 else
          "\nFix the FAIL lines above, then run: timesheet config test")
    return code


def register_schedule(mode: str) -> None:
    """Registers (remind / auto) or removes (off) the job that runs `timesheet scheduled` every 15
    minutes; once the day is handled, those runs exit without contacting the site."""
    script = str(HERE / "timesheet.py")
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")  # no console window popping up
        exe = pyw if pyw.exists() else Path(sys.executable)
        # "; exit 0": removing a job that was never registered is not a failure
        ps = (f'Unregister-ScheduledTask -TaskName "{JOB_NAME}" -Confirm:$false -ErrorAction SilentlyContinue; exit 0'
              if mode == "off" else
              f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '\"{script}\" scheduled' -WorkingDirectory '{HERE}'\n"
              "$t = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today)\n"
              "$t.Repetition = (New-ScheduledTaskTrigger -Once -At ([datetime]::Today) -RepetitionInterval "
              "(New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 59)).Repetition\n"
              # StartWhenAvailable: a run missed while asleep happens on wake. Battery flags: laptops default to off.
              "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
              "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)\n"
              f'Register-ScheduledTask -TaskName "{JOB_NAME}" -Action $a -Trigger $t -Settings $s -Force '
              "-ErrorAction Stop | Out-Null")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, text=True)
        if r.returncode:
            raise TSError(f"could not change the scheduled task: {r.stderr.strip()[:300]}")
    elif sys.platform == "darwin":
        label = "local.timesheet-autofill"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        if mode == "off":
            plist.unlink(missing_ok=True)
        else:
            minutes = "".join(f"<dict><key>Minute</key><integer>{m}</integer></dict>" for m in (0, 15, 30, 45))
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                f"<key>Label</key><string>{label}</string><key>ProgramArguments</key><array>"
                f"<string>{sys.executable}</string><string>{script}</string><string>scheduled</string></array>"
                f"<key>RunAtLoad</key><true/><key>StartCalendarInterval</key><array>{minutes}</array>"
                # let the popup (a separate osascript process) outlive the script
                "<key>AbandonProcessGroup</key><true/></dict></plist>\n")
            subprocess.run(["launchctl", "load", str(plist)], check=True)
    elif mode != "off":
        print(f"On this system add the job yourself, e.g. a cron line:\n  */15 * * * * {sys.executable} {script} scheduled")
        return
    print(f"daily job: {mode}")


def cmd_config(args) -> int:
    action, items = args.action, args.items
    if not action:
        return guided_setup()
    if action == "list":
        env = read_env()
        for key, (default, _, about) in SETTINGS.items():
            print(f"{key}={cfg(key)}" + ("" if env.get(key) or os.environ.get(key) else "  (default)") + f"\n    {about}")
        return 0
    if action == "get":
        for key in items:
            if key.upper() not in SETTINGS:
                raise TSError(f"unknown setting {key} - see: timesheet config list")
            print(cfg(key.upper()))
        return 0
    if action == "set":
        if not items or any("=" not in item for item in items):
            raise TSError('use: timesheet config set KEY=value [KEY=value ...]  (quote values with spaces or "|")')
        return apply_settings(dict(item.split("=", 1) for item in items))
    if action == "unset":
        return apply_settings({key: None for key in items})
    if action == "forget":  # drop a remembered project answer, so it is asked again
        state, gone = load_state(), []
        for part in ("repos", "folders"):
            for key in [k for k in state.get(part, {}) if any(i.replace("\\", "/").lower() in k for i in items)]:
                gone.append(f"{key} ({state[part].pop(key)})")
        save_state(state)
        print("forgot: " + "; ".join(gone) if gone else "nothing remembered matches - see: timesheet config test")
        return 0
    if action == "set-secret":
        return set_secret()
    return config_test()


def cmd_scheduled(args) -> int:
    """Run by the daily job every 15 minutes. `remind` pops up when yesterday has no timesheet;
    `auto` creates yesterday's draft (never posting issues). Each failure pops up once a day."""
    mode, today = cfg("SCHEDULE"), now().date()
    if mode == "off":
        return 0
    state = load_state()
    sched = state.get("sched") or {}
    if sched.get("date") != today.isoformat():
        sched = {"date": today.isoformat(), "done": None, "alerted": []}
    if sched["done"]:
        return 0  # today already handled; don't touch the site again
    state["sched"] = sched
    day = today - dt.timedelta(days=1)
    try:
        emp = connect(network_wait=300)
        why = skip_reason(emp, day)
        sheet = None if why else find_sheet(emp, day)
        if why or sheet:
            sched["done"] = why or f"{sheet['name']} exists"
        elif mode == "remind":
            sched["done"] = "reminded"
            if flag("NOTIFY"):
                popup("No timesheet for yesterday", f"{fmt_day(day)} has no timesheet yet.\n\n"
                      f"Build the draft with:  timesheet create --date {day}", f"{cfg('TS_URL')}/app/timesheet")
        else:
            args.date, args.yes, args.scheduled = day, True, True
            args.note, args.force, args.no_conversations, args.submit = None, False, False, False
            cmd_create(args)
            sched["done"] = "created"
        log(f"scheduled ({mode}) for {day}: {sched['done']}")
        save_state(state)
        return 0
    except NetworkNotReady as e:
        log(f"{e}; will retry on the next run")
        return 1
    except TSError as e:
        log(f"error: {e.detail + ': ' if e.detail else ''}{e}")
        message = str(e)[:300]
        if message not in sched["alerted"]:
            sched["alerted"].append(message)
            save_state(state)
            popup("Timesheet job FAILED", f"{message}\n\nIt retries every 15 minutes once you fix this.",
                  f"{cfg('TS_URL')}/app/timesheet", error=True)
        return 1


# ---------------------------------------------------------------- command line

def a_date(value: str) -> dt.date:
    today = now().date()
    if value.lower() in ("today", "yesterday"):
        return today - dt.timedelta(days=value.lower() == "yesterday")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a date - use YYYY-MM-DD, today or yesterday") from None


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--date", type=a_date, help="YYYY-MM-DD, today or yesterday (default: yesterday)")
    common.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    common.add_argument("--yes", action="store_true", help="don't ask y/N")
    common.add_argument("--polish", action="store_true", help="tidy typed text through Claude Code")
    common.add_argument("--raw", action="store_true", help="store typed text exactly as typed")
    common.add_argument("--amend", action="store_true",
                        help="on a submitted sheet: cancel it and create the amended copy as a draft")
    ap = argparse.ArgumentParser(prog="timesheet", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")

    def cmd(name: str, fn, about: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=about, description=about)
        p.set_defaults(fn=fn, scheduled=False)
        return p

    def row_fields(p) -> None:
        p.add_argument("--from", dest="frm", help="start, 24h HH:MM")
        p.add_argument("--to", help="end, 24h HH:MM")
        p.add_argument("--description")
        p.add_argument("--type", help=f"activity type, or one of: {', '.join(WORK_KINDS)}")
        p.add_argument("--task", help="TASK-... to link the row to")

    p = cmd("create", cmd_create, "Build tasks, issues and the draft timesheet for one date")
    p.add_argument("--note", action="append", metavar='"13:20-16:11 | Testing | what you did"',
                   help="a fixed row for work with no trace; repeatable")
    p.add_argument("--force", action="store_true", help="build even on a holiday, leave or non-work day")
    p.add_argument("--no-conversations", action="store_true", help="use GitHub only")
    p.add_argument("--submit", action="store_true", help="after saving the draft, submit it (asks first)")
    cmd("submit", cmd_submit, "Submit the day's draft timesheet (asks first)")
    cmd("show", cmd_show, "List the day's rows, numbered")
    p = cmd("update", cmd_update, "Change a row by prompts or flags, or re-derive rows with --regen")
    p.add_argument("--row", help="row number from `show` (several for --regen: 3,5,7)")
    row_fields(p)
    p.add_argument("--regen", action="store_true", help="rebuild the rows from GitHub and conversations")
    p.add_argument("--only", help="with --regen: fields that may change (from,to,description,activity_type,task)")
    p = cmd("add-row", cmd_add_row, "Add a manual row with no issue behind it")
    row_fields(p)
    p.add_argument("--repo", help="owner/name, or a short name already seen (console): the row goes to its project")
    p.add_argument("--project", help="site project for the row, when it belongs to no repo")
    cmd("delete-row", cmd_delete_row, "Remove rows").add_argument("--row", required=True)
    cmd("delete", cmd_delete, "Delete a draft timesheet")
    p = cmd("start", cmd_start, "Create a task, then its GitHub issue, and start the clock")
    p.add_argument("text", help="what you are starting")
    p.add_argument("--repo", help="owner/name or a short name already seen (default: the repo of the folder you are in)")
    p.add_argument("--type", help=f"one of: {', '.join(WORK_KINDS)} (default: guessed from the text)")
    p.add_argument("--at", help="the real start time, if you forgot to start: HH:MM today")
    cmd("finish", cmd_finish, "Close the issue, add the row, complete the task").add_argument(
        "--at", help="the real end time, if you forgot to finish: HH:MM today")
    cmd("status", cmd_status, "Show what is currently started")
    cmd("issues", cmd_issues, 'Create the tracking issues for rows marked "no issue yet"')
    p = cmd("task", cmd_task, "Work on tasks alone")
    p.add_argument("action", choices=("create", "update", "list"))
    p.add_argument("name", nargs="?", help="TASK-... (for update)")
    p.add_argument("--subject")
    p.add_argument("--desc")
    p.add_argument("--hours", type=float)
    p.add_argument("--status")
    p.add_argument("--parent", help="parent TASK-...")
    p.add_argument("--repo", help="owner/name or a short name: sets the project; with --type, lists its GitHub activity")
    p.add_argument("--project", help="site project for a free-form task (default: the repo's, else asked)")
    p.add_argument("--type", help=f"one of: {', '.join(TASK_TYPES)}")
    p = cmd("config", cmd_config, "Settings: guided setup when run alone")
    p.add_argument("action", nargs="?", choices=("list", "get", "set", "unset", "set-secret", "test", "forget"))
    p.add_argument("items", nargs="*", help="KEY=value for set; KEY for get and unset; "
                                            "part of a repo or folder name for forget")
    cmd("scheduled", cmd_scheduled, "Run by the daily job (see SCHEDULE)")
    return ap


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows console defaults to cp1252; pythonw has no console
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args()
    args.date_given = args.date is not None
    args.date = args.date or now().date() - dt.timedelta(days=1)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\nstopped; nothing more was written.")
        return 130
    except TSError as e:
        log(f"error: {e.detail + ': ' if e.detail else ''}{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
