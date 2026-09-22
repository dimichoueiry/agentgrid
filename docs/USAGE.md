# agentgrid — a walkthrough

The [README](../README.md) is the reference: what each feature is and why it
is shaped that way. This file is the other half — what a day with agentgrid
actually looks like, keystroke by keystroke. It assumes agentgrid is
installed and the `claude` CLI works.

---

## 1. Morning: open the board

```bash
ag --web
```

The terminal prints something like:

```
agentgrid web UI
  http://127.0.0.1:8787/?t=vqXcR0h1sBpO4kLqzD3mWn8aYtFg2j5H
  projects from: /Users/you, /Users/you/Documents/GitHub
  loopback only, token-gated, stops when you Ctrl-C this terminal.
```

and the browser opens on that URL. Keep the terminal around — Ctrl-C is how
you stop the server, and the printed URL is the only way back in if you close
the tab (the token changes on every run).

Say five agents are alive and two of them are stuck. The board opens on three
columns — **Needs you 2**, **Working**, **Replied** — with the two settled
columns folded into the toggles in the top bar, each showing its count
(`Done 3`, `Idle 15`). The two cards under **Needs you** each carry a thin
amber stub on their left edge. That stub is the only warm colour on the page;
if you see amber, something is waiting on a human.

Leave the tab open. The board polls every two seconds; you never refresh it.

## 2. An agent finishes while you watch

A card slides — physically travels, over a quarter of a second — from
**Working** into **Replied**, and picks up a blue stub. Blue means "the agent
has said something you have not read".

Click the card. A panel slides in from the right with the conversation's
tail: your prompts amber-ruled, the agent's replies blue-ruled, the tool
traffic between them folded into thin strips (`23 calls · Bash, Read, Edit`).
Click a strip to unfold it; if any call failed, the strip says so even while
shut.

Read the reply, press `Esc` (or click the scrim) to close. The blue stub is
gone. If the agent says something new on its next turn, the stub comes back
by itself — you never mark anything read.

If you skimmed it and want it back in your queue: open the panel again and
click **Mark unread**.

## 3. Unblock a stuck agent

Click a card under **Needs you**. The panel's transcript ends at the
question the agent is asking — usually a permission prompt or a "shall I
proceed?".

Click **Open ↗**. What happens depends on where the session lives:

- **A background agent**: a new Terminal.app tab opens, already `cd`'d into
  the project and running `claude attach <job>`. You answer the prompt in
  that tab, and the card on the board moves back to **Working** within a
  couple of seconds. If you click **Open ↗** again later, the existing tab
  comes to the front instead of a second attach opening.
- **An interactive session in Terminal.app**: its real tab comes to the
  front.
- **An interactive session in Cursor or anywhere else**: the board tells you
  it cannot get you there and why. Switch to that window yourself; the board
  has at least told you *which* session wants you, which is the hard part.

When the work is genuinely finished — reviewed, merged, pushed — drag the
card to **Done**. That is your judgement, not the CLI's, and the card stays
there unless the session comes back to life.

## 4. Start a named agent

Press `n` (anywhere on the board that is not a text field), or click
**New agent**.

A sheet opens with four fields. Fill them top to bottom:

1. **Name** — `ranking fix`. This is what the card will say, so name it the
   way you will look for it. Press `Enter`; focus moves on rather than
   submitting.
2. **Project** — start typing; the list is every git checkout under your
   roots plus everywhere the fleet already is. If the board was filtered to
   one repo, that repo is already selected.
3. **Model** — leave the default unless you have a reason.
4. **Task** — the prompt. As long as you like. `Cmd+Enter` starts.

The sheet closes, a toast confirms, and within a couple of seconds a card
called `ranking fix` appears under **Working**. The agent belongs to the
daemon now — closing the browser, or the whole machine going to sleep and
waking, does not touch it.

## 5. A meeting starts: Split view

Click **Split** in the top bar (or press nothing — the mouse is fine here;
you are about to be typing prose, not commands). The board compresses
upward and the pad docks beneath it. Drag the seam if you want more of one
or the other.

Click `+` in the tab row, type `Kamil sync`, press `Enter`. A fresh page,
seeded with its title.

Now just type. When an action item falls out mid-sentence:

```
agreed the ranking change ships behind a flag
-[]
```

— the moment you type the space after `-[]`, it expands to `- [ ] `. Finish
the line:

```
- [ ] chase Kamil for the flag name tomorrow @ranking-fix
```

Three things happened in that one line:

- `tomorrow` becomes a due chip in the rail, resolved against the day this
  page belongs to — it will still mean the right Tuesday when you read this
  page back in three weeks.
- Typing `@` popped a little menu of the live sessions; arrow keys and
  `Enter` picked `@ranking-fix`. The mention is now a link — from the rail,
  clicking it opens that session's panel; from the session's panel, this
  line now appears under **Notes**.
- The line itself landed in the rail on the right, under **To do**, tagged
  with the page it came from.

`Enter` at the end of a checkbox line gives you the next `- [ ] `; `Enter`
on an empty one ends the list. `Tab` nests a sub-item; `Shift+Tab` lifts it
back out.

You never save. The pad saves itself about half a second after you stop
typing (`…` then `Saved`, top right of the pad bar), and flushes on every
navigation and on closing the tab. Type until the meeting ends and walk
away.

## 6. Tick things off from the rail

The rail collects every open checkbox from every page of the day. When you
chase Kamil, click the box in the rail — done. The underlying file changed
by exactly one character (` ` → `x`), which you can verify if your notes
directory is a git repo:

```bash
cd ~/.agentgrid/notes && git diff
```

The rail heading cycles scope: click it to toggle between **all pages** and
**this page only**. The Day / Week / Month buttons widen the window; Week is
the useful default on a Monday. Items from previous days that are still
open appear under **Carried over** — they follow you until they are ticked
or deleted, which is the point.

**Done** sits folded at the bottom with a count. Leave it folded.

## 7. Make the Friday 1-1 recur

You have a page called `Friday 1-1`. Click the `˅` on its tab (or
right-click the tab). In the menu, choose **Repeats**, and click `F`. Done:

- Next Friday, the page is there, empty, in the same group you keep it in.
- Tomorrow (a Saturday), it is not.
- Browsing back to last Friday still shows last Friday's page.

Content never recurs — a standing meeting gets a fresh page each time, and
anything unfinished from last week is already in **Carried over**.

The same menu is where **Rename** (edits the tab in place — `Enter`
commits, `Esc` cancels), **Group**, and **Delete from this day** live.
Deleting a recurring page from one day sticks for that day; it does not
unschedule the repeat for other days — that lives in the settings panel
(§9).

To group pages the way browser tabs group: menu → **Group** → type
`Product` (or tick an existing group). The tab picks up the tint; click the
group label to fold the whole group; drag the label to move the group as a
unit.

## 8. Find something you wrote weeks ago

Click `⊞` next to the tabs. Every page of the day appears as a card — first
lines, open-todo count. But the real use is history: click any card and you
get that page across **every** day, newest first, today at full brightness.
Your `Kamil sync` from three weeks ago is four entries down.

Reading it, you spot a decision that needs correcting — click **Edit ›** on
that entry and you are on that day, on that page, cursor in the pad.

Ticket keys are links here: `PROJ-857` in a page opens the Jira ticket
(set `AGENTGRID_JIRA_URL` if yours is not at the default).

## 9. Tidy up your pages

Click `⚙`. A panel opens over every page name you have ever used — spans,
groups, repeats — sorted the way your tabs are. Hover a row for its
controls:

- **Rename** here renames across every day the page exists. If any day
  already has a page with the new name, nothing is renamed and it tells you.
- **Group** and **Repeats** here are the same controls as the tab menu, but
  applied globally — this is also where you *unschedule* a recurring page.
- **Delete** here deletes everywhere, and the confirmation tells you what
  that means: *"Delete 'AQuA' — 12 days of notes"*. Read that number before
  clicking.

Dragging tabs reorders them; the order is a standing preference. Reordering
on a Tuesday leaves your Monday-only pages exactly where they were on
Monday.

## 10. On a server, or already in a terminal: the grid

```bash
ssh devbox
ag
```

The same fleet, as a grid of bordered cards. Anything needing you is top
left with a filled `NEEDS YOU` chip. Navigation is arrows or `hjkl`;
`Tab` hops straight to the next session that needs a human.

A working session's card:

- title row with a spinner while it runs
- the branch
- `› ` and the last thing you said to it
- tool counts, and up to three subagent rows with a `>` for running, `.`
  for done

`Enter` opens the detail view — full metadata, the subagent roster —
and `Enter` again opens the transcript (`j`/`k` scroll, `u`/`d` page,
`g`/`G` jump, `Esc` backs out one level).

`f` cycles the filter; `needs you` during triage, `recent` once idle
sessions have piled up. `a` attaches to a background session right there in
your terminal. `x` stops one — it always asks first. `⇧R` renames (and
lowercase `r` refreshes — they are different keys on purpose). `q` quits.

If you want to copy text out of the grid, hold `Option` while selecting, or
run `ag --no-mouse` and the terminal's own selection works untouched.

Scope it when a box runs many fleets:

```bash
ag --cwd ~/work/fusion       # only sessions under that tree
ag --filter "needs you"      # only the ones waiting on a human
```

## 11. Put the work on tickets

Click **Tickets**. Five columns, like the session board: Backlog, To do, In
progress, In review, Done.

Press `t` and file one:

> Title: **Login drops the session on refresh**
> Type: bug · Priority: high · Project: drawcal

It appears in To do as `DC-1`. Open it and click **▷ Start an agent on it**.
The New agent sheet opens with the ticket already the brief — description,
comments, and the commands the agent needs to report back — and the project
filled in. Start it. The ticket moves to In progress with the new agent's name
on it.

When the agent is done it runs `ag ticket move DC-1 review` itself, and the
count on the Tickets tab goes up: that number is how many tickets are waiting
on *you*, which is the same promise the Needs you column makes for sessions.

To hand a ticket to an agent that is already running, open the ticket and pick
the session from **Assignee**. It is not just a label — that agent gets the
ticket in its chat and can start straight away.

Three things worth trying once:

- **Lanes → by agent.** The board splits into one lane per agent. This is the
  "who is carrying what" view.
- **Lanes → by project.** The same for codebases.
- **List** layout, when you want every field at once rather than cards.

Your agents do all of this from a shell. Click the **ag ticket** button for
the full list, and use **Copy CLAUDE.md snippet** to paste the routine into a
project so its agents pick up tickets without being asked:

```bash
ag tickets                         # what is open in this checkout
ag ticket new "Title" --type bug   # file what you find
ag ticket take DC-1                # claim it and start
ag ticket comment DC-1 "…"         # leave a note on it
ag ticket move DC-1 review         # hand it back
```

## 11a. Hand a long job to an orchestrator

You have a goal rather than a task: "get the tickets in review shipped". You
do not want to sit and start five agents yourself.

First, once: **Providers** → paste your OpenRouter key → **Connect**. That key
is what an orchestrator thinks with; the agents it starts still run on your
Claude Code and Codex logins.

Now open a work area, click **Orchestrators** in the breadcrumb (next to Area
settings) and pick **+ New orchestrator**:

> Name: **Shipper** · Work area: Engineering · Model: `openai/gpt-5`
> Starts agents in: drawcal
> Standing instructions: *One agent per ticket. Make it comment on its ticket
> when it is done, then check the comment before you move on.*
> Starting agents: **Ask me every time**

Its window opens. Type the goal and press `⌘↩`:

> Take the tickets in `review`, check each one against its description, and
> either move it to done or hand it back with what is missing.

It reads the board, then asks. The banner says *It wants to start an agent —
your call*, with the project and mode it chose and the task it wrote. Fix the
task if it is not quite right, then **Approve & start**. The agent appears on
the board as an ordinary card; **Open ↗** works on it, chat works on it, it is
a normal session that happens to have been started by something other than
you.

Once you trust it, switch the header dropdown to **Auto** — mid-run is fine —
and it stops asking. It still asks before opening a Terminal window on your
desktop, because that one interrupts *you*.

Then leave. Close the tab. An approval waiting for you costs nothing while it
waits; a run stopped by a restart or a closed lid picks up from its last
completed step, and is told to look at what already exists before it starts
anything. Come back to the **Chat** tab for what it did, **Agents** for what
it started, and **Activity** for every decision and every dollar.

**Reuse it.** Open **Orchestrators ▾ → Personas…**. Your orchestrator's
role, model and defaults live on its *persona*; set *Agents it starts* to
Opus 5 · Interactive once, tick the saved agents and skills it should use, and
every team you post it to behaves that way. **Post here** puts the same
persona on another work area with its own product brief. When you correct it
("the SEO agent is retired"), it remembers — the chat shows where it kept it,
with an Undo, and the **Memory** tab lists everything.

Two things worth knowing:

- **The limits are real.** Under Limits: agents at once, agents per run, steps,
  and a spend ceiling. Agent Grid enforces them before each call; reaching one
  comes back to the orchestrator as "you are out of agents, finish with what
  you have".
- **One per area, or one over all of them.** Set Work area to **All work
  areas** and it can see every area and create new ones — the one to give
  "keep the whole board moving" to, rather than one project's backlog.

## 12. Install the hook (optional, five minutes)

Needs you works without it: background agents report `blocked` directly, and
an interactive session stopped at a question or permission prompt reports
`waiting` — both map straight onto the column. The hook is a refinement: it
also records *why* the session is waiting, and clears the wait the instant
you reply rather than on the next poll. Skip this section unless you want
that.

Run `ag hook install`. It merges the block below into
`~/.claude/settings.json`, keeps everything else in that file, and is safe to
run twice. `ag doctor` then shows `hook installed`. To do it by hand instead,
merge this in with the real path to your clone:

```json
{
  "hooks": {
    "Notification": [
      { "hooks": [ { "type": "command",
          "command": "python3 /Users/you/agentgrid/hooks/agentgrid_notify.py" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 /Users/you/agentgrid/hooks/agentgrid_notify.py" } ] }
    ]
  }
}
```

Then test it: in any interactive Claude Code session, ask for something that
triggers a permission prompt, leave the prompt unanswered, and watch the
session's card turn amber on the board. Answer the prompt; the card clears
on your next message.

## 13. Odds and ends worth knowing

- **A dragged card that will not drop into Working is not broken.** Working
  is not a destination — saying a process runs does not make it run. The
  card snaps back and the panel says so.
- **Marking a session Done is safe to be wrong about.** If the session
  starts working again, the override expires on its own; if you chat with
  it, it will not slide back to Done behind your back.
- **The picker missing a repo** means it is more than one level under a
  root. Launch with `--root` pointed one level above it: `ag --web --root
  ~/work/clients`.
- **Two browser tabs of the board are fine.** They share one poller on the
  server; ten tabs cost the same as one.
- **Your tickets are just files**, one JSON each under
  `~/.agentgrid/tickets/`. Grep them, edit them, commit them.
- **Your notes are just files.** `grep -r "flag name" ~/.agentgrid/notes/`
  works, and so does editing a pad in vim while the board is open — the app
  picks up the change on its next read.
- **Everything under `~/.agentgrid/` is disposable state** except `notes/`.
  Deleting the rest resets names, tags, moves and read positions, nothing
  more.
