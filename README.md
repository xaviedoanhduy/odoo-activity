# odoo-activity

A terminal UI for Odoo instances, on this machine or on a remote host over
ssh. One screen: host cpu/mem/uptime, every Odoo instance (`systemd --user`,
`supervisor` or docker compose) with its databases nested underneath, and a
detail pane for process/log/db inspection.

Full docs: <https://trobz.github.io/odoo-activity>

## Installation

```bash
uv tool install odoo-activity
```

Or with pip:

```bash
pip install odoo-activity
```

## Usage

```bash
oa                        # this machine
oa odoo@somehost          # a remote host over ssh
```

See [Getting Started][docs-getting-started] for the full quick example,
[Keybindings & Tabs][docs-keybindings] for every key and what each tab
shows, and [MCP Server][docs-mcp] for `oa-mcp`/`oa-mcp-multi` — the same
data exposed to an agent.

The Params tab shows `ir_config_parameter` secret-looking values unmasked
by default — you already have a shell on this host. Pass
`--no-include-sensitive-information` to keep odoo-db's own masking instead.

| Key | Action |
| --- | --- |
| `↑`/`↓` | move through instances and their nested dbs |
| enter | open the highlighted row's tabs (from the instances list) |
| `←`/`→` | switch tab (while the tab strip has focus) |
| `s` / `r` | start/stop toggle / restart (confirm popup) |
| `[` / `]` | switch tab in the detail pane |
| `f` | maximize/minimize the focused pane |
| `p` / `l` / `c` / `t` | Top / Logs / Config / Toolbox |
| `u` / `l` / `j` / `c` / `m` / `p` | Users / Locks / Jobs / Crons / Mail / Params |
| `K` | kill -9 the selected process (Top and Processes tabs, confirm popup) |
| `L` | kill -3 the selected process, then jump to Stacks (Top tab) |
| `D` | dump stacks of all workers, then jump to Stacks |
| `S` | copy the instance's `odoo shell` launch command to the clipboard |
| `e` | cycle compact/explain/expand/clean (Config tab) |
| `A` | show all rows, inactive ones included |
| enter | run the selected tool (Toolbox tab, confirm popup) / open a Jobs group / open a row's raw json (db tabs) |
| escape | back out of a Jobs group, or of a row's raw json |
| `/` | search |
| `R` | refresh the active tab now |
| `q` | quit |

Two tabs on each side have no letter shortcut — cycle to them with
`[`/`]` or click: **Processes** and **Stacks** (instance mode), **Queries**
and **Modules** (database mode).

`A` asks `odoo-db` for the rows it filters out by default (its `--all`
flag). Against a host whose `odoo-db` predates that flag, the tab falls
back to the default rows and `A` says so instead of doing nothing.

### Moving around

Three zones, walked with the arrow keys: the instances list, the tab strip,
and the tab body.

```
instances list  ──enter, or ↓ off the last row──►  tab strip  ──↓──►  tab body
       ▲                                              ▲  │              │
       └──────────────────── ↑ ───────────────────────┘  └───── ↑ ──────┘
                                                          (at its top row)
```

`enter` is the way in rather than `↓`, because the list is a tree: an
instance with databases nested under it is never the last row, and `↓` there
belongs to the row below it — which is a database, carrying the other mode's
tabs. On the strip, `←`/`→` move between tabs and `↑` goes back to the list;
in the body, `↑` at the top row goes back to the strip, and anywhere else it
scrolls as usual.

While the pane is maximized (`f`) the strip keeps `↑` to itself — the list
isn't on screen to go back to, and `f` is what leaves that view.

The letter shortcuts and `[`/`]` still jump straight to a tab from anywhere,
and `Tab`/`Shift+Tab` still cycle focus.

### Jobs (`j`)

queue_job's jobs grouped by function and state, numbered, with the oldest
creation date and the longest wait/run in each group — which is what a job
stuck in `started` for hours looks like. Enter opens a group as its
individual jobs (numbered too, `date_created`/`date_started` each, oldest
first, capped at 500), escape backs out, and enter on one of those opens its
raw json.

Under the table is the tab's action strip — buttons that act on the
database rather than on the row under the cursor, so they are not rows
themselves. Jobs has one: **Requeue jobs** puts every `started`/`enqueued`
job back to `pending` (after a confirm popup — including jobs a live worker
is still running, which will then run again), clearing the dates that go
with those states the way queue_job's own `set_pending` does — what a runner
does for its own dead jobs at startup, for when a worker was killed mid-job
and nothing else will revisit the row. It's offered even when the table above is empty, and the strip is
hidden entirely on a tab that has no actions.

The Processes tab lists the queue_job runner as its own role. Odoo only
labels a worker in `ps` when `setproctitle` is installed — with it, that
label is the whole answer and costs nothing. Without it, the runner is found
by its postgres connection instead: `application_name` names the pid outright
from Odoo 16.0 on, and before that (where odoo never set it) the connection
is traced by its TCP endpoint, `ss` or `lsof` saying which process holds the
client port. An instance on a unix socket reports no port and can't be traced
that way; if nothing can account for it, the runner just stays under HTTP
Worker.

### Odooly (experimental)

`oa --enable-odooly` reads `~/odooly.ini` at startup and matches each
database against it. Every database then carries an `ODOOLY` tag in the
instance rows' status column — green where an environment reaches it, and
the actions that need a login appear with it; red where none does, so a
database missing from the ini is visible rather than silent.

Matching is by name: the instance's, stripped of what only a process manager
adds (`odoo-acme18-integration.service` → `acme18-integration`), against
the section names — spelled either way (`-integration` / `-int`, `-staging` /
`-stag`, `-production` / `-prod`), and with a suffix allowed, since a
multi-db instance is usually configured one section per database
(`acme18-int-db1`). A section that names a `database` only matches that one.

Database > Toolbox then offers:
- Open odooly — copies `odooly -c ~/odooly.ini --env <env>` to the clipboard
  (`-c`, because odooly's own CLI looks for the ini in the working directory).
- Restore app icons — for a database restored without its filestore, where
  the apps menu comes up blank. It rewrites `web_icon` on the menus whose
  icon data is missing, which is what makes Odoo recompute the image from
  the module's own file; the ones that are fine are left alone, so running
  it twice is a no-op.

Jobs grows a **Create test job** button next to Requeue, which queues one
of queue_job's own test jobs to see whether a runner picks it up, and Mail
grows a **Send test mail** button, which prompts for a recipient and sends
one real email (calling `.send()` directly, so it goes out synchronously
rather than waiting on the mail queue cron) from the connecting user's own
company address — for checking outbound mail actually reaches an inbox,
not just that it queues. Mail always shows a **Check port 25** button too (no
odooly needed — a plain network probe, not an authenticated Odoo action):
`nc -z -w 3 localhost 25` on the target host, the question that matters
once `mail_servers` is empty and Odoo falls back to `localhost:25` for
outgoing mail. `-z` (scan, no data exchange) and the timeout keep it from
hanging forever if the port turns out to be open.

All three scripts live in `odoo_activity/scripts/` and run on their own too:

```bash
python -m odoo_activity.scripts.restore_app_icons --env acme18-int
python -m odoo_activity.scripts.create_test_job --env acme18-int
python -m odoo_activity.scripts.send_test_mail --env acme18-int --to me@example.com
```

They always run on **this** machine, even when `oa` is watching a remote
host: odooly reaches the instance over the network, using the `~/odooly.ini`
that is here, not there.

Toolbox (`t`) offers four tools:
- Spin a worker up (`SIGTTIN`) or down (`SIGTTOU`).
- Open shell — which copies the launch command instead of signaling, so it needs
  no confirm.
- Count sessions under the instance's data dir (walks the filesystem, may be
  slow).

### Remote hosts

The target is any ssh destination — `[user@]host` or a `~/.ssh/config`
alias. Only the tools already required locally are needed, but on the
remote host. Connections are multiplexed, so the first call opens the
session and the rest reuse it.

Everything still refreshes on its own against a remote host, just on a
slower tick — host stats and Top every 5s, the instance list every
15s. `R` refreshes the active tab immediately, plus the instance list and
the highlighted instance's databases.

## MCP server

`oa-mcp [host]` exposes the same read-only data as an MCP server, for an
agent to work an investigation alongside a human on `oa [host]` — both
looking at the same target. Every tool call is pinned to `host` (local if
omitted); a `host`/`ssh_port` argument on a tool call must match the pin
or is rejected.

`db_query`'s `params` output is masked by default, unlike the TUI's: a tool
call has no human at the screen, and the plaintext would land in the agent's
context. Unmasking is launch-time only, via `--include-sensitive-information`
on the `oa-mcp`/`oa-mcp-multi` command line — never a per-call tool
argument, so no tool call can turn it on itself. `mail_audit` (outbound mail
config — neutralization status, config parameters, alias domains,
addresses, outgoing mail servers, mass_mailing state) follows the same
rule for `smtp_user`/`smtp_pass`. It's a separate tool rather than another
`db_query` command: odoo-db's `mail` answers one nested object, not the
flat row list every `db_query` command shares — the same reason the TUI
renders it through its own `panes/mail.py` instead of the generic table
pane.

`--enable-odooly` is the same launch-time-only pattern, for the one
non-read-only exception: `list_odooly_envs`, `instance_odooly_env`, and
`odooly_run_script` match a database against `~/odooly.ini` and run the
packaged scripts (`create_test_job`, `restore_app_icons`, `send_test_mail`
— the last needs `to`), the same actions the TUI's `oa --enable-odooly`
offers a human through the Toolbox — now callable by the agent directly.

`oa-mcp-multi` instead leaves the target per-call, capped by
`--host-filter` (an odoo dbfilter-style regex; unset means unrestricted)
and `--host-file` (which `~/.ssh/config`-style file reads aliases from).

Both default to the `stdio` transport (spawned by the MCP client); add
`--transport streamable-http --bind-host ... --bind-port ...` to run as a
network server instead.

## Managers

An instance's `manager` — `systemd`, `supervisor`, `odoosh` or `docker` — is
discovered per instance, not configured, and decides which controller
process/log/start-stop-restart lookups route through:

- **`systemd`** — a `systemd --user` unit, controlled via `systemctl --user`.
- **`supervisor`** — a `supervisorctl status` program, controlled via
  `supervisorctl`.
- **`odoosh`** — the odoo.sh build a host is running, when odoo-activity
  itself runs directly on that host (installed via `requirements.txt` at
  build time, same as `odoo-config`/`odoo-db`). One host is one build, so
  there's nothing to enumerate — the whole box is "the instance". Start/stop
  isn't supported (odoo.sh handles sleep/wake on its own); restart goes
  through `odoosh-restart`, needed on `PATH` — which ships pre-installed on
  odoo.sh hosts.
- **`docker`** — a docker compose project running Odoo, doodba-shaped or
  not. One project is one instance (the odoo container and its postgres are
  two halves of the same thing), named after the project; a project running
  several odoo services shows one row each, as `<project>/<service>`.
  Stopped projects are listed too, so `s` can start them.

  Everything is probed **inside** the odoo container — `ps`, the config
  file, signals — because that is where the instance's pids, paths and
  logs actually are. The database tabs reach postgres over the compose
  network, using the address of the db container and the credentials from
  the container's own `odoo.conf`, so nothing has to be published to the
  host. Logs come from `docker logs` (an odoo image writes to stdout, not
  to a logfile). Start/stop/restart go through the project's own
  `invoke start|restart` when it has a `tasks.py` — doodba's, which is what
  a developer already drives it with — and fall back to `docker compose`
  otherwise. Stopping always uses `docker compose stop`: doodba's own
  `invoke stop` is `docker compose down`, which deletes the containers, and
  the instance would then vanish from the list instead of reading
  `stopped`, with nothing left to start it from.

  **Limitation: Linux only.** Everything above assumes the docker daemon
  runs on the same kernel as the box odoo-activity is probing, which is
  true on Linux and on a remote Linux server over ssh. On Docker Desktop
  (macOS/Windows) the containers live inside a VM: `docker ps` still
  answers, but the compose network isn't routable from the host, so the
  database tabs won't connect. Fixing that means publishing the db port
  and reading it back from `docker port` — not implemented yet.

### Config tab modes

`e` cycles the Config tab through `odoo-config`'s `compact`/`explain`/
`expand`/`clean` views of the highlighted instance's config file — see
[odoo-config's CLI docs][odoo-config-cli] for what each one shows.

`ODOO_ACTIVITY_DB_ROLE` overrides the postgres role used to resolve an
instance's databases (default: the instance's `db_user`, falling back to
its name).

## Architecture

```
odoo_activity/
├── host.py            # local vs ssh command dispatch
├── probes.py          # all system data: no Textual import, shared by the TUI and MCP server
├── mcp_server.py      # oa-mcp / oa-mcp-multi: probes.py as a read-only MCP tool API
├── panes/detail.py    # ActivityPane: the one stateful rendering widget
├── panes/processes.py   # Processes tab: workers grouped by role
├── panes/stacks.py    # Stacks tab: parsed dumpstacks, busy-first
├── panes/mail.py       # Mail tab: one Rich table per section, into the log body
├── scripts/           # odooly actions the Toolbox shells out to (network, not host)
└── tui.py             # app shell: layout, list, timers, actions
```

- **`host.py`** — a `Host` is this machine or an ssh destination. Every probe
  takes one and runs the same way against either, so nothing above this
  layer knows whether it is local or remote.
- **`probes.py`** — pure functions, no UI. Every `systemctl`/`supervisorctl`/
  `ps`/`psql` call and `/proc` read lives here, returning plain dicts/lists
  so it's testable without spinning up a screen. An instance's databases,
  logfile and top all resolve from **one config**: its
  `<workdir>/config/{odoo.conf,server.conf}`.
- **`mcp_server.py`** — thin `@mcp.tool()` wrappers over `probes.py`, no
  logic of its own; the same data the TUI shows, for an agent instead of a
  human (see [MCP server](#mcp-server)).
- **`panes/detail.py`** — `ActivityPane`, the one stateful render widget: a
  tab strip over a Log/DataTable/Tree, mode-switched by whatever's
  highlighted (see Modes below) — not a separate popup screen. Delegates
  the Processes, Stacks, and Mail tab bodies to `panes/processes.py`/
  `panes/stacks.py`/`panes/mail.py`.
- **`tui.py`** — the shell only: `compose()` layout, the nested instances+dbs
  `ListView`, focus/highlight wiring, refresh timers, start/stop/restart.
  Delegates rendering to `ActivityPane`, data to `probes.py`,
  confirm popups to `panes/confirm.py`'s `ConfirmScreen` (shared with
  `ActivityPane`, which also confirms mutating actions like Toolbox).

### Modes

`ActivityPane` mode-switches on whatever's highlighted in the instances list:

- **Instance mode** — an instance row is highlighted. Tabs: Top,
  Processes, Stacks, Logs, Config, Toolbox.
- **Database mode** — one of its nested database rows is highlighted. Tabs:
  Queries, Users, Locks, Jobs, Crons, Mail, Modules, Params, Toolbox.

Both modes share the same tab strip and Log/DataTable widgets (just a
`_mode` flag), and several letter-key shortcuts are reused across them for
whichever tab they map to in each (e.g. `l` is Logs in instance mode, Locks
in database mode).

### Data sources

- **Instances** — `systemctl --user list-units`, `supervisorctl status` and
  `docker ps` (compose labels), merged by name.
- **Databases** — each instance's `<workdir>/config/{odoo.conf,server.conf}`
  gives a db role (or `ODOO_ACTIVITY_DB_ROLE`); `psql` lists the databases owned
  by that role. A container's config names its own role, address and
  password instead, and `ODOO_ACTIVITY_DB_ROLE` (a convention of *this*
  box's cluster) deliberately doesn't apply to it.
  Each db row then carries its neutralization status, from four signals in
  one query: the `database.is_neutralized` flag the db claims, plus whether
  Odoo's stub mail relay is in place and whether any real mail server or
  cron is still active. The stub is only expected from Odoo 16, where
  `neutralize.sql` first ships — on 14/15 the flag is set by whatever copied
  the database, with no stub to find. Green `NEUTRALIZED` is claimed *and*
  confirmed —
  nothing on it reaches the outside. Red `NOT NEUTRALIZED` is a live
  database, treat every action as production. Yellow `PARTIALLY
  NEUTRALIZED` is the case the extra signals pay for: the flag says safe
  but something can still fire (a flag written by hand, a neutralization
  that died halfway, a cron switched back on afterwards) — treat as live
  until someone looks. On top of the `base` tables it checks eight module
  surfaces a neutralized database must not still have live — payment
  providers, IAP credits, fetchmail, mail templates pinned to a relay,
  WhatsApp, VoIP, bank feeds, signing certificates — each condition copied
  from that module's own `data/neutralize.sql`. Those tables are absent on
  most databases and naming a missing one would fail the whole statement, so
  they go through `query_to_xml` behind a `to_regclass` guard; the checks
  that hang off `res_company`/`res_users` (sms_twilio, microsoft_calendar,
  `l10n_*` EDI credentials) are deliberately left out, since those tables
  exist everywhere while their columns come and go with the module. A mail
  catcher (mailhog, mailpit, maildev) is not counted as a live relay: it
  accepts mail and never relays it, which is what the Mail tab already says
  about it. The report also carries `checked`, the surfaces actually
  evaluated — without it a misspelled table name and a module that isn't
  installed look identical, and a typo would retire a check silently.
  It costs one extra round trip for the whole list (a psql
  connection per db, sent as one shell loop), and a db postgres couldn't
  answer for carries no tag at all rather than a guess. The MCP
  `instance_databases` tool returns the whole report — state plus the raw
  signals — as a `neutralized` map.
- **Top** — the manager gives the instance's master pid (`systemctl ...
  -p MainPID` / `supervisorctl pid`); `ps -eo pid,ppid,user,%mem,args` is then
  walked down the ppid tree from there to find every worker.
- **Logs** — the same config gives `logfile`, tailed by reading backward in
  fixed-size chunks from the end so a multi-GB file costs a few reads, not a
  full scan. A container has no logfile: `docker logs` gives the snapshot and
  `docker logs -f` the stream.
- **Config** — read-only: `odoo-config {compact,explain,expand,clean}` is run
  against the instance's config file and its plain-text stdout is shown as-is;
  the version passed to it comes from `odoo-addons-path <workdir> --verbose
  --format json`'s `version` key.
- **Params** — `odoo-db params <db>` reads `ir_config_parameter`; `/` filters
  rows by key or value. Values are shown as they are: odoo-db masks
  secret-looking ones (`password`, `token`, an `enterprise_code`, ...) as
  `********` by default, so the TUI always runs it with
  `--include-sensitive-information`.
- **Mail** — `odoo-db mail <db>` audits outbound mail config (config
  parameters, per-company alias domains, addresses, outgoing mail servers,
  relevant modules) as one nested object rather than a flat row list.
  Unlike every other db tab, it doesn't go through the generic table
  renderer: the sections don't share columns, so `panes/mail.py` renders
  each non-empty one as its own table in the log body instead (`/` search
  and the generic DataTable are unused here). Outgoing mail servers is
  shown first — whether mail leaves the box at all is the most important
  question — with test-catcher/known-relay/neutralization-stub detection
  surfaced as summary lines below the table rather than per-row columns.
  Mail always shows a **Check port 25** button alongside **Send test
  mail** (see the Jobs/Mail actions paragraph above). A neutralized
  database (`database.is_neutralized` — every odoo.sh staging build)
  leads with its own red banner, since it's the single most
  common reason mail never leaves an Odoo database at all.
  `--enable-odooly` grows a **Send test mail** button (see Odooly below).

[odoo-config-cli]: https://github.com/trobz/odoo-config/blob/main/CLI.md
[docs-getting-started]: ./site-docs/docs/getting-started.md
[docs-keybindings]: ./site-docs/docs/keybindings.md
[docs-mcp]: ./site-docs/docs/mcp.md
