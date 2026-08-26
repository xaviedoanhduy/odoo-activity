"""odoo-activity TUI — host stats, instances (with their dbs), activity pane.

The app here is just the shell: it lays out the rows and wires focus, selection
and the refresh timers. The system data lives in :mod:`odoo_activity.probes`;
the mode-switched instance-mode and db-mode tabs are in
:mod:`odoo_activity.panes.detail`.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import TYPE_CHECKING, ClassVar

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.theme import Theme
from textual.widgets import Footer, Label, ListItem, ListView, Static

from odoo_activity.host import Host, to_thread
from odoo_activity.panes.confirm import ConfirmScreen
from odoo_activity.panes.detail import ActivityPane
from odoo_activity.probes import (
    NEUTRALIZED,
    NOT_NEUTRALIZED,
    PARTIAL,
    Instance,
    container_host,
    databases_of,
    dump_and_parse_stacks,
    format_duration,
    instance_action,
    instance_status,
    instance_workdir,
    list_instances,
    match_odooly_env,
    neutralization_of,
    pg_target_of,
    read_cpu_times,
    read_host_stats,
    read_odooly_envs,
    signal_process,
)

# sort priority for the instances list: running first, then a failure state
# (systemd "failed", supervisor "exited"/"fatal"), then a clean "stopped"
_STATUS_ORDER = {"running": 0, "stopped": 2}

# --debug (see main.py) points this at a file. Silent (NullHandler-equivalent,
# nothing configured) otherwise, so normal runs pay nothing for this.
_log = logging.getLogger("odoo_activity")

_LAG_INTERVAL = 0.25  # how often the watchdog below ticks

# Trobz brand palette (see trobz brand-guidelines skill)
TROBZ_THEME = Theme(
    name="trobz",
    primary="#E54F0D",
    accent="#FFFFFF",  # distinct from primary so :focus borders are visible
    background="#1A110E",
    surface="#311E18",
    panel="#311E18",
    foreground="#FFFFFF",
    dark=True,
)


_PULSE_PERIOD = 1.6  # seconds, one full on/off cycle of a running dot


def _pulse_on(name: str) -> bool:
    """Whether `name`'s running dot is lit right now. The phase is offset per
    name so rows breathe on their own rather than in lockstep, and holds as
    long as the row keeps its name — which is what keeps it the same row."""
    offset = _PULSE_PERIOD * (hash(name) % 100) / 100
    return (time.monotonic() + offset) % _PULSE_PERIOD < _PULSE_PERIOD / 2


def _display_name(inst: Instance) -> str:
    """Instance name for display — `.service` is systemd-unit plumbing, not
    part of the name a user recognizes."""
    return inst["name"].removesuffix(".service")


def _db_field_width(name_width: int, uptime_width: int, indent: int) -> int:
    """How wide a db row's `dbname ... port` field is, so its right edge lands
    on the same column as the instance rows' uptime right edge (dot + space +
    name_width + space + the uptime field) regardless of `indent`."""
    return max(0, name_width + uptime_width + 1 - indent)


def _db_label(db: str, port: str | None, name_width: int, uptime_width: int, indent: int) -> str:
    """`dbname            port`, filling the field width above — not a fixed
    column. Padded even without a port, so whatever follows starts where the
    instance rows' status does."""
    width = _db_field_width(name_width, uptime_width, indent)
    if not port:
        return f"{db:<{width}}"

    pad = max(1, width - len(db) - len(port))
    return f"{db}{' ' * pad}[dim]{port}[/]"


def _odooly_marker(env: str | None) -> str:
    """The `ODOOLY` tag every db row carries once `--enable-odooly` is on:
    green when a section can reach it, red when none can. Both are shown —
    an absent marker reads as the flag not being passed, which is the one
    thing the tag exists to tell apart."""
    return "[green]ODOOLY[/]" if env else "[red]ODOOLY[/]"


# green: the database says it is neutralized and nothing on it can still
# reach the outside. yellow: those two disagree — a hand-written flag, or a
# cron switched back on after the fact. red: a live database.
_NEUTRALIZATION_TAGS = {
    NEUTRALIZED: "  [green]NEUTRALIZED[/]",
    PARTIAL: "  [yellow]PARTIALLY NEUTRALIZED[/]",
    NOT_NEUTRALIZED: "  [red]NOT NEUTRALIZED[/]",
}


def _neutralized_marker(state: str | None) -> str:
    """The neutralization tag on a db row.

    Empty while it is still unknown — the fetch hasn't landed, or psql
    couldn't read that db — since guessing either way is the one thing this
    tag exists to prevent.
    """
    return _NEUTRALIZATION_TAGS.get(state or "", "")


def _bar(pct: float, width: int = 24, red_at: float = 80, yellow_at: float = 50) -> str:
    """htop-style bar: green/yellow/red fill by load, dim track.

    Swap uses tighter thresholds than CPU/MEM — any swapping at all is a
    worse sign than the same percentage of CPU/RAM in use."""
    filled = min(width, round(pct / 100 * width))
    color = "red" if pct >= red_at else "yellow" if pct >= yellow_at else "green"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


class InstanceList(ListView):
    """The instances list, as the top of the three-zone arrow-key walk.

    `enter` opens the highlighted row's tabs. That, not `down`, is the way
    in: the list is a tree, so a row's own tabs have to be reachable from
    that row — an instance with databases nested under it is never the last
    row, and `down` there belongs to the row below it.

    `down` past the last row lands on the strip too, for the one row where
    there is nothing below to move to. See TabStrip for the rest of the walk.
    """

    if TYPE_CHECKING:

        @property
        def app(self) -> OdooActivity: ...

    def action_select_cursor(self) -> None:
        self.app.query_one(ActivityPane).focus_tabs()

    def action_cursor_down(self) -> None:
        if self.index is not None and self.index >= len(self.children) - 1 and self.app.databases_settled():
            self.app.query_one(ActivityPane).focus_tabs()
            return

        super().action_cursor_down()


class OdooActivity(App):
    CSS = """
    #body { height: 1fr; }

    #stats-row { height: 4; }
    .stat-panel { border: round $accent; width: 1fr; padding: 0 1; }
    .stat-title { width: 1fr; }
    .stat-value { width: auto; text-style: bold; }
    #uptime-text { height: 2; }

    #instances { border: round $accent; background: transparent; height: 6; }
    #activity { height: 1fr; }
    #instances:focus { border: round $primary; }
    /* fixed height 6 is right for the normal layout, but maximize should
       fill the screen like every other pane, not center at that height */
    #instances.-maximized { height: 1fr; }

    /* selected item stays visible whether or not its list has focus;
       color: auto keeps the text readable on top of the accent background */
    ListView { background: transparent; }
    ListView > ListItem.-highlight { background: $panel; color: auto; }
    ListView:focus > ListItem.-highlight { background: $accent; color: auto; }

    /* mouse text-selection: Textual defaults its foreground to transparent,
       which hides the selected text — force a readable one */
    .screen--selection { background: $primary; color: $text; }
    """

    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("s", "toggle_start_stop", "Start/Stop"),
        ("r", "restart", "Restart"),
        ("[", "prev_tab", "Prev tab"),
        ("]", "next_tab", "Next tab"),
        ("p", "select_tab('Top')", "Top"),
        ("p", "select_tab('Params')", "Params"),
        ("l", "select_tab('Logs')", "Logs"),
        ("l", "select_tab('Locks')", "Locks"),
        ("c", "select_tab('Config')", "Config"),
        ("c", "select_tab('Crons')", "Crons"),
        ("m", "select_tab('Mail')", "Mail"),
        ("t", "select_tab('Toolbox')", "Toolbox"),
        ("u", "select_tab('Users')", "Users"),
        ("j", "select_tab('Jobs')", "Jobs"),
        ("slash", "search", "Search"),
        ("K", "kill_process", "Kill -9"),
        ("L", "quit_process", "Log dump -3"),
        ("D", "dumpstacks", "Dump stacks"),
        ("S", "copy_shell_command", "Copy shell cmd"),
        ("A", "toggle_show_all", "All/active"),
        ("e", "toggle_config_mode", "Compact/Explain/Expand/Clean"),
        ("f", "toggle_maximize", "Maximize"),
        ("R", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        host: Host | None = None,
        *,
        include_sensitive_information: bool = True,
        enable_odooly: bool = False,
    ) -> None:
        super().__init__()
        self.host = host or Host()
        self.include_sensitive_information = include_sensitive_information
        self.enable_odooly = enable_odooly
        # read once, at startup: the file is the user's own and small, and a
        # row's marker must not depend on when it happened to be rendered
        self.odooly_envs = read_odooly_envs() if enable_odooly else []

    def compose(self) -> ComposeResult:
        with Vertical(id="body"):
            with Horizontal(id="stats-row"):
                with Vertical(id="cpu-panel", classes="stat-panel"):
                    with Horizontal():
                        yield Static("CPU", classes="stat-title")
                        yield Static("", id="cpu-pct", classes="stat-value")
                    yield Static("", id="cpu-bar")

                with Vertical(id="mem-panel", classes="stat-panel"):
                    with Horizontal():
                        yield Static("MEM", classes="stat-title")
                        yield Static("", id="mem-pct", classes="stat-value")
                    yield Static("", id="mem-bar")

                with Vertical(id="swap-panel", classes="stat-panel"):
                    with Horizontal():
                        yield Static("SWAP", classes="stat-title")
                        yield Static("", id="swap-pct", classes="stat-value")
                    yield Static("", id="swap-bar")

                with Vertical(id="uptime-panel", classes="stat-panel"):
                    yield Static("", id="uptime-text")

            yield InstanceList(id="instances")
            yield ActivityPane(id="activity")

        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(TROBZ_THEME)
        self.theme = "trobz"

        self.query_one("#instances", InstanceList).border_title = "Instances"

        # cheap synchronous seed -- instant locally; for a remote host the
        # real numbers land on the first refresh_host tick (see below),
        # this just avoids blocking the app's first paint on an ssh call
        self._cpu = read_cpu_times() if self.host.is_local else (0, 0)
        self._instances: dict[str, Instance] = {}
        self._instance_status: dict[str, str] = {}
        self._row_owner: dict[str, str] = {}  # row key -> owning instance key
        self._row_db: dict[str, str] = {}  # db row key -> db name
        self._db_cache: dict[str, tuple[list[str], str | None]] = {}  # instance key -> its (dbs, port)
        self._neutralized: dict[str, dict[str, dict]] = {}  # instance key -> {db: neutralization report}
        self._shown_key: str | None = None  # highlighted row driving the activity pane
        self._instances_ready = False  # first _rebuild_instances has finished mounting rows

        # spinner over the initial (possibly slow, over ssh) discovery only --
        # _rebuild_instances clears this once it lands and focuses the list
        # (a loading widget can't take focus -- see Widget._check_disabled);
        # membership-change rebuilds later never set loading, so they don't
        # blank already-shown rows or steal focus back
        self.query_one("#instances", InstanceList).loading = True
        self.refresh_instances()

        # also paces the Top tab, which rides this tick (ActivityPane.tick)
        self.set_interval(1.0 if self.host.is_local else 5.0, self.refresh_host)
        self.set_interval(0.5, self.query_one(ActivityPane).poll)
        # slower remotely (a tick is one ssh round trip per instance), but not
        # off: an externally started/stopped instance has to show up on its own
        self.set_interval(5.0 if self.host.is_local else 15.0, self.poll_instances)
        # samples the pulse, doesn't drive it — the phase is per row, so this
        # only has to be finer than _PULSE_PERIOD to render it smoothly
        self.set_interval(0.2, self._pulse_running)

        self._lag_tick = time.monotonic()
        self.set_interval(_LAG_INTERVAL, self._check_loop_lag)

    def _check_loop_lag(self) -> None:
        """Diagnostic-only: a `set_interval` timer fires late by exactly how
        long *something else* held the event loop synchronously in the
        meantime (a blocking widget mutation, a call that should've been
        `to_thread`d but wasn't) -- this is what makes keypresses feel
        dropped/delayed. Logs nothing unless --debug wired up a handler."""
        now = time.monotonic()
        drift = now - self._lag_tick - _LAG_INTERVAL
        self._lag_tick = now
        if drift > 0.05:
            _log.warning("event loop lag: %.0fms", drift * 1000)

    def on_show(self) -> None:
        """Run after layout is complete and app is shown."""
        self.refresh_host()

    def _get_bar_width(self, panel_id: str) -> int:
        """Get the available width for a bar in a stat panel."""
        try:
            panel = self.query_one(f"#{panel_id}", Vertical)
            return max(10, panel.size.width)
        except Exception:
            return 24

    def refresh_host(self) -> None:
        self._refresh_host()

    @work(exclusive=True, group="host-stats")
    async def _refresh_host(self) -> None:
        # local reads are a handful of /proc opens (microseconds) -- only
        # the remote branch needs a thread, so the once-a-second tick isn't
        # queuing on Python's shared, bounded to_thread executor for nothing
        stats = read_host_stats(self.host) if self.host.is_local else await to_thread(read_host_stats, self.host)
        if stats is None:
            return  # a bad remote round trip -- try again next tick, not fatal

        if not self.is_running:
            # this tick's worker got scheduled before quit/teardown started
            # and only ran after -- the widgets below are already gone
            return

        (total, idle), (mem_pct, swap_pct), (load1, load5, load15), uptime = stats
        d_total = total - self._cpu[0]
        d_idle = idle - self._cpu[1]
        self._cpu = (total, idle)

        cpu_pct = (1 - d_idle / d_total) * 100 if d_total else 0.0

        self.query_one("#cpu-pct", Static).update(f"{cpu_pct:4.1f}%")
        self.query_one("#cpu-bar", Static).update(_bar(cpu_pct, self._get_bar_width("cpu-panel")))
        self.query_one("#mem-pct", Static).update(f"{mem_pct:4.1f}%")
        self.query_one("#mem-bar", Static).update(_bar(mem_pct, self._get_bar_width("mem-panel")))
        self.query_one("#swap-pct", Static).update(f"{swap_pct:4.1f}%")
        self.query_one("#swap-bar", Static).update(
            _bar(swap_pct, self._get_bar_width("swap-panel"), red_at=10, yellow_at=1)
        )

        self.query_one("#uptime-text", Static).update(
            f"uptime     {format_duration(uptime)}\nload avg   {load1:.2f} {load5:.2f} {load15:.2f}"
        )

        self.query_one(ActivityPane).tick()

    def refresh_instances(self) -> None:
        """Rebuild the instances+dbs list (initial load / membership change)."""
        self._rebuild_instances()

    @work(exclusive=True, group="instances")
    async def _rebuild_instances(self) -> None:
        lv = self.query_one("#instances", InstanceList)
        first_load = not self._instances_ready
        keep = lv.highlighted_child.name if lv.highlighted_child else None
        await lv.clear()

        fresh_list = await to_thread(list_instances, self.host)

        # key by manager:name — the same name can exist under both managers
        statuses = {}
        for inst in fresh_list:
            key = f"{inst['manager']}:{inst['name']}"
            statuses[key] = await to_thread(instance_status, inst, self.host)

        # running first, then a failure state, then a clean stop
        fresh_list.sort(key=lambda inst: _STATUS_ORDER.get(statuses[f"{inst['manager']}:{inst['name']}"], 1))

        self._instances = {f"{inst['manager']}:{inst['name']}": inst for inst in fresh_list}
        self._row_owner = {}
        self._row_db = {}
        keys, items = [], []
        name_width = self._name_width()
        uptime_width = self._uptime_width()

        for inst in fresh_list:
            key = f"{inst['manager']}:{inst['name']}"
            self._instance_status[key] = statuses[key]
            self._row_owner[key] = key
            items.append(ListItem(Label(self._render_instance_row(inst, statuses[key])), name=key))
            keys.append(key)

            # only replayed from cache here — an uncached instance's dbs are
            # fetched when its row is highlighted (see _load_databases)
            for item in self._db_items(key, name_width, uptime_width):
                items.append(item)
                keys.append(item.name or "")

        if items:
            # await the mounts, else setting index races the append and the
            # highlight bar lands on nothing
            await lv.extend(items)
            lv.index = keys.index(keep) if keep in keys else 0

        self._instances_ready = True
        lv.loading = False
        if first_load:
            # a loading widget can't take focus (Widget._check_disabled), so
            # the on_mount focus() call landed while this was still disabled
            lv.focus()
        self._load_databases(self._highlighted_owner())

    def _db_items(self, key: str, name_width: int, uptime_width: int) -> list[ListItem]:
        """`key`'s db rows, built off `_db_cache` and registering their row
        mapping. Empty until that instance's dbs have actually been fetched."""
        cached = self._db_cache.get(key)
        if cached is None:
            return []

        names, port = cached
        items = []

        instance_name = self._instances[key]["name"] if key in self._instances else ""

        for db in names:
            db_key = f"{key}::db::{db}"
            self._row_owner[db_key] = key
            self._row_db[db_key] = db
            # two spaces off the filled field, the gap the instance rows put
            # between their uptime and their status: the tag reads as that
            # same column rather than as a ragged suffix of the db name
            marker = f"  {_odooly_marker(self.odooly_env_for(instance_name, db))}" if self.enable_odooly else ""
            neutral = _neutralized_marker(self._neutralized.get(key, {}).get(db, {}).get("state"))
            label = f"  [dim]└──[/] {_db_label(db, port, name_width, uptime_width, indent=4)}{neutral}{marker}"
            items.append(ListItem(Label(label), name=db_key))

        return items

    def odooly_env_for(self, instance_name: str, db: str) -> str | None:
        """The odooly env serving `db` on `instance_name`, or None when there
        is no match (or `--enable-odooly` was not passed, which leaves
        `odooly_envs` empty and every lookup unmatched)."""
        return match_odooly_env(instance_name, db, self.odooly_envs) if self.odooly_envs else None

    def highlighted_odooly_env(self) -> str | None:
        """The odooly env of the highlighted database row, if it has one."""
        hit = self.highlighted_db()

        return self.odooly_env_for(hit[0]["name"], hit[1]) if hit is not None else None

    def _highlighted_owner(self) -> str | None:
        item = self.query_one("#instances", InstanceList).highlighted_child
        return self._row_owner.get(item.name) if item is not None and item.name else None

    def databases_settled(self) -> bool:
        """True when the highlighted row's databases are already known.

        `_load_databases` runs on highlight and costs 3-4 ssh round trips, so
        remotely the last row is "last" only until they land. Without this,
        `down` there jumps to the tab strip and skips the rows about to
        appear -- the same keypress doing two different things depending on
        how fast the fetch was.
        """
        owner = self._highlighted_owner()
        return owner is None or owner in self._db_cache

    @work(exclusive=True, group="databases")
    async def _load_databases(self, key: str | None) -> None:
        """Fetch one instance's dbs and mount them under its row.

        On highlight, not for every instance up front: `databases_of` is ~3-4
        ssh round trips each, and the list is cleared before the rebuild, so
        remotely the pane stayed empty for the sum of all of them. `exclusive`
        means arrowing fast only fetches what you land on.
        """
        if key is None or key in self._db_cache:
            return

        inst = self._instances.get(key)
        if inst is None:
            return

        self._db_cache[key], self._neutralized[key] = await to_thread(self._fetch_databases, inst)
        await self._mount_databases(key)

    def _fetch_databases(self, inst: Instance) -> tuple[tuple[list[str], str | None], dict[str, dict]]:
        """The instance's dbs and their neutralization, in one thread hop.

        Both in a single `to_thread`, deliberately: each await in the worker
        is a point where an arrow keypress starts the next (exclusive) fetch
        and cancels this one — mid-way, after the cache was filled but
        before the rows were mounted, which leaves an instance showing no
        databases at all.

        `pg_target_of` rather than the port `databases_of` returns: a docker
        instance's postgres also needs its container address and credentials
        to be reachable.
        """
        dbs = databases_of(inst, self.host)
        neutralized = neutralization_of(dbs[0], pg_target_of(inst, self.host), self.host) if dbs[0] else {}
        return dbs, neutralized

    async def _mount_databases(self, key: str) -> None:
        """Replace `key`'s db rows in place with what `_db_cache` now holds."""
        lv = self.query_one("#instances", InstanceList)
        rows = [(i, item.name or "") for i, item in enumerate(lv.children)]

        row = next((i for i, name in rows if name == key), None)
        if row is None:
            return  # rebuilt out from under the fetch; the rebuild replays the cache itself

        stale = [i for i, name in rows if name != key and self._row_owner.get(name) == key]
        if stale:
            await lv.remove_items(stale)
            row -= sum(1 for i in stale if i < row)

        await lv.insert(row + 1, self._db_items(key, self._name_width(), self._uptime_width()))

    def _name_width(self) -> int:
        """Name column width, sized to the longest instance currently shown
        (some real unit names run past the old fixed 24, which misaligned
        every row's uptime/status against a longer neighbour)."""
        if not self._instances:
            return 24
        return max(24, max(len(_display_name(inst)) for inst in self._instances.values()))

    def _uptime_width(self) -> int:
        """Uptime column width, sized to the longest uptime currently shown.

        `format_duration`'s `<D>d HH:MM:SS` grows past a fixed width once an
        instance has been up for days — a hardcoded width just misaligned
        the db rows' port column against it once that happened.
        """
        if not self._instances:
            return 10
        return max(10, max(len(inst["uptime"]) for inst in self._instances.values()))

    def _render_instance_row(self, inst: Instance, status: str) -> str:
        dot = self._dot(status, inst["name"])
        color = {"running": "green", "stopped": "dim"}.get(status, "red")
        width = self._name_width()
        uptime_width = self._uptime_width()
        return f"{dot} {_display_name(inst):<{width}} {inst['uptime']:>{uptime_width}}  [{color}]{status.upper()}[/]"

    def _dot(self, status: str, name: str) -> str:
        if status == "stopped":
            return "○"
        if status == "running":
            return "[green]●[/]" if _pulse_on(name) else " "
        return "[red]●[/]"  # failed / exited / fatal

    def _pulse_running(self) -> None:
        """Redraw the running dots in place — a cheap re-render off the
        cached state, no process polling (that's poll_instances' job)."""
        try:
            listview = self.query_one("#instances", InstanceList)
        except NoMatches:  # the 0.2s tick can land mid-shutdown, after the tree unmounts
            return
        for item in listview.children:
            inst = self._instances.get(item.name or "")
            if inst is None:  # a db row, not an instance row
                continue

            label = next(iter(item.query(Label)), None)
            if label is not None:
                label.update(self._render_instance_row(inst, self._instance_status.get(item.name or "", "stopped")))

    def poll_instances(self) -> None:
        """Refresh the running marks in place so an external start/stop shows up.

        Rebuilds the list only when the set of instances changes — otherwise it
        just re-labels, leaving selection, the db rows and the log/top views
        untouched.

        A no-op until the first `_rebuild_instances` has finished: it shares
        this worker's exclusive group, so firing before that finishes would
        cancel it mid-build (before anything ever mounts) and immediately
        re-trigger another rebuild that's just as likely to run past this
        timer's own next tick — a livelock, most visible over a slow ssh
        round trip where the initial build can genuinely take longer than
        the 5s interval.
        """
        if not self._instances_ready:
            return
        self._poll_instances()

    @work(exclusive=True, group="instances")
    async def _poll_instances(self) -> None:
        fresh_list = await to_thread(list_instances, self.host)
        fresh = {f"{i['manager']}:{i['name']}": i for i in fresh_list}
        if set(fresh) != set(self._instances):
            self.refresh_instances()
            return

        self._instances = fresh
        for item in self.query_one("#instances", InstanceList).children:
            inst = fresh.get(item.name or "")
            if inst is not None:
                self._instance_status[item.name or ""] = await to_thread(instance_status, inst, self.host)

        await self._retry_empty_docker_databases()

    async def _retry_empty_docker_databases(self) -> None:
        """Refetch docker instances whose cached db list came back empty.

        A first fetch right after `docker compose up` races the db container's
        own startup: postgres isn't reachable yet, so `databases_by_role`
        degrades to `[]` and that empty list is what gets cached -- forever,
        since locally nothing invalidates it. A non-empty cache is left alone,
        so this never disturbs (or resets the selection on) rows the user is
        browsing.

        Only running instances: a stopped project's empty list is the right
        answer, and retrying it would copy a config out of a dead container
        every tick for as long as it stays down.
        """
        for key, (names, _port) in list(self._db_cache.items()):
            inst = self._instances.get(key)
            if names or inst is None or inst["manager"] != "docker" or inst["status"] != "running":
                continue

            fetched, neutralized = await to_thread(self._fetch_databases, inst)
            if fetched[0]:
                self._db_cache[key] = fetched
                self._neutralized[key] = neutralized
                await self._mount_databases(key)

    def current_instance(self) -> Instance | None:
        item = self.query_one("#instances", InstanceList).highlighted_child
        if item is None or item.name is None:
            return None

        owner = self._row_owner.get(item.name)
        return self._instances.get(owner) if owner else None

    def highlighted_db(self) -> tuple[Instance, str] | None:
        """(instance, db name) if a db row is highlighted, else None."""
        item = self.query_one("#instances", InstanceList).highlighted_child
        if item is None or item.name is None:
            return None

        db = self._row_db.get(item.name)
        if db is None:
            return None

        inst = self._instances.get(self._row_owner[item.name])
        return (inst, db) if inst else None

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = self.query_one("#instances", InstanceList).highlighted_child
        key = item.name if item is not None else None

        if key != self._shown_key:
            self._shown_key = key
            self._load_databases(self._row_owner.get(key) if key else None)
            hit = self.highlighted_db()

            if hit is not None:
                inst, db = hit
                self.query_one(ActivityPane).show_database(inst, db)
            else:
                self.query_one(ActivityPane).show_instance(self.current_instance())

        self.refresh_bindings()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # start/stop/restart only make sense on the instances pane, so their
        # footer entries appear/disappear as focus moves (see check_action)
        self.refresh_bindings()

    async def on_event(self, event: events.Event) -> None:
        # Logged here, not in on_key: keys bubble up from the focused widget
        # and Input.stop()s every printable one, so on_key can't see typing at
        # all. Count these against the acsearch value length to tell a key
        # that never arrived from one the app dropped.
        if isinstance(event, events.Key) and not event.is_forwarded:
            _log.debug("key %r focused=%r", event.key, self.focused)

        await super().on_event(event)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # False hides the footer key entirely (None just dims it — Textual's
        # Screen.active_bindings skips on `is False`, not falsy)
        if action in ("toggle_start_stop", "restart"):
            focused = self.focused
            return bool(focused is not None and focused.id == "instances")

        # a tab shortcut only makes sense if its name is one of the current
        # mode's tabs (instance-mode, db-mode); "l"/"c" each bind two names,
        # gated here so only the active one shows/fires
        if action == "select_tab":
            (name,) = parameters
            return self.query_one(ActivityPane).has_tab(str(name))

        if action == "search":
            return self.query_one(ActivityPane).has_search()

        if action == "kill_process":
            # Top lists processes as a table, Processes as a tree; both can
            # name the pid under the cursor, so both can kill it
            pane = self.query_one(ActivityPane)
            return pane.is_top_active() or pane.is_processes_active()

        if action == "quit_process":
            return self.query_one(ActivityPane).is_top_active()

        # a db row resolves to its owning instance (see _row_owner), so these
        # two need the mode as well to stay out of database mode
        if action in ("dumpstacks", "copy_shell_command"):
            return self.query_one(ActivityPane).is_instance_mode() and self.current_instance() is not None

        if action == "toggle_show_all":
            return self.query_one(ActivityPane).has_show_all()

        if action == "toggle_config_mode":
            return self.query_one(ActivityPane).is_config_active()

        return True

    def action_prev_tab(self) -> None:
        pane = self.query_one(ActivityPane)
        pane.prev_tab()
        pane.focus_active()
        self.refresh_bindings()

    def action_next_tab(self) -> None:
        pane = self.query_one(ActivityPane)
        pane.next_tab()
        pane.focus_active()
        self.refresh_bindings()

    def action_select_tab(self, name: str) -> None:
        pane = self.query_one(ActivityPane)
        pane.select_tab_by_name(name)
        pane.focus_active()
        self.refresh_bindings()

    def action_search(self) -> None:
        self.query_one(ActivityPane).open_search()

    def action_toggle_show_all(self) -> None:
        self.query_one(ActivityPane).toggle_show_all()

    def action_toggle_maximize(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif self.focused is not None:
            self.screen.maximize(self.focused)

    def action_toggle_start_stop(self) -> None:
        inst = self.current_instance()
        if inst is None:
            return

        running = self._instance_status.get(f"{inst['manager']}:{inst['name']}") == "running"
        self._instance_action("stop" if running else "start")

    def action_restart(self) -> None:
        self._instance_action("restart")

    def _instance_action(self, action: str) -> None:
        inst = self.current_instance()
        if inst is None:
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._run_instance_action(action)

        self.push_screen(
            ConfirmScreen(f"{action.capitalize()} {inst['name']} ({inst['manager']})?"),
            on_confirm,
        )

    @work(exclusive=True, group="instance-action")
    async def _run_instance_action(self, action: str) -> None:
        inst = self.current_instance()
        if inst is None:
            return

        name, manager = inst["name"], inst["manager"]

        # a remote systemctl takes seconds and nothing changes on screen until
        # the re-label below, so the confirmation reads as not having registered
        self.app.notify(f"{action} {name}…", timeout=2)

        error = await to_thread(instance_action, name, action, manager, self.host, inst.get("workdir"))
        if error:
            self.app.notify(error, severity="warning", timeout=3)
        else:
            self.app.notify(f"{action} {name}: done", timeout=2)

        self.poll_instances()  # re-label in place; keeps selection, no flicker

    def process_host(self) -> Host:
        """`self.host`, narrowed to the highlighted instance's container
        when it has one: a pid only means anything in the namespace it was
        read from, and for docker that's the container's, not the box's."""
        inst = self.current_instance()
        return self.host if inst is None else container_host(inst, self.host)

    def action_kill_process(self) -> None:
        pane = self.query_one(ActivityPane)
        proc = pane.selected_process()
        if proc is None:
            # the Processes tree groups its leaves by role: a group node (or
            # the "Loading…" placeholder) names no pid, and `K` there would
            # otherwise look like a dead key
            if pane.is_processes_active():
                self.notify("Select a process, not a group", severity="warning", timeout=3)
            return

        if proc.get("kind") == "pg":
            return

        async def on_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return

            # the pid came out of the instance's own namespace, so the
            # signal has to go back into it (see Host.in_container)
            await to_thread(signal_process, proc["pid"], signal.SIGKILL, self.process_host())
            # Processes only, and deliberately: it is a snapshot taken when
            # the tab was opened, so the pid just killed would sit there
            # until reopened. Top re-polls every tick on its own, and
            # re-rendering it here would blank its table back to
            # "Loading top…" for a poll — a step backwards, not a refresh.
            pane = self.query_one(ActivityPane)
            if pane.is_processes_active():
                pane.refresh_active()

        self.push_screen(ConfirmScreen(f"Kill PID {proc['pid']}?"), on_confirm)

    async def action_quit_process(self) -> None:
        """Send SIGQUIT (kill -3) — the process dumps a traceback to its
        logfile — then jump to Stacks (the last dump's parsed view; raw text
        is still one tab over, on Logs).

        Odoo rows only: a postgres backend isn't ours to signal directly
        (use the DB tools' own termination, not SIGQUIT/SIGKILL)."""
        proc = self.query_one(ActivityPane).selected_process()
        if proc is None or proc.get("kind") == "pg":
            return

        await to_thread(signal_process, proc["pid"], signal.SIGQUIT, self.process_host())
        pane = self.query_one(ActivityPane)
        pane.select_tab_by_name("Stacks")
        pane.focus_active()

    def action_toggle_config_mode(self) -> None:
        self.query_one(ActivityPane).toggle_config_mode()

    def action_refresh(self) -> None:
        """Refresh the active tab now, rather than waiting out its timer.

        Remotely it also re-polls the instance list, on a slower tick there,
        and refetches the highlighted instance's dbs, which are cached until
        asked."""
        if not self.host.is_local:
            self.poll_instances()
            key = self._highlighted_owner()
            self._db_cache.pop(key or "", None)
            self._load_databases(key)

        self.query_one(ActivityPane).refresh_active()

    def action_dumpstacks(self) -> None:
        inst = self.current_instance()
        if inst is None:
            return
        self.app.notify(f"Dumping stacks for {inst['name']}…", timeout=2)
        self._run_dumpstacks(inst)

    @work(exclusive=True, group="dumpstacks")
    async def _run_dumpstacks(self, inst: Instance) -> None:
        """Trigger a stack dump, parse it into the Stacks tab (the point:
        surfacing what's actually long-running without the user having to
        guess which worker first), then jump there."""
        error, workers = await to_thread(dump_and_parse_stacks, inst, self.host)
        activity = self.query_one(ActivityPane)

        workdir = await to_thread(instance_workdir, inst, self.host)
        if error:
            self.app.notify(error, timeout=3)
        elif not activity.render_stacks(inst, workers, workdir):
            self.app.notify("dump ok — nothing long-running", timeout=3)
        activity.select_tab_by_name("Stacks")
        activity.focus_active()

    def action_copy_shell_command(self) -> None:
        inst = self.current_instance()
        if inst is None:
            return
        self._copy_shell_command(inst)

    @work(exclusive=True, group="shell-command")
    async def _copy_shell_command(self, inst: Instance) -> None:
        await self.query_one(ActivityPane).copy_shell_command(inst, self.host)


def run() -> None:
    OdooActivity().run()
