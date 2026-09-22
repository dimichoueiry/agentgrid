# Operating agentgrid

How to install, run, keep running, upgrade and back up agentgrid. For what
the features do, see the [README](../README.md); for a day with it, see
[USAGE.md](USAGE.md).

## Requirements

| Needed for | What |
|---|---|
| Running it at all | Python 3.10 or newer (`python3 --version`) |
| Installing and upgrading | `git` |
| Seeing and starting Claude sessions | the Claude Code CLI (`claude`) on `PATH`, already signed in |
| Codex sessions (optional) | the `codex` CLI on `PATH` |
| Interactive sessions and tab focusing (optional) | macOS Terminal.app |
| Orchestrators (optional) | an OpenRouter API key |
| The web review overlay (optional) | Chrome, with the [extension](../extension/README.md) |
| Running the browser-side tests (contributors only) | Node.js |

Nothing else. No pip packages, no virtualenv, no build step, no npm: a clone
is a working install.

## Supported platforms

- **macOS** — the primary platform; everything works.
- **Linux** — the board, the grid, background agents, tickets and notes work.
  Interactive sessions need Terminal.app, so they are reported as unavailable
  and background sessions are used instead. There is no Keychain, so set
  `OPENROUTER_API_KEY` in the environment `ag --web` runs in.
- **Windows** — not supported (the launcher is a bash script and the grid uses
  curses). WSL is untested.

## Install

```bash
git clone https://github.com/dimichoueiry/agentgrid.git ~/agentgrid
mkdir -p ~/.local/bin
ln -sf ~/agentgrid/bin/ag ~/.local/bin/ag
ag --version
```

If `ag` is not found, `~/.local/bin` is not on your `PATH`. Add it
(`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`, then open a new
terminal), link `ag` into a directory that already is, or skip the link and
run `~/agentgrid/bin/ag`. `python3 -m agentgrid` from the clone also works.

The clone can live anywhere; `~/agentgrid` is only the example used on this
page.

## Launch, stop, restart

| Do | How |
|---|---|
| Launch the board | `ag --web` — prints a URL and opens it |
| Launch without a browser | `ag --web --no-open`, then open the printed URL |
| Launch the terminal grid | `ag` |
| Stop | Ctrl-C in the terminal running it |
| Restart | Ctrl-C, then `ag --web` again |
| Check what is installed | `ag --version` |

The printed URL carries an access token, and **the token changes every
launch**. After a restart, use the new URL; an old tab gets 403. Reopen the
board tab once after a restart so the Chrome extension picks up the new token.

If port 8787 is taken, `ag --web` picks a free port and prints it. Use
`--port N` to choose one.

What stopping does and does not stop:

- Background agents (`claude --bg`, `codex exec`) keep running. They are not
  children of agentgrid, and they are on the board again when you relaunch.
- Interactive sessions live in their Terminal tabs and are unaffected.
- Orchestrator runs pause and resume from their last completed step on the
  next launch.
- A chat-panel reply that is mid-turn is cut off with the server. Messages
  queued behind it are kept, held, and sent once that session is free again.

## Keep it running

agentgrid runs in the foreground. It has no daemon mode and no service file.
To keep it up:

- **Simplest:** leave it in a terminal tab of its own.
- **Detached:** run it in tmux and reattach when you need the URL:

  ```bash
  tmux new -d -s agentgrid 'ag --web --no-open'
  tmux attach -t agentgrid     # shows the URL; Ctrl-b d to detach again
  ```

- **Stop macOS idle sleep while it runs:** `caffeinate -i ag --web`. Closing
  the lid on battery still sleeps the machine; work resumes on wake.
- **Always on:** run it on a machine that never sleeps and reach it over an
  SSH tunnel (`ssh -L 8787:127.0.0.1:8787 <host>`) or a private network. Never
  expose the port publicly. See
  [Running with the laptop shut](../README.md#running-with-the-laptop-shut).

The running server's port and token are also in `~/.agentgrid/web.json`
(owner-readable only).

## Upgrade

```bash
git -C ~/agentgrid pull
ag --version        # confirms the new commit
```

Then restart `ag --web`. There is no migration step: state files are read
tolerantly and written in place.

If you move the clone, re-create the `ag` link and update the
[Notification hook](../README.md#the-notification-hook) path in
`~/.claude/settings.json` if you installed it.

To go back to an earlier version: `git -C ~/agentgrid checkout <commit>`,
then restart.

## Persistence and backup

Everything agentgrid writes is under `~/.agentgrid/`: names, tags, card
moves, tickets, notes, personas, orchestrator runs, saved agents and the chat
queue. The full list is in [State on disk](../README.md#state-on-disk).

Not in `~/.agentgrid/`:

- **Session transcripts** belong to the CLIs (`~/.claude/`, `~/.codex/`).
  Back those up separately if you want them. agentgrid only reads them, with
  one exception: deleting a conversation from History moves its transcript
  into `~/.agentgrid/trash/` until it is purged.
- **The OpenRouter key** is in the macOS Keychain, or in `OPENROUTER_API_KEY`.

Back up by copying the directory. Stop `ag --web` first so nothing is written
mid-copy:

```bash
tar -czf ~/agentgrid-backup-$(date +%F).tar.gz -C ~ .agentgrid
```

Restore by stopping agentgrid and unpacking over `~`:

```bash
tar -xzf ~/agentgrid-backup-YYYY-MM-DD.tar.gz -C ~
```

Notes alone can also sync to a private GitHub repo from the app; see
[Syncing notes to GitHub](../README.md#syncing-notes-to-github).

Deleting `~/.agentgrid/` resets agentgrid to a fresh install. Your sessions
are not affected; only agentgrid's own names, tags, tickets and notes go.

## Uninstall

```bash
rm ~/.local/bin/ag
rm -rf ~/agentgrid          # the clone
rm -rf ~/.agentgrid         # your data — back it up first if you want it
```

Remove the hook entries from `~/.claude/settings.json` if you added them, and
remove the extension at `chrome://extensions`.

## When something is wrong

| Symptom | Fix |
|---|---|
| `ag: command not found` | See [Install](#install): `~/.local/bin` is not on `PATH`. |
| `SyntaxError` on launch | Python is older than 3.10. Check `python3 --version`. |
| Browser shows 403 | The token changed. Use the URL printed by the current run. |
| No sessions on the board | Check `claude` works in a terminal, and that you are signed in. |
| A project is missing from New agent | It is more than one level under a root. Launch with `--root <its parent>` or use **+ Add path**. |
| Review overlay does nothing | Open the board tab once so the extension learns the new token. |

When reporting a problem, include the output of `ag --version`.
