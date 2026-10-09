# timesheet-autofill

Fills in your daily timesheet for you.

At the end of a day (or the next morning) you run one command. The tool looks at what you actually did, writes the timesheet rows and the tasks behind them on your company's project site, and shows you everything before saving. You check it, say yes, and it's done.

It works with any company site built on Frappe / ERPNext, such as `projects.8848digital.com`.

## What it does for you

- **Works out what you did.** It reads two things:
  - your GitHub activity for the day: the changes you finished, the changes of others you reviewed, the issues you closed and the issues you raised;
  - your Claude Code conversations on this computer: what you worked on, in which project folder, and from when to when.
- **Writes the timesheet.** Each piece of work becomes a row with a start time, an end time, a type of work and a short description.
- **Creates the tasks.** Every row is linked to a task on the site. Missing tasks are created; existing ones are reused, never duplicated.
- **Puts work under the right project.** You never type a project name. The tool learns it from your earlier tasks, and asks once for anything it has not seen before.
- **Keeps a record on GitHub.** For work that has no GitHub issue behind it, it can open a small tracking issue, if you say yes.
- **Lets you correct anything.** Change a row, add one, remove one, or throw the day away and start again.

## What it will not do without asking

- **Save anything.** Every change is shown first and waits for your `y`.
- **Submit a timesheet.** It saves a draft. It submits only when you ask it to, and asks again before doing so.
- **Post on GitHub.** Each issue it wants to open is listed first.
- **Share your conversations.** Only a one-line summary of a conversation is ever written anywhere. The conversation itself stays on your computer.
- **Store your password in a file.** It is kept in your computer's own password store (Windows Credential Manager or the macOS Keychain).

## Before you start

You need:

1. **Python** (version 3.9 or newer). On Windows, install it from [python.org](https://www.python.org/downloads/) and tick "Add python.exe to PATH". On a Mac it is already there.
2. **The GitHub command-line tool**, signed in. Install it from [cli.github.com](https://cli.github.com), then run `gh auth login` once.
3. **Your site address, email and password.** If you normally sign in with Google, set a password on the site first.
4. **Optional: Claude Code.** With it, the tool can also read your conversations and tidy your wording. Without it, the tool uses GitHub only.

## Install

Put this folder somewhere permanent, open a terminal in it, and run the installer.

**Windows** (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

**Mac** (Terminal):

```bash
./install.sh
```

It asks a few questions (site address, email, the days and hours you work, your password), checks that the login works, and finishes. It does not ask about projects or repositories.

**After installing, open a new terminal window.** The `timesheet` command only works in terminals opened after the install. In the old window you can still type `.\timesheet` (Windows) or `./timesheet` (Mac) from inside this folder.

## The everyday routine

Most days you need only these three steps.

**1. Preview the day.** Nothing is saved.

```bash
timesheet create --dry-run
```

**2. Create it.** You see the same preview and answer `y` to save the draft.

```bash
timesheet create
```

**3. Submit it** once you are happy with it.

```bash
timesheet submit
```

All three work on **yesterday** unless you name a date:

```bash
timesheet create --date 2026-10-08
```

```bash
timesheet create --date today
```

## Every command

A date is written `2026-10-08`, or as the words `today` or `yesterday`. Leave `--date` out and the command works on yesterday.

### Building and checking a day

#### `timesheet create`

Builds the day: the rows, the tasks, and the draft timesheet.

**Use it when** you want a day's timesheet filled in. Run it again later on the same day and it adds what is new, leaving the rows that are already there untouched.

```bash
timesheet create --date 2026-10-08
```

Useful additions:

| Add this | What it does | When to use it |
|---|---|---|
| `--dry-run` | Shows the result and saves nothing | Always worth doing first |
| `--submit` | Offers to submit the timesheet after saving it | When you trust the result and want one step |
| `--note "..."` | Adds a row you describe yourself | For work that left no trace, such as testing by hand |
| `--no-conversations` | Uses GitHub only | When you want a quicker run, or only the GitHub work |
| `--force` | Builds the day even on a holiday, a leave day or a weekend | When you did work on such a day |

A note is written as time, type of work, and what you did, separated by `|`:

```bash
timesheet create --date 2026-10-08 --note "13:20-16:11 | Testing | Manual testing of the console and portal in the browser"
```

#### `timesheet show`

Lists the day's rows with a number in front of each.

**Use it when** you want to read a day's timesheet, or need a row's number to change or remove it.

```bash
timesheet show --date 2026-10-08
```

#### `timesheet submit`

Shows the day's timesheet and its total hours, asks `y/N`, then submits it.

**Use it when** the draft is correct and you are ready to hand it in. After this, the sheet can only be changed with `--amend` (see below).

```bash
timesheet submit --date 2026-10-08
```

### Correcting a day

#### `timesheet update --row N`

Changes one row. With nothing else on the line, it asks you field by field and shows the current value in brackets. Press Enter to keep a value, or type a new one.

**Use it when** a time or a description is wrong.

```bash
timesheet update --date 2026-10-08 --row 3
```

```
from [14:00]:
to [15:30]: 16:00
description [Manual testing of the portal]: retested invoices
activity type [Testing]:
task [TASK-2026-12537]:
```

Or say exactly what to change, and only that changes:

```bash
timesheet update --date 2026-10-08 --row 3 --from 09:30 --to 11:00 --description "Retested invoices"
```

You can also pass `--type` (the type of work) and `--task` (a task number).

#### `timesheet update --regen`

Rebuilds rows from GitHub and your conversations again.

**Use it when** the rows were filled in by hand or are out of date, and you want the tool's version instead.

```bash
timesheet update --date 2026-10-08 --regen
```

To rebuild only some rows, or only some parts of them:

```bash
timesheet update --date 2026-10-08 --regen --row 3,5 --only description,task
```

#### `timesheet add-row`

Adds one row that you describe yourself.

**Use it when** the timesheet already exists and you remember something that is missing, such as a meeting or testing done by hand.

```bash
timesheet add-row --date 2026-10-08 --from 13:20 --to 16:11 --type testing --description "Manual testing in the browser"
```

Leave the details out and it asks for them one by one. Add `--repo console` or `--project "Strategic Gears"` to say which project the row belongs to; otherwise it uses the project of the day's other rows, or asks.

#### `timesheet delete-row --row N`

Removes a row.

**Use it when** a row should not be there at all.

```bash
timesheet delete-row --date 2026-10-08 --row 4
```

#### `timesheet delete`

Deletes the whole draft for a day.

**Use it when** you want to start that day again from nothing. It only works on a draft, never on a submitted timesheet.

```bash
timesheet delete --date 2026-10-08
```

#### Changing a timesheet you already submitted

A submitted timesheet is locked. Add `--amend` to `update`, `add-row` or `delete-row`, and the tool cancels the submitted sheet and creates a corrected copy as a new draft, under a new number. You then submit that copy.

**Use it when** you spot a mistake after submitting.

```bash
timesheet update --date 2026-10-06 --row 2 --description "Corrected description" --amend
```

### Timing work as you do it

These two are optional. They are for work you want timed exactly, and recorded on GitHub while it happens.

#### `timesheet start "what you are doing"`

Creates a task, opens a GitHub issue for it, and starts the clock.

**Use it when** you begin a piece of work that would otherwise leave no trace: testing by hand, research, a long investigation.

```bash
timesheet start "manual testing of the portal"
```

Run it from inside the project's folder and it knows which project the work is for. From anywhere else, name the repository with `--repo console`. If you forgot to start on time, give the real time with `--at 09:30`.

#### `timesheet finish`

Stops the clock, adds the row to that day's timesheet, closes the issue and marks the task as completed.

**Use it when** you stop that piece of work. If you start testing again later, run `start` again; it becomes a new task.

```bash
timesheet finish
```

If you forgot to finish on time: `timesheet finish --at 11:00`.

#### `timesheet status`

Shows what is currently started, and for how long.

**Use it when** you are not sure whether you left the clock running.

```bash
timesheet status
```

### GitHub tracking issues

#### `timesheet issues`

Opens the tracking issues for rows that do not have one yet. Such rows carry the mark `[no issue yet]`.

**Use it when** you said no to the issues while creating the day, or the day was built automatically, and you now want them opened.

```bash
timesheet issues --date 2026-10-08
```

### Tasks on their own

For when you want a task without touching a timesheet.

#### `timesheet task list`

Shows your latest 20 tasks.

**Use it when** you need a task number.

```bash
timesheet task list --date 2026-10-08
```

#### `timesheet task create`

Creates a task. Either describe it yourself:

```bash
timesheet task create --subject "Prepare the launch checklist" --hours 3 --desc "Checklist for the October launch" --project "Strategic Gears"
```

or let the tool build it from one repository's GitHub activity for the day. The kinds are `fixes`, `review`, `issues` and `testing`:

```bash
timesheet task create --repo console --type review --date 2026-10-08
```

**Use it when** you need the task now and will fill in the timesheet later.

#### `timesheet task update`

Changes a task's status, title, description or hours.

**Use it when** a task is still marked as being worked on and you have finished it.

```bash
timesheet task update TASK-2026-12537 --status Completed
```

Tasks cannot be deleted with this tool. Do that on the site.

### Settings

#### `timesheet config`

Runs the setup questions again.

**Use it when** you want to change several settings at once.

```bash
timesheet config
```

#### Looking at and changing single settings

| Command | What it does |
|---|---|
| `timesheet config list` | Shows every setting, its value and what it means |
| `timesheet config get DAY_START` | Shows one setting |
| `timesheet config set DAY_START=10:00` | Changes a setting. A wrong value is refused and nothing changes |
| `timesheet config unset DAY_START` | Puts a setting back to its usual value |

The settings you are most likely to change:

| Setting | Meaning | Usual value |
|---|---|---|
| `DAY_START` | When your working day begins | `09:30` |
| `LUNCH` | Your lunch break | `13:00-14:00` |
| `WORK_DAYS` | The days you work | `Mon,Tue,Wed,Thu,Fri` |
| `IDLE_MINUTES` | How long a pause in a conversation counts as "stopped working" | `15` |
| `SCHEDULE` | Whether anything runs on its own (see below) | `off` |
| `POLISH` | Always tidy the text you type | `false` |

`.env.example` in this folder explains every setting.

#### `timesheet config set-secret`

Saves a new password. Your typing is hidden.

**Use it when** you have changed your password on the site.

```bash
timesheet config set-secret
```

#### `timesheet config test`

Checks that the tool can sign in to the site and to GitHub, and lists which project each repository and folder is filed under. It changes nothing.

**Use it when** something is not working and you want to know why.

```bash
timesheet config test
```

#### `timesheet config forget`

Makes the tool forget which project a repository or folder belongs to, so it asks you again next time.

**Use it when** you picked the wrong project by mistake.

```bash
timesheet config forget timesheet-autofill
```

### Extras you can add to any command that saves something

| Add this | What it does | When to use it |
|---|---|---|
| `--dry-run` | Shows what would happen, saves nothing | Whenever you are unsure |
| `--yes` | Does not ask `y/N` | When you are certain and want it quick |
| `--polish` | Rewrites the text you typed into clean timesheet wording, using Claude Code | When you typed rough notes |
| `--raw` | Keeps your text exactly as typed | When polishing is always on and you want to skip it once |
| `--amend` | Works on a timesheet that is already submitted | When correcting after submitting |

For example, rough notes turned into a clean row:

```bash
timesheet add-row --from 10:00 --to 11:30 --type testing --description "tested console n portal by hand, found 2 bugs" --polish
```

## Which command when

| You want to... | Run |
|---|---|
| See what yesterday's timesheet would look like | `timesheet create --dry-run` |
| Fill in yesterday's timesheet | `timesheet create` |
| Fill in a particular day | `timesheet create --date 2026-10-08` |
| Fill it in and submit in one go | `timesheet create --submit` |
| Hand in a draft | `timesheet submit` |
| Read a day's timesheet | `timesheet show` |
| Fix a time or a description | `timesheet update --row N` |
| Add work the tool could not see | `timesheet add-row` |
| Remove a row | `timesheet delete-row --row N` |
| Start a day again from nothing | `timesheet delete`, then `timesheet create` |
| Correct a timesheet after submitting it | `timesheet update --row N --amend` |
| Time a piece of work exactly | `timesheet start "..."`, then `timesheet finish` |
| Check whether the clock is running | `timesheet status` |
| Open the tracking issues you skipped | `timesheet issues` |
| Mark a task as completed | `timesheet task update TASK-... --status Completed` |
| Fix a wrong project choice | `timesheet config forget <name>` |
| Save a new password | `timesheet config set-secret` |
| Find out why something fails | `timesheet config test` |

## How it decides things

**The times.** Work of the same kind done back to back becomes one row. A row runs from where the previous one ended (or from the start of your day, or the end of lunch) up to the moment the work was finished. Rows you wrote yourself are never moved; the tool fits its rows around them.

**One day per timesheet.** A timesheet only holds 00:00 to 23:59 of its own date. If you worked past midnight, that part shows up when you build the next day.

**The type of work.**

| What happened | Type of work on the row |
|---|---|
| A change of your own was finished | Customization / Development |
| You reviewed and accepted someone else's change | Code Review |
| You closed an issue | Issue handling |
| You raised an issue, or ran tests | Testing |

**The project.** The tool looks at the tasks you already have on the site. If earlier tasks for a repository sit under "Strategic Gears", new work on that repository goes there too, with the same style of title. For a repository or folder it has never seen, it shows the list of projects and asks once. If it cannot ask (for example when run automatically), it leaves that work out and tells you, instead of guessing.

**Days it skips.** Holidays, days you are on leave, and days outside your working week. Add `--force` to build one anyway. A timesheet that is already submitted is left alone.

**Gaps.** Time it could not explain is listed at the end as a gap, with the command to fill it. Work done by hand, away from GitHub and Claude Code, is the usual reason.

**Conversations it cannot read.** Only Claude Code conversations on this computer (the terminal and the desktop app's Code tab). It cannot see the desktop app's Chat tab, claude.ai in a browser, or sessions run in the cloud.

## Letting it run on its own

By default nothing runs by itself. You can change that with the `SCHEDULE` setting:

| Value | What happens |
|---|---|
| `off` | Nothing runs on its own |
| `remind` | A popup reminds you when yesterday has no timesheet |
| `auto` | Yesterday's draft is created for you |

```bash
timesheet config set SCHEDULE=remind
```

Even on `auto` it never submits and never posts on GitHub. Rows that would have had an issue are marked `[no issue yet]`, for you to open later with `timesheet issues`.

## When something goes wrong

| What you see | What to do |
|---|---|
| `timesheet` is "not recognized" | Open a new terminal window, or type `.\timesheet` from inside this folder |
| `no saved password for ...` | Run `timesheet config set-secret` |
| `Invalid login credentials` | Your password changed. Run `timesheet config set-secret` |
| `... is not set - run: timesheet config` | Run `timesheet config` |
| `gh ... failed` | Run `gh auth login`, then `timesheet config test` |
| `... is submitted. Add --amend` | The timesheet is locked. Add `--amend` to correct it |
| `row ... overlaps row ...` | Two rows cover the same time. Change one of them |
| `Expected Start Date cannot be after Project ...'s Expected End Date` | That project is closed for new tasks on the site. If you picked it by mistake, run `timesheet config forget <name>` |
| A message from the site you do not understand | Nothing half-finished is left on the timesheet. Read the message; it comes from the site's own rules |

The file `timesheet.log` in this folder keeps a record of what each run did.

## Removing it

**Windows:** `powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall`

**Mac:** `./install.sh --uninstall`

This stops the automatic job and offers to delete the saved password. Your timesheets and tasks on the site are not touched.

## Credits

Based on [hrms-attendance](https://github.com/DhavalGala999/hrms-attendance) by Dhaval Gala, which creates daily attendance requests in the same way.
