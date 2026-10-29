# agent-rooms

English | [中文](README.zh-TW.md)

Lets several coding agents that are open at the same time (Claude Code, Codex) work together in "rooms". Each agent keeps working in its own window; the room carries messages, wakes people up, hands out tasks and checks the results.

- **Rooms**: a member list, a conversation, a whiteboard and tasks. An agent is in one room at a time and only sees that room.
- **Waking up**: whoever is @-mentioned gets woken. A judge model then reads the recent conversation to decide who else should know (whoever assigned the work, whoever it will touch). A message that only @-mentions the user also wakes the room's lead, who passes it on. Idle agents can be woken; a busy one gets the message after its next tool call.
- **Tasks**: the lead posts tasks, members claim, do and submit them, and a checker closes them (the one who did the work cannot close it).
- **The user** can move any agent into or out of a room, set do-not-disturb, and mark finished members offline. When something needs the user's decision, the room lights up.
- **Quota**: the left side of the page lists every Claude / Codex account on this machine with its 5-hour and 7-day usage and when each resets; new members are started on an account that still has quota.

Only the Python standard library is used (the desktop adapter also uses Node on the Windows side). The interface text and the rules given to agents are in Traditional Chinese.

## Parts

| Part | Where | What it does |
|---|---|---|
| Core | `rooms/core.py`, `rooms/server.py` | Room state and the HTTP API (default `127.0.0.1:8775`); can run in a container |
| Command line | `bin/room` | How agents and the user work with rooms; also the Claude Code hook |
| Dispatcher | `bin/rooms-dispatch` | Listens on behalf of Codex members and delivers into their conversation when they should be woken |
| Adapters | `adapters/codex_cli`, `adapters/codex_desktop` | Connect to the Codex CLI app-server and to the Windows desktop Codex |
| MCP | `adapters/mcp` | Room tools for Codex (the Codex sandbox cannot reach the local network) |
| Judge | `rooms/judge.py` | Asks a decision model who a message without an @ is for |
| Local tools | the balls on the right of the page | Web pages you run locally (dashboards, data viewers, helper tools) registered with the rooms: open in a narrow pane or a tab, attach to a room, view a code map (templates in `contrib/tool-templates.md`) |
| Local helper (optional) | `bin/room local` | If [acc](https://github.com/tenplus1000/acc) is installed, adds an "acc" button on members to see their goals and history; with a judgment layer it lists suggestions waiting for you; starts and stops local tools (`rooms/local.py`) |

## Getting started

### 1. Run the core

```bash
docker compose up -d --build
```

Data is kept in `./data` (change with `ROOMS_DATA_DIR`). Without a container: `bin/rooms-serve`.

### 2. Connect Claude Code and Codex (one command)

```bash
python3 contrib/add-room-hooks.py
```

It finds every Claude account on the machine (`~/.claude`, `~/.claude-*`, and on WSL also Windows' `%USERPROFILE%\.claude`) and every Codex account (`~/.codex`, `~/.codex-*`), and adds whatever hooks and MCP settings are missing. What is already set up is left alone, and each file is backed up as `.bak` before it is changed. Run it again after adding an account. What it does is described below if you prefer to set things up by hand.

#### Claude Code hooks

Add to `hooks` in `~/.claude/settings.json`:

```json
"UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "timeout": 5 }] }],
"PostToolUse":      [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "timeout": 5 }] }],
"Stop":             [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "asyncRewake": true, "timeout": 86400 }] }],
"SessionStart":     [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "asyncRewake": true, "timeout": 86400 }] }]
```

- `UserPromptSubmit`: each turn of a conversation that is in a room carries the room rules and the messages that have piled up.
- `PostToolUse`: interrupts mid-task only when someone is looking for it.
- `Stop`: after each turn, waits on the room in the background and wakes the conversation when someone calls.
- `SessionStart`: a conversation that was just opened or resumed, and has not run a turn yet, also waits in the background (without it, a reopened window only receives room messages after the user types something).

Conversations that are not in any room are not affected.

Claude on Windows (for example the desktop app) runs on Windows, so the hooks go through `wsl.exe` into WSL: the same hooks, with `command` set to
`MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- python3 <win_bridge.py> /path/to/agent-rooms/bin/room hook`.
`win_bridge.py` has to turn `C:\...` paths in the hook JSON (`cwd`, `transcript_path`) into `/mnt/c/...` before handing it to `room hook`; when the room sees a transcript path starting with `/mnt/`, it gives that conversation the `wsl.exe` form of the room command.

#### Codex MCP and the dispatcher

Register the MCP server (`~/.codex/config.toml`):

```toml
[mcp_servers.agent-rooms]
command = "python3"
args = ["/path/to/agent-rooms/adapters/mcp/server.py"]
default_tools_approval_mode = "approve"

[mcp_servers.agent-rooms.env]
ROOMS_URL = "http://127.0.0.1:8775"
```

Then run the dispatcher locally:

```bash
bin/rooms-dispatch
```

To start it at boot and restart it if it dies, use `contrib/rooms-dispatch.service` (a systemd user service; installation is described at the top of the file). If a Codex account's app-server is not running, the dispatcher starts it.

If acc (or a judgment layer, set `ROOMS_JUDGMENT_LAYER`) is installed, also run `bin/room local` (or `contrib/rooms-local.service`). It binds `127.0.0.1:8777`, the room page talks to it directly, and it only accepts the page origins listed in `ROOMS_LOCAL_ORIGINS`. If it is not running, nothing extra appears and the rooms work as usual.

A Codex window has to be attached to the app-server for the room to deliver into it: open it with `codex` or `codex resume <id>`, without flags such as `-c` / `--enable` / `--disable` / `--search` (with those it runs standalone).

### 3. Open a room

```bash
bin/room create my-project --user
bin/room move <session id> R1 --user        # move an agent into the room
bin/room rename <session id> worker1 --user
bin/room role worker1 "writes the tests" --user --room R1   # one line on what it does; the judge relies on it
bin/room overview
bin/room delete R3 --user                   # delete a room: it must be empty; the history is archived in data/rooms/deleted/
```

The lead (or the user) can also:

```bash
bin/room spawn worker2 "read the whiteboard first" --role "writes the docs" --cwd ~/my-project   # start a new Codex member
bin/room on worker1                     # bring an offline member back
```

- `spawn` starts a new Codex conversation on the app-server (no window). It picks the first account in `ROOMS_SPAWN_ACCOUNTS` that still has quota, or the one given with `--account`. The member starts working when its first message arrives; to watch it, attach with the `codex resume <id>` it prints.
- For offline conversations that are not yet put away, the dispatcher asks the judge, with the member's goal and the end of its conversation, whether it is "done and need not be brought back". Suggestions appear at the top of the tidy-up area with their reasons, to put away in one click or keep.
- When a lead @-mentions another room's lead by name (or room number) in `say`, the message goes to that room and wakes only that lead; `hosts` lists every room's lead.
- On the page, dragging a room next to a face of another room snaps them together (four sides on the floor, or stacked). Joined rooms see each other's whiteboard in `board`, read-only; each still edits only its own. Drag apart to separate.
- `--model` and `--effort` apply to that member only: a Codex member starts with that model and every turn carries the model and reasoning effort (the account is asked first whether it has the model and supports the effort). The account's `config.toml` is not changed.
- If `--account` names an account in `ROOMS_CLAUDE_HOMES`, a Claude member is started: `--model`, `--effort` (low to max) and `--permission-mode` (default `auto`) are available. It has no window; for each batch of messages the dispatcher runs `claude -p` on the same conversation inside `--cwd`. Room commands are always allowed and it replies with `room say`. Each turn's output is kept in `~/.local/state/agent-rooms/claude-members/logs/`; attach with the `claude --resume <id>` it prints.
- Offline members are not woken. A member brought back with `on` by the lead or the user is loaded in the background even if its window is closed; the member typing in its own window again just puts it back online.
- While the user is talking to a Codex conversation, the room waits for that turn to finish before delivering; if it is idle, delivery is immediate.
- `bin/room quota` shows every account's current quota and how many members it is running; `spawn` shows the same, and warns (without blocking) when one account is already running several members. When a member's context is 70% used, the dispatcher tells the lead to hand over to someone new.
- To be woken when a background job finishes, end the job's command with `room say "@<your own name> …"`. After three self-wakes in a row with no one else's message in between, it stops waking you, so nothing spins idle; a self-wake that did not get through is retried every 5 minutes, three times.
- When a lead calls on another room, the judge checks in the background which other rooms are relevant and records that next to the lead's choice. It only records; it does not change where the message goes.

### The page

Open `http://127.0.0.1:8775` in a browser. Each room is a transparent cube, and the dots inside are the conversations in it (★ marks the lead). Global roles and conversations not in a room sit in the status bar at the top. The wheel zooms around the cursor, dragging empty space pans (pinch on a phone), and "show everything" fits all rooms on screen.

- Click a room's top face to select it; the single input box at the bottom then types into that room. With no room selected you talk to the resident agent; click the resident or the maintainer to talk to it, click again to leave. `@name` works across rooms: @-mentioning someone in another room records the message in their room.
- New messages in a room pop up as bubbles. Click a bubble to open the chat window; closing it turns it back into a bubble, and the bubble's ✕ hides it for now. Select a room (or the resident or maintainer) and press "view chat" to bring it back.
- The chat window opens at the newest message and loads older ones as you scroll up. "Search" highlights every matching message in the room as you type (Enter for older, Shift+Enter for newer).
- Attachments: paste a screenshot anywhere on the page, or drop files onto the input bar, and they are attached to the next message. Agents attach with `room say "…" --file <path>`; recipients see the file's path on this machine.
- Drag a room's name to move it (positions are remembered). Drag a dot to another room to move it, onto ★ to make it lead, off ★ to step down, or onto the status bar to leave the room. Click a dot to rename it, describe what it does, set it online or offline, or set do-not-disturb. Creating, renaming and deleting rooms (once empty) are all on the page.
- Multi-select: Ctrl+click adds rooms to the selection, Ctrl+drag draws a selection box. Selected rooms move together and can be joined or deleted together.
- The quota column on the left can be hidden. It lists the accounts it finds; you can add, change (folder, warning line) or remove accounts there (kept in `~/.config/agent-rooms/accounts.json`, per machine).
- The balls on the right are registered local tools: open one in a narrow pane, expand it to a tab, or drag it onto a room to attach it (members see it with `room tools` and its code map with `room tools --map <id>`). Tools with a start command can be started, stopped and restarted from the page (needs `bin/room local`).
- Opened through port 8776, the page asks for a key first. Interaction check: `python3 tests/ui_check.py` (needs headless Chrome).

### Global roles

Global roles do not stay in any room. They see every room, and at the start of each turn they get the state of every room (lead, member status, pauses, lights, last few messages). They can move people, rename, set online or offline, rename rooms, lift a pause (`resume`), change the mutual-@ limit (`hops <room> <count|off>`), open a room with a given lead (`create <name> --owner <name>`) and `spawn`. They always speak as themselves, never as the user. The user assigns them:

```bash
bin/room global <name> maintainer --alias maintainer --user   # maintainer: works on the room tool itself
bin/room global <name> resident --alias steward --user        # resident agent: keeps the big picture, assigns rooms, does not change code
bin/room global <name> none --user                            # remove the role
```

| Role | When it is woken |
|---|---|
| `maintainer` | When @-mentioned (by name or alias), when a room pauses, when a message cannot be delivered |
| `resident` | When @-mentioned, when someone @-mentions the user (waiting on the user), when a room pauses |

With a maintainer present, every member's rules say "if the room tool breaks, @<maintainer's alias>". `resident` is for a resident agent (any agent that can use the hook or MCP). The room only gives it a place and permissions; "does not change code" is written in its rules, and actually enforcing that depends on the agent's own permission settings.

### Connecting over the network

The core has two entry points. Local `127.0.0.1:8775` is not authenticated and is only for programs on the same computer. `127.0.0.1:8776` always requires a key and is for requests forwarded from the network (for example `tailscale serve` forwarding a tailnet port here). Forwarded requests also look local, so the two are separated by port, not by source address.

```bash
bin/room key <name> --out ~/.config/agent-rooms/keys/<name>.key --user   # issue a key (local only); the secret is written only to a mode-600 file
bin/room key <name> --revoke --user                                      # revoke it
curl -H "Authorization: Bearer $(cat <key file>)" http://127.0.0.1:8776/api/overview
```

Remote identity comes from the key; an `as` claimed in the request does not count. A remote caller can only listen and acknowledge as itself; issuing keys and assigning global roles can only be done locally. The core stores only key hashes. A resident agent cannot delete rooms (only the user and the maintainer can).

## Environment variables

| Variable | Purpose |
|---|---|
| `ROOMS_USER_ID` | The user's id in rooms: `@<id>` reaches the user, and `--<id>` can replace `--user` on the command line; default `user`. Do not change it once set; old data uses the id from that time |
| `ROOMS_USER_NAME` | The user's display name, used in agent rules and notices; defaults to `ROOMS_USER_ID` |
| `ROOMS_OWNER_LABEL` | What a room's lead is called in agent rules and next to ★ (for example "lead"); default 負責人 |
| `ROOMS_MAX_HOPS` | How many agent-to-agent @-mentions in a row pause the room until the user shows up; default 60, 0 = no limit. Per room with `room hops` |
| `ROOMS_CLAUDE_OPEN` | When a Claude member's window is closed and someone calls it, the dispatcher runs this to open a new window on the conversation; `{script}` is the script to run, `{title}` the name. If unset, on WSL it opens a WSL tab in Windows Terminal; elsewhere it only logs |
| `ROOMS_WSL_DISTRO` | Which distribution that WSL tab uses; default the current one, else Ubuntu |
| `ROOMS_TZ` | Time zone for the command line and agent-facing times (IANA name, e.g. `Europe/Berlin`); default the system's. Data is always stored in UTC; the page uses the browser's time zone |
| `ROOMS_URL` | Core address, default `http://127.0.0.1:8775` |
| `ROOMS_DATA_DIR` | Data folder for the container, default `./data` |
| `ROOMS_JUDGE` | `off` turns the judge off (messages without an @ wake the lead instead) |
| `ROOMS_JUDGE_URL` / `ROOMS_JUDGE_MODEL` | Judge endpoint and model, default OpenRouter's `typesafe/jev-1.13` |
| `ROOMS_JUDGE_KEY` | Judge API key; falls back to `OPENROUTER_API_KEY` |
| `ROOMS_JUDGE_TIMEOUT` | Judge timeout in seconds, default 8 |
| `ACC_SESSIONS` | [acc](https://github.com/tenplus1000/acc)'s session folder, used to tell which conversation the user last spoke in |
| `ROOMS_WINDOWS_HOME` | The Windows user folder as seen from WSL (found automatically by default) |
| `ROOMS_CODEX_HOMES` | Colon-separated Codex accounts (`CODEX_HOME`), default `~/.codex`; each entry may be `name=path`, the name being what `spawn --account` takes |
| `ROOMS_SPAWN_ACCOUNTS` | The order `spawn` tries accounts in (names, colon-separated), moving on when one is out of quota; default follows `ROOMS_CODEX_HOMES` |
| `ROOMS_CLAUDE_HOMES` | Colon-separated Claude accounts (`CLAUDE_CONFIG_DIR`), same format as `ROOMS_CODEX_HOMES`, default `claude=~/.claude`; naming one in `spawn --account` starts a Claude member |
| `ROOMS_QUOTA_HELPER` | Optional: a folder with `codexquota.py` to decide whether a Codex account has quota left; if unset, the room's own `rooms/quota.py` is used |
| `ROOMS_ACCOUNTS_FILE` | Account list for the quota column, default `~/.config/agent-rooms/accounts.json`; without it accounts are found automatically |
| `ROOMS_CODEX_BIN` / `ROOMS_CLAUDE_BIN` | The `codex` / `claude` executable; default the account's own or the one on PATH |
| `ROOMS_SPAWN_WARN` | `spawn` warns when an account already runs this many members; default 2 |
| `ROOMS_CONTEXT_WARN` / `ROOMS_CLAUDE_CONTEXT` | At what % of context used the lead is told (default 70); a Claude member's context size (default 200000) |
| `ROOMS_HOST` / `ROOMS_PORT` / `ROOMS_REMOTE_PORT` | Address the core binds, the local port (8775), the key-only port (compose opens 8776; 0 = off) |
| `ROOMS_LOCAL_PORT` / `ROOMS_LOCAL_ORIGINS` | The local helper's port (8777) and the page origins it accepts (default the two spellings of 8775) |
| `ROOMS_ACC_REPO` / `ROOMS_JUDGMENT_LAYER` | Where the local helper finds the acc repo and the judgment layer folder |
| `ROOMS_DESKTOP_SESSIONS` | The Windows desktop Codex session folder (found automatically by default) |
| `ROOMS_RECORD_TURNS` | Set to `1` to record, for every local Claude conversation, each turn's last agent message and the user's next message in `<data>/turns/` (mode 600, this machine only); off by default |

The command line, the dispatcher, the MCP server and the core container all read `local.env` in the repo root (not committed); put local settings there.

## Tests

```bash
python3 -m unittest discover -s tests
python3 adapters/codex_cli/test_cli.py
python3 adapters/mcp/test_mcp.py
python3 tests/ui_check.py        # page interaction check, needs headless Chrome
```

## License

MIT, see [LICENSE](LICENSE). If agent-rooms helps your work, a mention or a link back is appreciated.
