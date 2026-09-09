# agentgrid

A local, zero-dependency dashboard over every Claude Code session on your
machine. It answers three questions at a glance — **which agent needs me**,
**which is still working**, and **what is each one actually doing** — and does
the two things that follow: start a new agent in any repo, and get into a
session that wants you. Because it is already the place you sit while agents
run, it also carries a daily notepad whose checkbox lines are the todo list.

Two front ends, one data model:

**The board** — `ag --web`, a browser page with a column per state. Cards
physically travel between columns as sessions change state.

```
 agentgrid   Board · Split · Notes    All projects ▾  All tags ▾  Done 3  Idle 15   New agent  ⌕
 ───────────────────────────────────────────────────────────────────────────────────────────────
  NEEDS YOU 2              WORKING 4                REPLIED 3
 ───────────────────────────────────────────────────────────────────────────────────────────────
  ▌ fix the flaky tests     ● slack update gen       ▌ pr review ranking
    agentgrid · 41m           ai-native-qa-ui · 2m       fusion-ui · 12m
                                                    ───────────────────────
  ▌ investigate FUSAI-857   ● migrate the fixtures    docs sweep
    fusion-core · 1h          fusion-core · 8m          agentgrid · 3h
```

**The grid** — `ag`, a curses grid in the terminal, drillable into each
session's subagents and transcripts.

```
┏━ ai-native-qa-ui ━━━━━━━━━━━━━━  NEEDS YOU  ┓  ╭─ fusion-core · bg ─────────────╮
┃ slack update gen                     2m ago ┃  │ ⠧ migrate the fixtures   8m ago│
┃ exp/dec/tc11-descriptions-luna-low          ┃  │ main                           │
┃ › ohh okay sure let's do a .gitignore for   ┃  │ › port the fixture loader to   │
┃ Bash 25 · Read 4 · Write 2                  ┃  │ Bash 12 · Edit 7 · Read 5      │
┃  > Explore  find the ranking code    10msg  ┃  │  . Explore  map the fixtures   │
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛  ╰────────────────────────────────╯
```

The board is the everyday front end; the grid exists for when you are already
in a terminal (or on the other end of an ssh connection) and for the subagent
drill-down. They are interchangeable and read the same sources.

*Why this is the way it is.* The operating system offers only window and tab
titles to tell twenty sessions apart, and a title carries no status: a session
blocked at a permission prompt looks exactly like one that finished twenty
minutes ago. Most sessions on the original author's machine live in Cursor,
whose terminal tabs exist only in renderer memory — there is no list to look at
and nothing to click. agentgrid is the missing list.

---

## Requirements

- macOS or Linux (the Terminal.app integration is macOS-specific and degrades
  to a printed instruction elsewhere)
- Python 3.10 or newer
- the `claude` CLI on `PATH`
- optionally, Terminal.app as your interactive terminal, for tab focusing

There are **no other dependencies**. No pip packages, no virtualenv, no build
step, no npm. Standard library only, in both front ends.

*Why this is the way it is.* On the corporate network this was built for,
`files.pythonhosted.org` is blocked outright and TLS is intercepted by a proxy
whose root certificate is absent from the keychain, so `pip install` fails. A
tool that needed installing would not have been installable. Standard library
only means a clone is a working install, everywhere, forever.

## Install

```bash
git clone <this repo>
cd agentgrid
chmod +x bin/ag
ln -s "$PWD/bin/ag" ~/.local/bin/ag     # or anywhere on your PATH
```

`bin/ag` resolves its own symlink chain, so it can be linked from anywhere.
No symlink at all also works:

```bash
python3 -m agentgrid          # from the repo root
```

## Run

```
ag                      # terminal grid
ag --web                # browser board, opens automatically
ag --filter active      # all | active | needs you | recent
ag --cwd ~/myrepo       # only sessions under a directory
ag --no-mouse           # keep the terminal's own text selection
ag --refresh 5          # poll interval in seconds
ag --web --port 9000    # pick the port (falls back to a free one if taken)
ag --web --no-open      # do not launch a browser
ag --web --root ~/work  # scan somewhere else for projects (repeatable)
```

`ag --web` prints the URL (which carries the access token — see
[Security](#security)), the roots it scanned for projects, and a one-line
statement of the security posture. Nothing runs in the background: both front
ends stop when you close them, and no port is left listening after Ctrl-C.

---

## Sessions and the state model

Every fact on a card comes from an interface Claude Code already exposes —
the CLI's JSON or the session transcript on disk. Nothing is scraped from a
rendered screen.

| Field on a card | Source |
|---|---|
| status, kind, cwd, pid, job id, startedAt | `claude agents --json --all` |
| title | `type:"ai-title"` in the transcript (`aiTitle`) |
| branch, model | transcript entry fields |
| last prompt | `type:"last-prompt"` (`lastPrompt`) |
| tool counts | `tool_use` blocks in the transcript |
| subagent roster | `<sessionId>/subagents/agent-*.jsonl` |
| subagent names | parent's `Agent` tool_use `input.description` |

Background sessions report a `state` (`working` / `blocked` / `done` /
`failed` / `stopped`); interactive sessions report a `status` (`busy` /
`idle`). agentgrid collapses both onto one vocabulary in one place.

Neither field, on its own, answers "is it working right now": `state` can
stick at `working` after a job's turn has ended, and `status` says `busy`
whenever any task is in flight — a stray shell winding down keeps a finished
job in Working. So for background sessions the daemon's own activity signal,
`tempo` (`active` / `idle`) in `~/.claude/jobs/<id>/state.json`, decides
working-or-not; the fields above remain the fallback for interactive sessions,
which have no job directory. `blocked`, `failed` and `stopped` are never
overridden by tempo.

None of those signals catches a *hung* agent — one whose `state`/`status`/
`tempo` still claim it is working while its transcript has gone silent. So a
card still reported working but silent past a threshold (15 min by default)
carries an advisory **⚠ quiet 18m** badge. It is advisory on purpose: it never
moves the card out of Working. Measured over real transcripts, P99.9 of the
gaps between writes *during genuine work* is ~12.6 min, so 15 min sits just
past it — but the tail is long (a slow build or a sleeping laptop can be
legitimately silent for hours), and reclassifying a live agent as settled is
the one mistake the board refuses to make. The badge flags the suspicion and
leaves the verdict to you.

A session's display name falls back through four sources, in order: a name you
typed, the AI-generated title, the CLI's auto-generated name, the first eight
characters of the session id.

Cards are ordered by status first — `blocked`, `working`, `failed`, `done`,
`idle`, `complete`, `stopped` — then by most recent activity within each band.
Polling is every 2 seconds, on a worker thread in both front ends.

*Why this is the way it is.* A session you opened a minute ago and a session
blocked on a prompt for an hour are not equally urgent, so this is not a plain
"recently active" list. Transcripts are parsed incrementally from a stored byte
offset because they are append-only and can get very long; re-reading in full
on every poll would scale with conversation length rather than with what
changed.

## Codex sessions

The board watches Codex agents alongside Claude ones. There is no fleet
command to ask, so everything comes from what the Codex CLI writes itself:
sessions are discovered from the rollout files under `~/.codex/sessions/`,
titles from Codex's own `session_index.jsonl`, and the model, last prompt and
tool counts from the rollout entries. Status is inferred from the rollout's
own record — a session whose last `task_started` has no matching
`task_complete` and is still being written is **Working**; one whose last task
completed is **Replied**; an open task that has gone quiet was interrupted and
files as stopped. Sessions leave the board after a day without activity.

Codex cards carry a `· codex` marker and behave like any other card: rename,
tags, moves, unread and `@mentions` all work, and the transcript renders in
the panel. What they cannot do is be joined — a Codex TUI's keyboard belongs
to the terminal that owns it and there is no attach command, so Open says why
rather than guessing.

New agents can be started with either engine — see below.

*Why this is the way it is.* Reading the CLI's own files keeps the C3
constraint intact for a second engine: nothing is scraped from a screen, and
the status shown is only what the rollout can actually support. Inferring
"working" from anything less direct than the task events would put cards in
Working that are not.

## The five columns

| Key | Shown as | Matches | Hideable |
|---|---|---|---|
| `blocked` | **Needs you** | `blocked`, `failed` | no |
| `working` | **Working** | `working` | no |
| `done` | **Replied** | `done`, `stopped` | no |
| `complete` | **Done** | `complete` | yes |
| `idle` | **Idle** | `idle` | yes |

Left to right is descending order of how likely you are to act. Both settled
columns are hidden by default, with their counts kept on the toggles, so the
opening view is the three that still want something from you.

Interactive sessions have no `done` of their own — the CLI only gives them
`busy` and `idle` — so a turn ending would otherwise drop a card straight
from Working into a hidden column with its answer unread. An interactive
session that is idle **with output you have not seen** therefore files under
**Replied** (if you have opened it before, or it spoke within the last hour);
reading it returns the card to Idle. Sessions that have sat unopened for ages
stay honestly Idle.

*Closing a terminal no longer deletes a card.* `claude agents` lists an
interactive session only while its process is alive, so closing its tab used
to make the card vanish from every column even though the whole conversation
was still on disk. Its transcript is now read back the same way Codex sessions
always have been: a recently-touched interactive transcript the live fleet no
longer knows about is resurfaced as an ordinary card — filed by the same
Replied/Idle rules — for 24 hours after its last activity, so a reply you had
not read is still there to find and resume. Background jobs are left out of
this, because the CLI keeps reporting finished background jobs itself.

*Why Replied is not Done.* The CLI's `done` means *the agent's turn ended*,
which is a weaker claim than *the work is finished* — an agent that asks
"shall I push?" reports `done` with the conversation wide open. So `done`
files under **Replied**, and **Done** is reserved for `complete`, a state that
exists only in agentgrid and that only a human can set, by moving a card
there. Filing every ended turn under "Done" would make the column meaningless.

## Moving cards by hand (overrides)

Drag a card between columns, or use the **Move to** buttons in the detail
panel — dragging is not discoverable, so the same moves are spelled out.
Destinations: **Needs you**, **Replied**, **Idle**, **Done**. **Working** is
refused in both directions: saying a process is running does not start one,
and a genuinely working card *is* running — stop the session instead.

An override records the real status at the time of the move and the session's
last activity at that moment, and applies only while both still hold. Marking
a blocked session Done drops the override the instant the session starts
working again; marking a session Done and then chatting with it does not
silently slide it back to Done when that next turn ends. Overridden cards are
marked `moved`; **Auto** in the panel clears the override.

*Why this is the way it is.* A manual move is your judgement about a result.
Recording the reality it was made against is what lets it expire by itself
when reality moves on — and is what stops the board from slowly becoming a
pile of stale opinions.

## Unread

A card in **Replied** carries a blue stub until you open it — the same shape
as the amber stub on **Needs you**, so "the agent has said something you have
not read" reads at a glance. Opening the card clears it; the agent's next
reply brings it back on its own. **Mark unread** in the panel puts it back by
hand.

*Why this is the way it is.* Unread is stored as how far you had read — the
session's last activity at the moment you opened it — not as a flag. New
output therefore makes a card unread with nothing watching for it; a boolean
would need clearing by whatever noticed the new output, and nothing is
watching. **Mark unread** *forgets* the read position rather than setting a
flag, because the honest way to say "I have not seen this far" is to have no
record at all. Unread appears only on settled sessions — an unread marker on a
running agent would just be saying "it is running", which the column already
says.

## Alerts

The **Alerts** toggle in the bar turns on desktop notifications: one when any
session lands in **Needs you**, one when a working agent replies with
something you have not read. Clicking a notification focuses the board and
opens that session's panel. The first click asks the browser for permission.

*Why this is the way it is.* The founding problem was discovering a blocked
agent minutes or hours late, and a board still has to be looked at. The
notification closes that loop — and it lives in the page, not a service, so
alerts exist exactly while a board tab is open. Nothing runs when it closes.

## Tags

Free-form labels per session: at most 6 each, at most 24 characters. A
starting vocabulary is offered (`PR review`, `Bug fix`, `Feature`, `Spike`,
`Task`, `Chore`) but any tag you type joins the list for as long as something
is wearing it — creating a tag and applying it are the same action. "PR
review" and "PR Review" are the same label; the first spelling seen wins.

A tag may be *suggested* from the session's title and prompt, shown as a
dashed outline. It is never applied automatically.

*Why this is the way it is.* Guessing from a title is right often enough to
save typing and wrong often enough that it must not label anything on its own.
Tags stay visually neutral because colouring them would compete with the one
accent that means "a human is needed". The tag filter offers only tags
something actually has, plus `Untagged` — a filter that can only ever return
nothing is noise.

## Starting agents

**New agent** (or `n`) opens a sheet with a **Library** row and these fields:
**Name** (optional), **Project**, **Engine** (Claude or Codex), **Session**
(Background or Interactive), **Model**, **System prompt** (optional), **Task**.

The library holds reusable agent definitions — a name, an engine, a model
and a system prompt, stored in `~/.agentgrid/agents.json`. Picking one fills
the sheet; **Save** stores the current fields under the Name; a definition
is nothing but a saved way of launching a real session, so everything on the
board works on the result. The system prompt rides inside the task as a
framed preamble, because neither CLI documents a version-stable
system-prompt flag for detached runs.
The model options follow the engine; Default always means "let the CLI's own
config decide". `Cmd/Ctrl+Enter` in the task field starts. `Enter` in the
name field advances rather than submitting — the task is required, and
submitting from the name box would start an agent with nothing to do.

With Claude and **Session: Background** (the default) this runs
`claude --bg [--model M] <prompt>` in the chosen directory, hands the session
to the daemon and returns immediately. The agent appears on the board within a
couple of seconds, carrying the name you typed, and outlives the browser — and
the terminal, since there isn't one. With Codex it runs `codex exec` detached
in the same directory; Codex prints no job id to wait on, so the name is held
against the directory and applied to the first Codex session that appears
there.

**Session: Interactive** (Claude only, macOS) instead opens a Terminal.app tab
running an ordinary `claude <prompt>` you can watch and type into — the same
session shape you would get from running `claude` yourself. Every part of the
shell line is quoted, so a prompt full of quotes or shell metacharacters runs
as one argument, not as commands. An interactive session lives and dies with
its tab; when you close it the card does not vanish (see below), but the
process is gone, so the board offers to resume it rather than reopen it.

The project picker lists every directory containing `.git`, one level under
each root, unioned with every directory the live fleet is already working in.
Defaults: `~` and `~/Documents/GitHub`. `--root DIR` (repeatable) or
`AGENTGRID_ROOTS` (path-separator separated) **replaces** the defaults. The
scan is one level deep — `~/work/clients/foo` needs `--root ~/work/clients`.
Two checkouts sharing a folder name are disambiguated by their parent folder.

The picker is also curatable, in `~/.agentgrid/projects.json`: **+ Add path**
puts any directory in the list by hand (the escape hatch for the one-deep
scan), **★ Favourite** floats a project to the top under its own heading,
and **Hide** drops one from the everyday list — hidden entries stay listed
at the bottom so they can be unhidden without a separate manager. Hiding is
a display preference, never a security boundary: hidden projects still count
as known directories for spawning.

*Why this is the way it is.* Name comes first because it is the thing you will
look for on the board afterwards — auto-generated titles are derived from the
first prompt, so three sessions started from the same ticket all read
identically. The name is applied asynchronously: the session does not exist
when you press Start, so the name is held against the job id that
`claude --bg` prints and written the moment the fleet first reports it. The
prompt is unconstrained, but the directory must be one of the discovered
projects — that boundary is what keeps the endpoint from being a general
"execute anything anywhere" hole.

## Getting into a session

| Session type | `a` (grid) / **Open ↗** (board) |
|---|---|
| Background | `claude attach <job-id>` — genuinely joins the running session |
| Interactive in Terminal.app | brings the real tab to the front |
| Interactive elsewhere | explains why not; the grid then offers a forking `claude --resume` behind a confirmation |

From the board, **Open ↗** opens a tab in Terminal.app, named after the
session, `cd`'d to the session's directory. Clicking it twice does not open a
second attach — the second click focuses the tab already attached.

The button lives in two places: hovering a card reveals **Open ↗** on the
card itself, and the panel carries **Open in Terminal** — the card button for
getting in without the panel detour, the panel one for when you are already
reading.

*Why interactive sessions are refused.* An interactive session's keyboard
belongs to the pty inside the emulator that owns it; no other process can
write to it, so read-only is the structural ceiling, not a missing feature.
`claude --resume` on a live session *forks* the conversation into a copy, and
quietly ending up on a divergent branch is worse than being told no — the
grid offers the fork explicitly, behind a confirmation, and refuses while the
session is actively working.

**One manual setup step for tab naming.** Terminal.app appends the active
process name and argument to tab titles, and those two components are the only
ones not exposed to AppleScript. Turn them off under **Terminal → Settings →
Profiles → Window → Title**, or every attached tab reads "claude".

The first focus or open will also ask for automation permission; if it is
refused, grant it under **System Settings → Privacy & Security → Automation**.

## Renaming

Rename from the board panel, or `⇧R` in the grid. Names are stored by session
id in `~/.agentgrid/names.json` and survive restarts. An empty name clears it
and returns the card to its generated title.

*Why renaming is manual.* There is nothing to import names from. Terminal tab
names cannot be read back from the emulator; Cursor keeps terminal titles in
renderer memory only — its workspace state stores nothing but internal pty
ids, and the name is absent from the process environment; Terminal.app is
scriptable but only exposes the titles Claude Code itself sets.

## Reading transcripts

Clicking a card opens the transcript in a side panel, rendered as structured
messages — not a wall of text, and not chat bubbles, which waste the width
that code needs. Both speakers get the same shape — raised surface, left rule,
uppercase label — and differ only by hue: amber for you, blue for the agent.
The panel shows the tail of the conversation (the last 600 blocks), because
the end is the part you opened it to read.

Tool traffic recedes and collapses: everything between two messages — calls,
results, thinking markers — is one trace unit. Fewer than three calls render
inline; three or more collapse into a strip summarising the count and the
distinct tool names. A collapsed trace still shows how many calls failed.

Markdown rendering is deliberately partial: fenced code, inline code, bold,
headings, bullets, bare URLs, `@mentions`. Every construct added is another
chance to mangle text that was fine as it was.

Reading is always safe — the JSONL is opened read-only, so it is safe on a
session that is actively running, and on one running in a terminal that
cannot be touched.

## Subagents

A card shows `N agents, M running` when a session has fanned work out. The
panel lists the parent conversation and every subagent as one selectable
roster, so "which of these am I reading" is always answerable; selecting a row
loads that agent's own transcript. Up to three subagents show outright; past
that the roster folds, still naming the conversation currently selected, and
scrolls rather than pushing the transcript off screen.

*Why this is the way it is.* A subagent file does not record its own name, so
it is joined back to the parent's `Agent` tool call by prompt text. A file
that fails to match still appears, labelled by its own opening line — a
subagent is never silently dropped.

## Notes and todos

The pad is the second half of the product. It exists because the board is
already where you sit while agents run, and meeting notes and agent work are
the same working memory.

Storage is plain markdown on disk, one directory per day, one file per page,
and **the filename is the page title**:

```
~/.agentgrid/notes/2026-08-05/Notes.md
~/.agentgrid/notes/2026-08-05/Kamil sync.md
```

The filesystem is the source of truth: the directory reads the same in Finder
as in the app, a page added there appears, one deleted there falls out. Any
line matching `- [ ] text` — any bullet, any indent — is a live todo,
collected into a rail on the right and tickable from there. Ticking rewrites
exactly one character; every other byte of the file is untouched, because the
file is something you also edit by hand.

Todos span every page of a day; unfinished items from earlier days carry
forward under a separate *Carried over* heading. The rail window is Day /
Week / Month, counted back from the day being viewed. Done folds away,
keeping its count. The Notes tab carries a count of everything still open, so
the board tells you what is outstanding without leaving it.

Writing shortcuts, for the moment you are trying to keep up with a meeting:

- `-[]` then space expands to `- [ ] ` (also `-[x]`)
- `Enter` on a checkbox line continues the list; on an empty item, ends it
- `Tab` nests an item, `Shift+Tab` lifts it out — only on checkbox lines

Due dates are written in the sentence and shown as a chip: `tomorrow`,
`tonight`, `in 3 days`, `next week`, `march 3rd`, `3 March 2027`,
`2026-12-01`. They resolve against the day the note belongs to, not the
clock — "tomorrow" written on Monday still means Tuesday when read back a
fortnight later. A bare weekday deliberately does not count ("wednesday sync"
is a meeting name, not a deadline); a weekday counts only when tagged
(`#mon`, `#friday`) or preceded by a cue (`on friday`, `by monday`, `due
tuesday`, `next`, `before`, `until`, `till`). Only overdue and due-today are
coloured.

Ticket keys become links: `PROJ-857` →
`https://jira.example.com/browse/PROJ-857`, overridable with
`AGENTGRID_JIRA_URL`. Rendered in the rail and history, not in the editor —
the pad is a plain textarea on purpose, because a rich editor fights the
keyboard in the one moment notes have to be frictionless.

Autosave runs about 600 ms after you stop typing, plus a flush on blur, on
every navigation, and on closing the tab. A poll landing mid-sentence never
clobbers what is being typed.

*Why one store.* Notes and todos are the same document, and a todo written
from a session panel lands in the ordinary daily pad. Anything that has to be
re-entered somewhere else does not get done, and a second copy is one more
thing able to disagree with the first.

## Pages, groups and repeats

Tabs sit between the date and the pad. `+` creates a page (seeded with
`# Title`, because an empty page would be deleted by the next save). Drag a
tab to reorder; order and grouping are global — how you like your pages
arranged is a standing preference, not a fact about one Tuesday — and
reordering leaves pages absent from today exactly where they were. Only the
collapsed state is per day.

Groups work like browser tab groups: a shared label and a tint, with the tabs
still in one row — nothing can end up buried inside something you forgot
about. Click the label to fold a group (it keeps its label, its count, and
the page you are on); drag the label to move the whole group. A group sits
wherever its first member sits, so one ordering array governs both. Grouped
and loose pages interleave freely.

The tab menu (`˅` or right-click) touches only the day you are on: Rename
(in place), Group, Repeats, **Delete from this day**. **Repeats** pins a page
so it is recreated in the same group every day, or only on the weekdays you
pick — a Friday 1-1 appears on Fridays and nowhere else, empty each time.
Structure recurs, content does not: *Carried over* already surfaces
unfinished work, and copying it forward would put it in a second place to rot.

`⊞` shows every page of the day as a card with its first lines and its open
count — for finding one among many. Clicking a card opens that page's
**history**: everything written on it across every day, newest first, today
at full brightness, **Edit ›** on any day jumping there.

`⚙` opens a settings panel over every page name you have ever used, showing
how many days each spans, and renames / groups / re-schedules / deletes it
across all of them.

*Why two deletes.* A page is not one object — it is one file per day that
happens to share a name. "Delete from this day" and "Delete everywhere" are
both labelled in full, and the destructive confirmation carries the day
count, because "delete AQuA" reads very differently once you know it is
twelve days of notes.

## Notes tied to a session (@mention)

Typing `@` in the pad offers the live sessions and inserts a slug handle
(`@agent-run`) — a slug rather than the title, because a title with spaces
has no end marker in plain text. The mention renders as a link back to that
session; clicking it opens that session's panel. In the other direction, a
session's panel gains a **Notes** block listing every line in your notes that
names it, with a box to add more — **Add todo** writes a checkbox, **Add
note** writes plain text, both into the ordinary daily pad.

*Why this is the way it is.* The panel is a view over the pad, not a second
store. Mentions are read out of the pads on demand (120 days back) rather
than indexed — an index would be one more thing able to disagree with the
notes.

## Syncing notes to GitHub

Notes are plain markdown, so syncing them is a git problem. The **⟳ Sync**
button (top right) turns `~/.agentgrid/notes/` into a small git repository and,
on each click, commits what changed, pulls what the remote has, and pushes.
Name a repo you can push to once — an SSH or HTTPS GitHub URL — and it is
remembered; only the notes and their page arrangement travel, never the
machine-local state (session names, tags, read positions).

The transport is **your own git** — your SSH key or credential helper — so
agentgrid stores no token or password, in keeping with the loopback, no-secrets
posture everywhere else. There is nothing to paste and nothing to leak.

The order of operations is the safety guarantee, because notes are the one
thing here you cannot afford to lose: local changes are committed *before*
anything is pulled, the pull is a rebase that is **aborted, never forced
through**, the moment it conflicts, and nothing ever force-pushes or resets
hard. A genuine conflict (two machines editing the same lines) is reported for
you to merge by hand rather than resolved by guesswork. A second machine whose
notes folder is empty adopts the remote outright, so getting set up elsewhere
is one click.

## View modes

Three ways to sit, remembered across restarts: **Board** (the columns alone),
**Notes** (the pad alone), and **Split** — the pad docked under the board at
a fixed, resizable height, for meetings where you want to watch agents run
while you write. Switching modes never reflows the columns you were reading.

## Filtering and search

- **Project dropdown**, built from what the fleet is actually in, with a
  count per repo. If the active repo disappears from the fleet, the filter
  falls back to All projects rather than showing an empty board.
- **Tag dropdown**, including `Untagged`.
- `/` focuses search; matches name, project, branch, prompt and tags, and
  works alongside both dropdowns.
- **Column toggles** for Done and Idle, with live counts.

Single-key shortcuts (`n`, `/`) never fire while you are typing in any field.

## The terminal grid

Three views, `Enter` goes deeper and `Esc` comes back:
`GRID → DETAIL → TRANSCRIPT`. Anything waiting on a human sorts to the top
left.

| Key | Grid | Detail | Transcript |
|---|---|---|---|
| arrows / `hjkl` | move between cards | move between subagents | scroll |
| click | select; click again to open | select a subagent | — |
| `Enter` | open the session | open the transcript | — |
| `Esc` | — | back to grid | back to detail |
| `Tab` | jump to next session needing you | — | — |
| `⇧R` | rename | rename | — |
| `f` | cycle filter (all / active / needs you / recent) | — | — |
| `a` | attach | attach | — |
| `x` | stop (asks first) | stop | — |
| `g` / `G` | — | — | top / bottom |
| `u` / `d` / space | — | — | page up / down |
| `r` | refresh | refresh | refresh |
| `q` | quit | quit | quit |

`recent` means active in the last 24 hours — the practical view once
long-lived idle sessions pile up. Every card is the same height so the grid
stays aligned; selection is shown by heavy border rules rather than a filled
highlight. Stopping always asks first, because every row is a live process:
background sessions get `claude stop` (conversation kept, resumable),
interactive ones get a signal to the pid (the transcript stays on disk either
way).

Click support puts the terminal into mouse-reporting mode, which takes over
text selection — hold `Option` to select anyway, or launch with `--no-mouse`.
`--cwd PATH` scopes to sessions under a directory, matching by path
component, so `--cwd ~/repo` does not also pull in `~/repo-tests`.

## The Notification hook

**Needs you** has two completely separate paths in, and both work out of
the box:

1. **Background sessions** report their `state` directly, and one of its
   values is literally `blocked` — it maps straight through to the column.
2. **Interactive sessions** report `waiting` when a question or permission
   prompt is pending — discovered by sampling the CLI while a question
   actually blocked a session. It maps straight through as well. (Earlier
   CLI versions only knew `busy`/`idle`, which is why the hook below was
   once the only source of this signal.)

The optional hook is now a refinement rather than a requirement: it also
records *why* a session is waiting, and clears the wait the instant you
submit a reply rather than on the next poll.

Install it into `~/.claude/settings.json` under both `Notification` and
`UserPromptSubmit`:

```json
{
  "hooks": {
    "Notification": [
      { "hooks": [ { "type": "command",
          "command": "python3 /path/to/agentgrid/hooks/agentgrid_notify.py" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /path/to/agentgrid/hooks/agentgrid_notify.py" } ] }
    ]
  }
}
```

It records `{waiting, since, reason}` per session in
`~/.agentgrid/state.json`: `agent_needs_input` sets the wait,
`agent_completed` or a prompt submission clears it, and entries older than
seven days are dropped. It always exits 0 and never writes to stdout, so a
failure here can never block or disrupt the session it observes.

Hook state may only promote `idle` to `blocked`. A session reporting `busy`
is demonstrably running, which beats a hook event that may predate it; and
transcript activity newer than the event means you already replied, so the
wait clears even if the clearing event never fired.

## Security

The web server can start processes, so its posture is deliberate and worth
stating plainly:

- It binds `127.0.0.1` only. It is not reachable from another machine, and
  the port must not be forwarded or exposed off the machine.
- A fresh token is minted per run. The printed URL carries it; every request
  must present it (`?t=` or an `X-Agentgrid-Token` header). A bare
  `http://127.0.0.1:8787/` returns 403 with a pointer at the printed URL. The
  token is what stops any other page in your browser from driving the server.
- Every response carries `X-Frame-Options: DENY` and
  `Referrer-Policy: no-referrer` — no embedding, and no referrer leakage of
  the token.
- Per-request logging is silenced; it would scroll the launching terminal.

## State on disk

Everything agentgrid itself writes lives under `~/.agentgrid/`:

```
~/.agentgrid/
├── state.json          # the hook's record of who is waiting
├── names.json          # your names for sessions
├── overrides.json      # manual card moves, with the reality they were made against
├── tags.json           # tags per session
├── read.json           # how far you had read each session
├── note-meta.json      # page arrangement and pins
├── sync.json           # notes-sync remote, branch and last-sync time (no secrets)
└── notes/              # the daily pads, plain markdown (a git repo once you sync)
```

Every state file is written atomically (write a temp file, then rename), so a
concurrent reader never sees a half-written file. Every read tolerates a
missing or corrupt file — these are optional enrichments, and deleting
`~/.agentgrid/` entirely just resets them.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The layout tests run the real curses drawing code against a hand-written fake
window and assert on the resulting character grid, so a card-geometry
regression fails in CI rather than only being visible to a human squinting at
a screen.

## Further reading

- [docs/USAGE.md](docs/USAGE.md) — a task-oriented walkthrough

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 Dimitri
El-Choueiry. In short: use, modify and redistribute freely, including
commercially, provided you keep the license and attribution and note any
changes; it also grants a patent license and comes with no warranty.

### Choosing an exact model

In **New agent**, choose Claude or Codex, then pick a model or select
**Custom model…** and enter its exact ID. Saved library agents retain that ID.
Suggestions come from Codex's local model cache and Claude's configured model,
local usage history, and models seen on the board. Suggestions may include older
models; the CLI checks availability. **Default** uses the CLI's configuration.

In Claude chat, `/model` opens the model picker. `/model <exact-id>` selects an
ID directly, and `/model default` clears the override. The selection applies to
subsequent messages, not turns already running or queued. These commands are
handled locally and are not sent as prompts. Codex's chat composer remains
unavailable; choose its model when creating a new agent.
