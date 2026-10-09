# timesheet-autofill

Builds a day's **tasks and draft timesheet** on a Frappe / ERPNext site from your GitHub activity and your Claude Code conversations, opens GitHub issues for work that has none, and lets you edit everything from the terminal.

It never submits a timesheet, and every write is shown for a y/N first.

A fork of [hrms-attendance](https://github.com/DhavalGala999/hrms-attendance): same login, same plumbing, same installers.

## Requirements

- Python 3.9 or newer
  - **macOS:** the built-in `python3` works.
  - **Windows:** install Python from [python.org](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**.
- The [GitHub CLI](https://cli.github.com), signed in (`gh auth login`).
- Your site address, email and password. If you sign in with Google, set a site password first.
- Optional: [Claude Code](https://claude.com/claude-code), for reading your conversations and for `--polish`. Without it, GitHub is the only source.

## Install

Put this folder somewhere permanent, then run the installer.

**Windows** (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

**macOS** (Terminal):

```bash
./install.sh
```

The installer finds Python, installs `keyring`, creates the `timesheet` command and runs `timesheet config`, which asks for your settings and password, tests the login, and sets up the daily job if you chose one.

Your password is saved in Windows Credential Manager or the macOS Keychain. It is never written to a file, and the tool uses no API key.

## Commands

Dates are `YYYY-MM-DD`, `today` or `yesterday`. Without `--date`, every command works on **yesterday**.

| Command | What it does |
|---|---|
| `timesheet create [--date] [--note ...]` | Builds tasks, issues and the draft timesheet for one date |
| `timesheet show [--date]` | Lists the day's rows, numbered |
| `timesheet update --row N` | Changes a row, by prompts or by flags |
| `timesheet update --regen [--row N,N] [--only fields]` | Re-derives rows from the sources |
| `timesheet add-row` | Adds a manual row with no issue behind it |
| `timesheet delete-row --row N` | Removes a row |
| `timesheet delete` | Deletes a draft timesheet |
| `timesheet start "text" [--repo] [--type] [--at]` | Creates a task, then its GitHub issue, and starts the clock |
| `timesheet finish [--at]` | Closes the issue, adds the row, completes the task |
| `timesheet status` | Shows what is currently started |
| `timesheet issues` | Creates the issues for rows marked "no issue yet" |
| `timesheet task create / update / list` | Works on tasks alone |
| `timesheet config [list / get / set / unset / set-secret / test]` | All settings |

Flags on every writing command:

| Flag | Effect |
|---|---|
| `--dry-run` | Shows what would happen and changes nothing |
| `--yes` | Skips the y/N |
| `--polish` / `--raw` | Tidies typed text through Claude Code / stores it exactly as typed |
| `--amend` | On a submitted sheet: cancels it and creates the amended copy as a draft under a new number |

## How `create` builds a day

Sources, in order of priority:

| Source | Gives |
|---|---|
| Rows already in the draft, and `--note` entries | Fixed rows that nothing overwrites |
| Tracked issues from `start` / `finish` | A row from the issue's start and finish times |
| GitHub | PRs merged, issues you closed, issues you raised |
| Claude Code conversations on this machine | What you worked on, where, and when |

- **Skipped days:** holidays, leave days and non-work days (`--force` builds them anyway). A submitted sheet is left alone; an existing draft is kept and filled around.
- **GitHub events become rows by kind:**

| Event | Task | Activity type |
|---|---|---|
| Your own PR merged | `... - Own fixes raised and merged - 7 Oct 2026` | Customization / Development |
| Someone else's PR you merged | `... - PR Review & Merge - ...` | Code Review |
| Issue you closed | `... - GitHub Issues Resolved - ...` | Issue handling |
| Issue you raised | `... - UAT Testing - ...` | Testing |

- **Row times:** consecutive events of the same repo and kind share a row. A row runs from the previous row's end (or day start, or lunch end) to its last event, and is trimmed around the fixed rows.
- **One date per sheet:** rows sit between 00:00:00 and 23:59:59. Work past midnight is cut there, and the rest appears only when you run `create` for the next date.
- **Conversations:** each session is split into blocks, ending a block after 15 idle minutes. A session whose branch has a PR merged that day, or that names one of the day's PRs or issues, is already covered by those rows. A session on a branch with an open PR becomes "Worked on PR #N" rows. Anything else gets a one-line summary from `claude -p`, its own task, and a proposed tracking issue.
- **Tracking issues:** each proposed issue is listed with its repo, title and time window, and you choose all, none, or one by one. An issue is opened with its times in the body and closed straight away. Rows you decline keep the mark `[no issue yet]`; `timesheet issues` creates them later.
- **Tasks:** one per repo and kind of work per day, looked up by subject first so re-runs never duplicate. Expected time is the sum of the task's rows.
- **Report:** gaps with no source, and work left for the next day.

Work done by hand, with no Claude session and no GitHub activity, shows as a gap. Fill it with a note or a row:

```bash
timesheet create --date 2026-10-08 --note "13:20-16:11 | Testing | Manual testing of the console and portal in the browser"
```

```bash
timesheet add-row --date 2026-10-08 --from 13:20 --to 16:11 --type testing --description "Manual testing in the browser"
```

## Timed work: `start` and `finish` (optional)

```bash
timesheet start "manual testing" --repo portal
```

```bash
timesheet finish
```

- **`start`** creates the task as Working, then opens a GitHub issue for it, each linking to the other.
- **`finish`** adds the row to that date's draft, closes the issue, marks the task Completed with its time window in the name, and lists the bugs you raised in that repo meanwhile.
- **Forgot to start or finish on time:** `--at 09:30` records the real time in the issue, and that is used over GitHub's timestamps.
- **Closed the issue on GitHub by hand:** `create` still picks it up.
- **No `--repo`:** the issue goes to `TRACKING_REPO`.

## Updating a row

Prompted, one field at a time. Enter keeps the value in brackets:

```
timesheet update --date 2026-10-08 --row 3
from [14:00]:
to [15:30]: 16:00
description [Manual testing of the portal]: retested invoices
activity type [Testing]:
task [TASK-2026-12537]:
```

Or directly, changing only the fields you pass:

```bash
timesheet update --date 2026-10-08 --row 3 --description "retested invoices" --from 09:30 --to 11:00
```

Every update checks for overlaps and for crossing midnight, shows a before/after diff, recalculates the task's expected time, and re-reads the sheet afterwards to confirm that no other row moved.

## Settings

```bash
timesheet config list
```

```bash
timesheet config set POLISH_HINT=false LUNCH=13:30-14:30
```

### Projects follow the repo

Each entry in `REPOS` names the site project its work is filed under, so one day can hold rows for several projects:

```
key=owner/name|Label|merge branch|Project|Subject prefix
```

```bash
timesheet config set "REPOS=console=strategic-gears/sg-network-internal-console|Internal Console|staging|Strategic Gears|SG Network,ada=acme/ada-web|Ada Web|main|ADA"
```

- **GitHub activity and conversations** take the project of the repo they happened in. A conversation belongs to a repo when its folder, or that folder's git remote, is that repo.
- **Testing rows** are one task per project per day.
- **`PROJECT`** is only the default, for repos that name no project and for work in folders outside every repo. Leave it empty and that work is left out and listed in the report, instead of being filed under the wrong project.
- **Manual work:** `add-row`, `--note` (`"13:20-16:11 | Testing | text | ada"`) and `start` take a repo key; a row given `--task` follows that task's project.

`.env.example` describes every setting. A bad value or an unknown setting is refused and nothing is written. Changing the site address or email re-tests the login.

After changing your site password:

```bash
timesheet config set-secret
```

### Daily job

`SCHEDULE` is your choice:

| Value | What happens |
|---|---|
| `off` (default) | Nothing runs on its own |
| `remind` | A popup when yesterday has no timesheet, with the command to run |
| `auto` | Creates yesterday's draft. It never posts issues: rows are marked `[no issue yet]` for `timesheet issues` |

The job checks every 15 minutes and stops for the day once yesterday is handled. `timesheet config set SCHEDULE=...` registers or removes it.

## What leaves your machine

- **To the site:** tasks and timesheet rows, after you confirm.
- **To GitHub:** tracking issues, after you confirm. Only the one-line summary is posted, never raw conversation text, and a summary that contains a key-like string is replaced with a generic line.
- **To Claude Code (`claude -p`, your own login):** your prompts and Claude's closing message for the sessions being summarised, and text you ask to `--polish`.
- **Not readable:** the desktop app's Chat tab, claude.ai in the browser, and cloud sessions.

## Troubleshooting

| Message | Fix |
|---|---|
| `no saved password for ...` | `timesheet config set-secret` |
| `Invalid login credentials` | Your password changed: `timesheet config set-secret` |
| `... is not set - run: timesheet config` | Run `timesheet config` |
| `gh ... failed` | `gh auth login`, then `timesheet config test` |
| `... is submitted. Add --amend` | The sheet is submitted; `--amend` cancels it and creates a draft copy |
| `row ... overlaps row ...` | Change the times, or remove the other row first |
| `'Research' is not an activity type on the site` | Use one of the listed types, or change `DEFAULT_ACTIVITY` |

`timesheet.log` in this folder records what each run did. To check the row-building logic: `python test_timesheet.py`.

To uninstall: `install.ps1 -Uninstall` on Windows, `./install.sh --uninstall` on macOS.
