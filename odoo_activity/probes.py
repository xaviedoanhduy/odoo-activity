"""System probes for odoo-activity — pure data, no TUI.

Everything the panes read about the host, its Odoo instances and their
databases lives here so it stays testable without spinning up Textual.
"""

from __future__ import annotations

import configparser
import contextlib
import ipaddress
import itertools
import json
import os
import platform
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyperclip

# typing_extensions, not typing: pydantic (via FastMCP, see mcp_server.py)
# can't build a schema from typing.TypedDict on Python < 3.12.
from typing_extensions import TypedDict

from odoo_activity.host import LOCAL, Host

CLK_TCK = os.sysconf("SC_CLK_TCK")

# Some envs (like odoo.sh) export PATH with an unexpanded `~` (e.g., `~/.local/bin`).
# While shells auto-expand this, os.execvp / subprocess.run treat it as a literal
# string, making those binaries invisible. Expand it manually for this process tree.
os.environ["PATH"] = os.pathsep.join(os.path.expanduser(p) for p in os.environ.get("PATH", "").split(os.pathsep))


def _parse_uptime(text: str) -> float:
    return float(text.split()[0])


def read_uptime() -> float:
    """System uptime in seconds, from /proc/uptime."""
    with open("/proc/uptime") as f:
        return _parse_uptime(f.read())


def _parse_loadavg(text: str) -> tuple[float, float, float]:
    one, five, fifteen = text.split()[:3]
    return float(one), float(five), float(fifteen)


def read_loadavg() -> tuple[float, float, float]:
    """1/5/15-minute load averages, from /proc/loadavg."""
    return _parse_loadavg(Path("/proc/loadavg").read_text())


def format_duration(seconds: float) -> str:
    """`H:MM:SS`, or `<D>d HH:MM:SS` past a day."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{hours}:{minutes:02d}:{secs:02d}"


def _parse_cpu_times(line: str) -> tuple[int, int]:
    vals = [int(x) for x in line.split()[1:]]
    idle = vals[3] + vals[4]  # idle + iowait
    return sum(vals), idle


def read_cpu_times() -> tuple[int, int]:
    """Return (total, idle) jiffies from /proc/stat."""
    with open("/proc/stat") as f:
        return _parse_cpu_times(f.readline())


def _parse_mem(text: str) -> tuple[float, float]:
    info: dict[str, int] = {}
    for line in text.splitlines():
        key, rest = line.split(":", 1)
        info[key] = int(rest.split()[0])  # kB

    mem_pct = (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"] * 100
    swap_total = info["SwapTotal"]
    swap_pct = (swap_total - info["SwapFree"]) / swap_total * 100 if swap_total else 0.0

    return mem_pct, swap_pct


def read_mem() -> tuple[float, float]:
    """Return (mem_used_pct, swap_used_pct) from /proc/meminfo."""
    with open("/proc/meminfo") as f:
        return _parse_mem(f.read())


def read_host_stats(
    host: Host = LOCAL,
) -> tuple[tuple[int, int], tuple[float, float], tuple[float, float, float], float] | None:
    """(cpu_times, mem_pcts, loadavg, uptime) — the 4 separate local /proc
    reads for a local host, or one batched round trip for a remote one.

    None on a failed/timed-out remote round trip (a bad connection, not a
    parse bug) — this runs on a per-second timer forever, so the caller
    should just skip that tick's update rather than treat it as fatal.
    """
    if host.is_local:
        return read_cpu_times(), read_mem(), read_loadavg(), read_uptime()

    script = "head -1 /proc/stat; echo ---; cat /proc/meminfo; echo ---; cat /proc/loadavg; echo ---; cat /proc/uptime"
    result = host.run(["sh", "-c", script])
    if result.returncode != 0:
        return None

    parts = result.stdout.split("---\n", 3)
    if len(parts) != 4:
        return None

    stat_txt, mem_txt, load_txt, uptime_txt = parts
    return _parse_cpu_times(stat_txt), _parse_mem(mem_txt), _parse_loadavg(load_txt), _parse_uptime(uptime_txt)


_SUPERVISOR_STATUS = {
    "RUNNING": "running",
    "STARTING": "running",
    "STOPPED": "stopped",
    "STOPPING": "stopped",
    "UNKNOWN": "stopped",
    "BACKOFF": "fatal",
    "EXITED": "exited",
    "FATAL": "fatal",
}

_SYSTEMD_STATUS = {"active": "running", "failed": "failed"}

# systemd --user sidecar units that ride alongside an instance's own unit,
# named after it (e.g. backup-odoo-acme-production.service next to
# odoo-acme-production.service) so `_is_odoo` matches their name too —
# without this they'd show up as extra, never-runnable "instances".
_SIDECAR_UNIT_PREFIXES = ("backup-", "logrotate-")


def _is_odoo(*text: str) -> bool:
    """True if any hint names an Odoo instance (odoo or the legacy openerp)."""
    blob = " ".join(text).lower()
    return "odoo" in blob or "openerp" in blob


def _is_sidecar_unit(unit_id: str) -> bool:
    """True for a systemd --user unit that manages a task *for* an Odoo
    instance (backups, log rotation) rather than being the instance itself."""
    return unit_id.lower().startswith(_SIDECAR_UNIT_PREFIXES)


def list_instances(host: Host = LOCAL) -> list[Instance]:
    """All Odoo instances on `host`, from systemd --user, supervisor, odoo.sh,
    docker compose and directly-run processes.

    Each row carries its `manager` so actions route to the right controller;
    managers can even expose the same name (e.g. odoo-demo).
    """
    return (
        systemd_instances(host)
        + supervisor_instances(host)
        + odoosh_instances(host)
        + docker_instances(host)
        + local_instances(host)
    )


def instance_status(inst: Instance, host: Host = LOCAL) -> str:
    """The instance's corrected status: `running`, `stopped`, or a manager
    failure state (`failed`/`exited`/`fatal`).

    A manager may report "stopped" while a bare shell runs it, so a live
    process promotes an ambiguous *stopped* report to running. An explicit
    failure (systemd "failed", supervisor "exited"/"fatal") is authoritative
    even if a process serving the same db is alive — `procs_of` matches by
    db name, not manager, so that process may belong to the *other*
    manager's instance of the same name/db (see `list_instances`).
    """
    if inst["status"] == "running":
        return "running"
    if inst["status"] == "stopped" and procs_of(inst, host):
        return "running"
    return inst["status"]


def systemd_instances(host: Host = LOCAL) -> list[Instance]:
    """Odoo instances from systemd --user units.

    Uses list-unit-files (catches stopped units, which list-units hides) then
    one batched `show` to read Description + state for each. Matches the unit
    name or Description so name-convention units (openerp-*.service) are
    caught. Sidecar units for a given instance (backup-*, logrotate-*) match
    the same way but aren't instances themselves, so they're dropped.
    """
    # --user only; add system-wide (`systemctl` without --user) when a
    # host needs it.
    try:
        files = host.run([
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service",
            "--no-legend",
            "--plain",
            "--no-pager",
        ]).stdout
    except FileNotFoundError:
        return []

    # drop template units (foo@.service) — `show` errors out on them
    units = [tok for tok in files.split() if tok.endswith(".service") and not tok.endswith("@.service")]
    if not units:
        return []

    show_cmd = [
        "systemctl",
        "--user",
        "show",
        *units,
        "-p",
        "Id",
        "-p",
        "Description",
        "-p",
        "ActiveState",
        "-p",
        "ActiveEnterTimestampMonotonic",
        "-p",
        "ActiveEnterTimestamp",
    ]
    if not host.is_local:
        # force a parseable weekday regardless of the remote's locale --
        # _systemd_active_secs's remote branch parses ActiveEnterTimestamp
        show_cmd = ["env", "LC_ALL=C", *show_cmd]
    out = host.run(show_cmd).stdout
    instances: list[Instance] = []

    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = props.get("Id", "")
        if not _is_odoo(name, props.get("Description", "")) or _is_sidecar_unit(name):
            continue

        status = _SYSTEMD_STATUS.get(props.get("ActiveState", ""), "stopped")
        uptime = "-"
        if status == "running" and (secs := _systemd_active_secs(props, host)) is not None:
            uptime = format_duration(secs)

        instances.append({"name": name, "status": status, "uptime": uptime, "manager": "systemd"})

    return instances


def _systemd_active_secs(props: dict[str, str], host: Host) -> float | None:
    """Seconds since a systemd unit's ActiveEnterTimestamp{,Monotonic}.

    Local: diffs the Monotonic property (microseconds since boot,
    CLOCK_MONOTONIC) against our own clock — excludes suspend time, so a
    resumed laptop doesn't overstate uptime by its sleep duration.

    Remote: CLOCK_MONOTONIC isn't queryable over ssh without another round
    trip, and /proc/uptime (CLOCK_BOOTTIME) isn't a safe stand-in for it —
    proven wrong against a real host where the two had diverged by months
    (a negative uptime). Diffs the wall-clock property against our own wall
    clock instead; NTP-synced closely enough for a rough uptime display.
    """
    if host.is_local:
        entered = int(props.get("ActiveEnterTimestampMonotonic", "0") or 0)
        if not entered:
            return None
        return time.clock_gettime(time.CLOCK_MONOTONIC) - entered / 1_000_000

    ts = props.get("ActiveEnterTimestamp", "")
    if not ts:
        return None
    # systemd emits a bare 2-digit UTC offset ("+07") when the minutes are
    # :00, and "UTC" literally for zero offset -- %z needs a 4-digit one.
    ts = re.sub(r" UTC$", " +0000", ts)
    ts = re.sub(r"([+-]\d{2})$", r"\g<1>00", ts)
    try:
        entered_at = datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return time.time() - entered_at.timestamp()


# supervisor programs are declared one-per-file here on servers; the
# [program:x] section carries `directory=` (the instance's odoo dir).
# Below is the standard path on the server.
SUPERVISOR_CONFD = Path("/opt/openerp/supervisor/conf.d")


def supervisor_instances(host: Host = LOCAL) -> list[Instance]:
    """Odoo instances under supervisor.

    Names + `directory`/`command` come from the conf.d programs; running state
    comes from `supervisorctl status`. Works with either source alone — a host
    without the conf.d layout still lists what supervisorctl reports.
    """
    states = _supervisor_states(host)
    confs = _supervisor_confs(host)
    instances: list[Instance] = []

    for name in sorted(set(states) | set(confs)):
        if not _is_odoo(name):
            continue

        conf = confs.get(name, {})
        st = states.get(name, {"status": "stopped", "uptime": "-"})
        instances.append({
            "name": name,
            "status": st["status"],
            "uptime": st["uptime"],
            "manager": "supervisor",
            "command": conf.get("command", ""),
            "directory": conf.get("directory", ""),
        })

    return instances


def _supervisor_states(host: Host = LOCAL) -> dict[str, dict[str, str]]:
    """program -> {status, uptime} from `supervisorctl status` (skips the
    pkg_resources banner and any non-status lines). `uptime` is supervisor's
    own `H:MM:SS`/`D:HH:MM:SS` text, lifted straight out of the status line —
    only RUNNING/STARTING programs carry one. Returns {} if supervisor isn't
    installed on this host."""
    try:
        out = host.run(["supervisorctl", "status"]).stdout
    except FileNotFoundError:
        return {}

    states = {}

    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2 or parts[1] not in _SUPERVISOR_STATUS:
            continue

        rest = parts[2] if len(parts) > 2 else ""
        uptime = rest.rsplit("uptime", 1)[1].strip(" ,") if "uptime" in rest else "-"
        states[parts[0]] = {"status": _SUPERVISOR_STATUS[parts[1]], "uptime": uptime}

    return states


def _supervisor_confs(host: Host = LOCAL) -> dict[str, dict[str, str]]:
    """program -> {command, directory} parsed from SUPERVISOR_CONFD/*.conf."""
    confs = {}

    for path in host.glob(str(SUPERVISOR_CONFD / "*.conf")):
        parser = configparser.RawConfigParser(strict=False)  # supervisor uses %
        try:
            parser.read_string(host.read_text(path))
        except configparser.Error:
            continue

        section = next((s for s in parser.sections() if s.startswith("program:")), None)
        if section is None:
            continue

        confs[section.split(":", 1)[1].strip()] = {
            "command": parser.get(section, "command", fallback="").strip(),
            "directory": parser.get(section, "directory", fallback="").strip(),
        }

    return confs


# odoo.sh: no systemd/supervisor, one build per host — discovered from the
# env vars its shell sources (PGDATABASE et al.) rather than any
# process-manager listing. Odoo.sh sets these system-wide, not just in an
# interactive login shell's rc file, so a plain `ssh host env` (no `-l`
# needed) sees them too — verified against a live odoo.sh instance.
def _odoosh_env(host: Host = LOCAL) -> dict[str, str] | None:
    """This host's odoo.sh build env, or None off odoo.sh."""
    if host.is_local:
        db = os.environ.get("PGDATABASE")
        version = os.environ.get("ODOO_VERSION", "")
    else:
        out = host.run(["sh", "-c", 'echo "$PGDATABASE"; echo "$ODOO_VERSION"']).stdout
        db, _, version = out.partition("\n")

    if not db:
        return None
    return {"db": db, "version": version.strip()}


def _proc_uptime(pid: str, host: Host = LOCAL) -> float | None:
    """Seconds `pid` has been running, via /proc/<pid>'s own wall-clock
    mtime (process-creation time, NTP-close-enough) against our wall clock —
    None if it's gone.

    Not /proc/<pid>/stat's starttime (clock ticks since boot) diffed against
    /proc/uptime: both are boot-relative, so they're only consistent if they
    share the same boot reference — true on a bare host, but odoo.sh's
    /proc/uptime reflects the shared build node's uptime (weeks/months),
    while a container's own top can start ticks-since-boot counting
    from a much more recent point, so the subtraction came out as the node's
    uptime, not the worker's. Same divergence class as the systemd
    ActiveEnterTimestamp/CLOCK_BOOTTIME fix noted in host.py's history —
    wall clock instead of a boot-relative clock that isn't guaranteed to be
    shared across a container boundary.
    """
    started = host.stat_mtime(f"/proc/{pid}")
    return None if started is None else time.time() - started


def odoosh_instances(host: Host = LOCAL) -> list[Instance]:
    """The single build `host` is running, when it's odoo.sh.

    One SSH-accessible odoo.sh host is one build, not several — "the
    instance" is just "this box", nothing to enumerate. `uptime` tracks the
    live `odoo-bin` worker rather than the build itself: odoo.sh spawns and
    reaps workers on demand, so "-" here means idle (no worker alive right
    now), not stopped.
    """
    env = _odoosh_env(host)
    if env is None:
        return []

    uptime = "(idle)"
    if (pid := _odoosh_master_pid(host)) is not None and (secs := _proc_uptime(pid, host)) is not None:
        uptime = format_duration(secs)

    return [
        {
            "name": env["db"],
            "status": "running",
            "uptime": uptime,
            "manager": "odoosh",
            "db": env["db"],
            "version": env["version"],
        }
    ]


def _odoosh_master_pid(host: Host = LOCAL) -> str | None:
    """The pid of odoo.sh's top-level odoo process, or None.

    PID 1 is the container init and reaps unrelated orphans, so the tree can't
    just be walked down from there — `_odoo_roots` picks the master out by
    argv instead. One odoo.sh host is one build, so the first root is the
    only one.
    """
    roots = _odoo_roots(_ps_snapshot(host)[0])
    return roots[0] if roots else None


# `_is_odoo` alone is too loose here (`vi odoo/tools/config.py` matches), so
# it only gates a narrower test: an odoo entry point, or a flag only odoo
# takes. `-c` is excluded from those — `sh -c` would match it.
_ODOO_ENTRYPOINTS = {"odoo", "odoo-bin", "openerp-server", "openerp-gevent"}
# `-d`/`--database` are out: psql and pg_dump take them too
_ODOO_FLAGS = {"--config", "--addons-path"}
# our own toolchain: every one has "odoo" in its name and is typically run
# from a shell, so each would otherwise look like an instance root
_OUR_TOOLS = ("odoo-activity", "odoo-config", "odoo-db", "odoo-addons-path")
# odoo's own process title, when setproctitle is installed: `odoo: WorkerHTTP
# 1234`. It replaces argv wholesale, so none of the entry-point/flag tests
# below match it -- the prefix is the whole evidence, and it is odoo's own.
_ODOO_TITLE_PREFIX = "odoo: "
# a root owned by one of these is already listed by that manager
_MANAGER_PARENTS = ("systemd --user", "supervisord")
# a containerized odoo is `docker_instances`' to list, not this manager's:
# it needs the container's own pid namespace and filesystem, and a compose
# project to act on (see docker_instances). Excluded here so it isn't
# listed twice, once with half the features working.
_CONTAINER_RUNTIMES = ("containerd-shim", "dockerd", "podman", "runc", "crun")

# `<project>/18.0` is a version directory, not an instance name
_VERSION_DIR_RE = re.compile(r"^\d+\.\d+$")


def _looks_like_odoo(cmd: str) -> bool:
    """True if `cmd` is an odoo server process (any runner: `odoo-bin`, an
    egg-installed `odoo` console script, a venv wrapper around either)."""
    # ps title, not argv (`sshd-session: odoo [priv]`) -- "odoo" there is the user
    if cmd.split(" ", 1)[0].endswith(":"):
        return False
    if cmd.startswith("postgres:") or not _is_odoo(cmd) or any(tool in cmd for tool in _OUR_TOOLS):
        return False

    tokens = cmd.split()
    # only what runs the program can name it: `psql -U odoo postgres` carries
    # an `odoo` token too, but as a flag's value
    program = itertools.takewhile(lambda tok: not tok.startswith("-"), tokens)

    return any(tok.rsplit("/", 1)[-1] in _ODOO_ENTRYPOINTS for tok in program) or any(
        tok.split("=", 1)[0] in _ODOO_FLAGS for tok in tokens
    )


# a python interpreter runs whatever script comes next, so it never names
# the program itself — `python .../bin/odoo` is odoo, `python .../bin/pew` is not
_PYTHON_RE = re.compile(r"^python[\d.]*$")
# systemd's per-user manager: an ancestor of everything in a user session,
# service unit and terminal alike, so it never identifies the unit itself
_USER_MANAGER_RE = re.compile(r"^user@\d+\.service$")


def _entry_point(cmd: str) -> str:
    """The program `cmd` actually runs: its first non-flag word, skipping a
    leading python interpreter (which only names the script that follows)."""
    words = (tok.rsplit("/", 1)[-1] for tok in cmd.split() if not tok.startswith("-"))

    return next((word for word in words if not _PYTHON_RE.match(word)), "")


def _runs_odoo_itself(cmd: str) -> bool:
    """True if `cmd`'s own entry point is odoo, rather than a wrapper that
    goes on to spawn it (`pew in <venv> odoo ...`, `poetry run odoo ...`).

    Narrower than `_looks_like_odoo`, which matches a wrapper too because
    odoo is named somewhere in its argv. The difference matters only when we
    signal: a wrapper is a plain process with no SIGQUIT handler, so the
    dumpstacks signal kills it instead of dumping (and takes the shell that
    started it down with it)."""
    return _entry_point(cmd) in _ODOO_ENTRYPOINTS


def _odoo_master(pid: str, by_pid: dict[str, ProcRow], children: dict[str, list[str]]) -> str:
    """The odoo master under `pid` — `pid` itself once it runs odoo directly,
    else the single odoo child a wrapper execs into, following a chain of
    them (`pew` spawns a python that spawns odoo).

    Only a child that runs a *different* program is a wrapper's exec; one
    that shares its parent's entry point is a fork of it, i.e. a worker. That
    keeps the descent off a master `_runs_odoo_itself` doesn't recognize (a
    custom launcher script, which `_looks_like_odoo` still matches on
    `--config` alone): with a single worker under it, stepping down would
    otherwise hand every signal and every field on the row to that worker.

    Falls back to `pid` when the chain forks or dies out, since there's then
    no better candidate to point at."""
    while not _runs_odoo_itself(by_pid[pid]["cmd"]):
        kids = [kid for kid in children.get(pid, []) if kid in by_pid and _looks_like_odoo(by_pid[kid]["cmd"])]
        if len(kids) != 1 or _entry_point(by_pid[kids[0]]["cmd"]) == _entry_point(by_pid[pid]["cmd"]):
            return pid
        pid = kids[0]

    return pid


def _is_odoo_process(cmd: str) -> bool:
    """True if `cmd` belongs to odoo, by argv or by odoo's own process title.

    `_looks_like_odoo` reads argv, which setproctitle overwrites: a worker
    renamed to `odoo: WorkerJobRunner 1234` carries neither an entry point
    nor a flag any more, and would otherwise read as "not odoo" — which is
    backwards, since that title is odoo naming itself.
    """
    return cmd.startswith(_ODOO_TITLE_PREFIX) or _looks_like_odoo(cmd)


def _containerized(pid: str, by_pid: dict[str, ProcRow]) -> bool:
    """True if `pid`'s ancestry passes through a container runtime — the
    whole chain, since an entrypoint shell often sits between the shim and
    odoo. Reads the ps snapshot only; blind to a runtime we don't name,
    `/proc/<pid>/cgroup` is the definitive test if that shows up."""
    while (row := by_pid.get(pid)) is not None:
        if any(runtime in row["cmd"] for runtime in _CONTAINER_RUNTIMES):
            return True
        pid = row["ppid"]

    return False


def _odoo_roots(by_pid: dict[str, ProcRow]) -> list[str]:
    """Pids that start an odoo process tree: an odoo process whose parent
    isn't one too. Prefork workers and the `gevent` child hang off these, so
    one root is one instance."""
    odoo = {pid: row for pid, row in by_pid.items() if _looks_like_odoo(row["cmd"])}
    return [pid for pid, row in odoo.items() if row["ppid"] not in odoo]


def _runs_under_unit(cgroup: str) -> bool:
    """True if `cgroup` (the contents of `/proc/<pid>/cgroup`) puts its
    process inside a systemd service unit.

    False when it runs under a scope instead (`vte-spawn-….scope` for a
    terminal, `session-3.scope` for a login) — nobody started it as a
    service. A cgroup we can't read or don't understand answers True:
    "don't know" has to keep the old parent-based answer rather than risk
    listing a real unit twice.

    Only systemd's own hierarchy answers this: v2's unified `0::` line, or
    the `name=systemd` line under v1. The other v1 controllers are mounted
    separately and commonly stop at the delegated user-manager cgroup
    (`…/user@1000.service`), whose leaf would otherwise read as a unit and
    put every terminal-started instance back in systemd's hands.
    """
    paths = [line.split(":", 2) for line in cgroup.splitlines() if line.count(":") >= 2]
    path = next(
        (parts[2] for parts in paths if parts[0] == "0" and not parts[1]),  # v2: `0::<path>`
        next((parts[2] for parts in paths if parts[1] == "name=systemd"), None),  # v1
    )
    if path is None:
        return True

    leaf = path.rstrip("/").rsplit("/", 1)[-1]

    return leaf.endswith(".service") and not _USER_MANAGER_RE.match(leaf)


def _cgroups_of(pids: list[str], host: Host = LOCAL) -> dict[str, str]:
    """`/proc/<pid>/cgroup` for each pid — one `cat` each locally, one ssh
    round trip for the lot remotely (same batching as `proc_cpu_ticks_many`,
    for the same reason: `list_instances` re-runs on a timer, and a host
    running several units would pay a round trip per unit per poll).

    A pid missing from the result is one whose cgroup couldn't be read.
    """
    if host.is_local:
        texts = {}
        for pid in pids:
            with contextlib.suppress(OSError):
                texts[pid] = host.read_text(f"/proc/{pid}/cgroup")
        return texts

    if not pids:
        return {}

    marker = "\x1e"  # ASCII record separator -- won't appear in /proc/*/cgroup
    script = ";".join(f"echo {marker}{pid}; cat /proc/{pid}/cgroup 2>/dev/null" for pid in pids)
    out = host.run(["sh", "-c", script]).stdout

    texts = {}
    for block in out.split(marker)[1:]:
        pid, _, data = block.partition("\n")
        if data.strip():
            texts[pid.strip()] = data
    return texts


def _owned_by_manager(parent: ProcRow, cgroup: str | None) -> bool:
    """True if `parent` is the process manager actually running the process
    whose `cgroup` this is, so that manager's own listing already covers it.

    `systemd --user` needs more than the parent's name: it reaps orphans as
    a subreaper, so a directly-run instance is reparented onto it the moment
    its wrapper or shell exits, and looked "owned" by it from then on — which
    dropped the row here while `systemd_instances` never listed it either
    (it has no unit), losing the instance entirely. The cgroup is what
    doesn't move on reparenting, so that's what decides. supervisord reaps
    nothing, so being its child is ownership enough.
    """
    if not any(mgr in parent["cmd"] for mgr in _MANAGER_PARENTS):
        return False

    return "supervisord" in parent["cmd"] or _runs_under_unit(cgroup or "")


def local_instances(host: Host = LOCAL) -> list[Instance]:
    """Odoo instances started directly — a shell, `emoi start`, a venv
    runner — rather than registered with a process manager.

    The other three managers each have a registry to ask (unit properties,
    conf.d + supervisorctl, build env vars); here the process *is* the
    identity, and its argv stands in for the config a registry would name.
    Roots owned by systemd or supervisord are dropped, since those already
    list themselves; so are containerized ones, unless `ODOO_ACTIVITY_DOCKER=1`.

    A wrapper that execs odoo in a venv (`pew in <venv> odoo ...`) matches
    too and becomes the root instead of the odoo process it spawned, so
    `_odoo_master` steps down to the odoo process itself: the row has to
    carry a pid that survives being signalled, and the wrapper does not.

    Two runners can serve the same db, which `_local_name` alone can't tell
    apart; those rows carry their master pid, the rest stay pid-free so a
    restart doesn't look like a different instance.
    """
    by_pid, children = _ps_snapshot(host)
    instances: list[Instance] = []
    roots = []

    found = _odoo_roots(by_pid)
    parents = {root: by_pid.get(by_pid[root]["ppid"]) for root in found}
    # only a manager-parented root needs its cgroup read, and reading them
    # together keeps a remote host to one round trip for all of them
    cgroups = _cgroups_of(
        [root for root, parent in parents.items() if parent is not None and "systemd" in parent["cmd"]], host
    )

    for root in found:
        parent = parents[root]
        if parent is not None and _owned_by_manager(parent, cgroups.get(root)):
            continue

        if _containerized(root, by_pid):
            continue

        pid = _odoo_master(root, by_pid, children)
        cwd = _proc_link(pid, "cwd", host)
        options = _cli_options(by_pid[pid]["cmd"])
        roots.append((pid, options, cwd, _local_name(options, cwd)))

    taken = Counter(name for *_, name in roots)

    for pid, options, cwd, name in roots:
        cmd = by_pid[pid]["cmd"]
        secs = _proc_uptime(pid, host)
        instances.append({
            "name": name if taken[name] == 1 else f"{name} [{pid}]",
            "status": "running",
            "uptime": format_duration(secs) if secs is not None else "-",
            "manager": "local",
            "command": cmd,
            "directory": cwd or "",
            "config": _abspath(options.get("config"), cwd) or "",
            "pid": pid,
        })

    return instances


def _local_name(options: dict[str, str], cwd: str | None) -> str:
    """A directly-run instance's display name: its db name, else the
    directory it was launched from.

    Not the pid — that changes on every restart, and `list_instances` re-runs
    on a timer, so the name is what keeps a row *the same row* across polls;
    `local_instances` appends one only to break a tie.
    """
    if db := options.get("db_name"):
        return db

    if not cwd:
        return "local"

    path = Path(cwd)
    return path.parent.name if _VERSION_DIR_RE.match(path.name) else path.name


def _abspath(path: str | None, cwd: str | None) -> str | None:
    """`path` resolved against the process's own cwd — a directly-run
    instance's `--config` is usually relative to wherever it was launched.
    None when it's relative and that cwd can't be read."""
    if not path:
        return None
    if path.startswith("/"):
        return path
    return str(Path(cwd) / path) if cwd else None


# docker compose stamps these on every container it creates, and they are
# the whole identity model here: one compose project is one instance, the
# way one unit is for systemd. Reading them off `docker ps` costs one call
# for every container on the box -- `docker inspect` would be one more
# round trip for the same three strings.
_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_SERVICE = "com.docker.compose.service"
_COMPOSE_WORKDIR = "com.docker.compose.project.working_dir"

# Tab-separated: a container's command has spaces in it, its name never
# does, so splitting on tabs keeps the command whole without quoting.
_DOCKER_PS_FORMAT = "\t".join((
    "{{.Names}}",
    "{{.State}}",
    "{{.Image}}",
    "{{.Command}}",
    f'{{{{.Label "{_COMPOSE_PROJECT}"}}}}',
    f'{{{{.Label "{_COMPOSE_SERVICE}"}}}}',
    f'{{{{.Label "{_COMPOSE_WORKDIR}"}}}}',
))

# The service a doodba project runs postgres as, plus the image any compose
# file would use for it -- either is enough, so a project that renamed the
# service still resolves as long as the image is recognisable.
_DB_SERVICES = ("db", "postgres", "database")
_DB_IMAGE_MARKER = "postgres"


class _ContainerRow(TypedDict):
    name: str
    state: str
    image: str
    command: str
    project: str
    service: str
    workdir: str


def _docker_ps(host: Host = LOCAL) -> list[_ContainerRow]:
    """Every compose-managed container on `host`, running or not.

    `-a`: a stopped instance still has to be listed, so `s` can start it —
    same as a stopped systemd unit. Containers with no compose project
    label are dropped: a hand-run `docker run odoo` has no project
    directory to run `invoke`/`docker compose` in, so it can't be
    controlled, and this manager's identity is the project name.

    `--no-trunc`: `docker ps` otherwise clips `.Command` to 20-odd
    characters, which is shorter than doodba's entrypoint path alone —
    `_is_odoo_process` would then never see the `odoo` token it matches on.
    """
    try:
        out = host.run(["docker", "ps", "-a", "--no-trunc", "--format", _DOCKER_PS_FORMAT])
    except FileNotFoundError:
        return []  # no docker on this box, same degradation as a missing supervisorctl

    rows: list[_ContainerRow] = []
    for line in out.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 7:
            continue

        name, state, image, command, project, service, workdir = fields
        if not project:
            continue

        # `docker ps` quotes .Command; the quotes are its formatting, not
        # part of the argv, and would otherwise sit in front of the
        # entrypoint path where _is_odoo_process looks for a program name.
        rows.append({
            "name": name,
            "state": state,
            "image": image,
            "command": command.strip('"'),
            "project": project,
            "service": service,
            "workdir": workdir,
        })

    return rows


def _docker_status(state: str) -> str:
    """A container state as one of our three: `restarting` is a crash loop
    (odoo exits, docker restarts it) rather than a healthy start, so it
    reads as a failure the way systemd's own `failed` does."""
    if state == "running":
        return "running"
    if state in ("restarting", "dead"):
        return "failed"
    return "stopped"


def docker_instances(host: Host = LOCAL) -> list[Instance]:
    """One instance per docker compose project running odoo.

    A project, not a container: `invoke`/`docker compose` act on the
    project, the odoo container and its postgres are two halves of the same
    instance, and the project name is what a developer already calls it.
    A project running several odoo services (rare) contributes one row per
    service, named `<project>/<service>` so they stay apart.

    Probing runs *inside* the odoo container (`Host.in_container`) rather
    than against the host's own pid namespace and filesystem. Both would
    half-work locally on Linux — container processes do show up in the host's
    `ps` — but the container's pids are what its logs quote and what a
    signal has to name, its filesystem is where the config lives, and none
    of it is visible at all from a Docker Desktop VM. One rule, one code
    path.
    """
    containers = _docker_ps(host)
    if not containers:
        return []

    by_project: dict[str, list[_ContainerRow]] = {}
    for row in containers:
        by_project.setdefault(row["project"], []).append(row)

    instances: list[Instance] = []
    for project, rows in sorted(by_project.items()):
        odoo_rows = [row for row in rows if _is_odoo_process(row["command"])]
        db_row = next(
            (row for row in rows if row["service"] in _DB_SERVICES or _DB_IMAGE_MARKER in row["image"]),
            None,
        )

        for odoo_row in odoo_rows:
            status = _docker_status(odoo_row["state"])
            uptime = "-"
            if status == "running":
                container = host.in_container(odoo_row["name"])
                if (pid := _odoo_master_in(container)) is not None and (
                    secs := _proc_uptime(pid, container)
                ) is not None:
                    uptime = format_duration(secs)

            inst: Instance = {
                "name": project if len(odoo_rows) == 1 else f"{project}/{odoo_row['service']}",
                "status": status,
                "uptime": uptime,
                "manager": "docker",
                "container": odoo_row["name"],
                "command": odoo_row["command"],
                "workdir": odoo_row["workdir"],
            }
            if db_row is not None:
                inst["db_container"] = db_row["name"]
            instances.append(inst)

    return instances


def _odoo_master_in(container: Host) -> str | None:
    """The odoo master's pid *inside* `container` — normally 1, but doodba's
    entrypoint is pid 1 in some images and odoo its child, so it's found the
    same argv way as everywhere else rather than assumed."""
    roots = _odoo_roots(_ps_snapshot(container)[0])
    return roots[0] if roots else None


def _copy_out_of_container(container: str, path: str | Path, box: Host = LOCAL) -> str | None:
    """Copy `path` out of `container` to a temporary file on `box`, and
    return that path (the caller removes it). None if it isn't there.

    `docker cp` rather than `docker exec cat`, because it works on a
    **stopped** container too -- and a config is exactly what someone wants
    to read when an instance won't start. It's also the only way to hand a
    container's file to a tool that lives on the box and takes a path
    (odoo-config, see render_config).
    """
    tmp = box.run(["mktemp"]).stdout.strip()
    if not tmp:
        return None

    if box.run(["docker", "cp", f"{container}:{path}", tmp]).returncode != 0:
        box.run(["rm", "-f", tmp])
        return None

    return tmp


def _read_out_of_container(container: str, path: str | Path, box: Host = LOCAL) -> str | None:
    """The contents of `path` inside `container`, or None if it isn't there
    -- running or stopped (see `_copy_out_of_container`)."""
    tmp = _copy_out_of_container(container, path, box)
    if tmp is None:
        return None

    try:
        return box.read_text(tmp)
    finally:
        box.run(["rm", "-f", tmp])


def container_host(inst: Instance, host: Host = LOCAL) -> Host:
    """`host` narrowed to the instance's own container, or unchanged for
    every other manager — so a caller can probe without branching."""
    return host.in_container(inst.get("container"))


def db_container_host(inst: Instance, host: Host = LOCAL) -> Host:
    """`host` narrowed to the instance's *postgres* container. Only the
    Top tab's backend list needs this: everything else reaches postgres
    over TCP (see `pg_target_of`), not by running commands next to it."""
    return host.in_container(inst.get("db_container"))


# doodba ships a tasks.py with start/stop/restart, and a developer running
# that project drives it with `invoke` -- so that's what we drive too, and
# a project keeps whatever its own tasks do around those calls (`start`
# waits for the services, `restart` touches only the odoo containers and
# skips the 10s stop timeout). Plain compose is the fallback for a project
# without tasks.py, or a box without invoke (a server usually has neither).
#
# `stop` is deliberately NOT in here: doodba's is `docker compose down
# --remove-orphans`, which deletes the containers rather than stopping
# them. The row would then vanish from `docker ps -a` entirely instead of
# reading `stopped`, leaving no instance to press `s` on to start it again
# -- so stopping goes through compose, which leaves the containers exited
# and listed. (Data is safe either way: doodba only removes volumes with
# an explicit --purge.)
_INVOKE_ACTIONS = ("start", "restart")


def _docker_action(action: str, host: Host = LOCAL, workdir: str | None = None) -> str:
    """start/stop/restart a compose project, via `invoke` when the project
    has tasks for it, else `docker compose`."""
    if not workdir:
        return "no compose project directory on this row — nothing to act on"

    attempts: list[list[str]] = []
    if action in _INVOKE_ACTIONS and host.is_file(f"{workdir}/tasks.py"):
        # -r: run the project's tasks.py from wherever we happen to be,
        # since a probe has no cwd on the target box (and none at all
        # remotely).
        attempts.append(["invoke", "-r", workdir, action])

    # --project-directory rather than a `cd`: the argv goes through ssh's
    # own shell quoting (see Host._argv), where a chained `cd x && ...`
    # would be one opaque string instead of a command with arguments.
    attempts.append(["docker", "compose", "--project-directory", workdir, action])

    error = ""
    for cmd in attempts:
        try:
            out = host.run(cmd)
        except FileNotFoundError:
            error = f"{cmd[0]} not found on PATH"
            continue

        if out.returncode == 0:
            return ""
        error = out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"

    return error


def instance_action(
    unit: str, action: str, manager: str = "systemd", host: Host = LOCAL, workdir: str | None = None
) -> str:
    """start/stop/restart an instance via its process manager.

    Odoo instances run under systemd --user, supervisor, odoo.sh or docker
    compose; the caller passes the `manager` recorded at discovery time so
    the right controller is used. Returns "" on success, else the
    controller's error output (so the UI can show why nothing happened
    instead of failing silently).

    `workdir` is docker's alone: compose acts on a project directory, not on
    a name the way a unit does.
    """
    if manager == "docker":
        return _docker_action(action, host, workdir)

    if manager == "local":
        return "no process manager — a directly-run instance can't be started or stopped from here"

    if manager == "odoosh":
        # odoo.sh has no separate start/stop — sleep/wake is the platform's
        # call, not ours; only a restart of the http workers is exposed.
        if action != "restart":
            return "start/stop not supported — odoo.sh handles sleep/wake on its own"

        # odoosh-restart takes one service at a time, unlike `supervisorctl
        # restart` which restarts everything for the instance in one call —
        # so restart both services it's equivalent to.
        for service in ("http", "cron"):
            try:
                out = host.run(["odoosh-restart", service])
            except FileNotFoundError:
                return "odoosh-restart not found on PATH"

            if out.returncode != 0:
                return out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"
        return ""

    # synchronous; --user odoo units activate fast. Move to a worker
    # if a unit's start/restart ever blocks the UI.
    cmd = ["supervisorctl", action, unit] if manager == "supervisor" else ["systemctl", "--user", action, unit]

    try:
        out = host.run(cmd)
    except FileNotFoundError:
        return f"{cmd[0]} not found on PATH"

    if out.returncode == 0:
        return ""

    return out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"


# db ownership: a database belongs to an instance when its owner role matches
# the instance. ODOO_ACTIVITY_DB_ROLE forces a single role (locally every db is
# owned by `openerp`); unset, the role is the instance name.
DB_ROLE = os.environ.get("ODOO_ACTIVITY_DB_ROLE", "")


@dataclass(frozen=True)
class PgTarget:
    """Where an instance's postgres is, for every psql/odoo-db call below.

    A bare `port` is all the other managers ever need: their postgres runs
    on the same box as the odoo process, reachable over the default socket
    or localhost. A container's does not — it sits on the compose network
    with its own address, role and password — so those three ride along
    here rather than being re-derived at each call site.

    Values go out as an `env K=V` prefix rather than subprocess's `env=`
    kwarg, so one argv works local or over ssh (`start_odoo_db` already
    did this for PGPORT alone). The password is then visible in that box's
    own `ps` while the call runs: it was read out of an odoo.conf sitting
    on that same box, readable by the same user, and the alternative
    (PGPASSFILE) means writing a secret to disk there.
    """

    port: str | None = None
    host: str | None = None
    user: str | None = None
    password: str | None = None

    @classmethod
    def of(cls, port: str | PgTarget | None) -> PgTarget:
        """Accepts what callers already pass — a port string, None, or a
        target — so adding docker didn't have to touch every signature's
        callers (mcp_server hands over a port it got as a tool argument)."""
        return port if isinstance(port, PgTarget) else cls(port=port)

    @property
    def env_prefix(self) -> list[str]:
        pairs = [
            f"{key}={value}"
            for key, value in (
                ("PGHOST", self.host),
                ("PGPORT", self.port),
                ("PGUSER", self.user),
                ("PGPASSWORD", self.password),
            )
            if value
        ]
        return ["env", *pairs] if pairs else []

    def psql(self, *args: str) -> list[str]:
        """`psql` with this target's connection settings applied.

        `-w`: without it psql prompts for a password it can never receive
        (probes run with stdin closed, see host._NO_STDIN) and the call
        hangs until it gives up, instead of failing with the reason.
        """
        return [*self.env_prefix, "psql", "-w", *args]


def _container_ip(container: str | None, host: Host = LOCAL) -> str | None:
    """A container's address on its compose network.

    Postgres in a compose project is normally not published to the host at
    all (doodba's devel.yaml publishes pgweb and mailhog, never the db), so
    its container address is how a client outside the network reaches it.
    Works because the container shares the box's kernel and its bridge is
    routable there — which also means it is only reachable from that box,
    hence `host` runs `docker inspect` on the same target the psql call
    will run on.
    """
    if container is None:
        return None

    out = host.run([
        "docker",
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        container,
    ]).stdout

    # Parsed, not just "first non-empty word": a stopped container has no
    # address and docker fills the field with the literal text `invalid IP`
    # (seen on docker 28), which would otherwise be handed to libpq as a
    # hostname -- and a hostname that fails to resolve looks like a network
    # problem rather than "the container isn't running".
    for token in out.split():
        with contextlib.suppress(ValueError):
            return str(ipaddress.ip_address(token))

    return None


# The official odoo image leaves the db settings commented out in the
# odoo.conf it ships and takes them from the environment instead
# (`USER`/`PASSWORD`, or postgres' own `POSTGRES_USER`/`POSTGRES_PASSWORD`),
# which its entrypoint turns into CLI flags at startup — so they exist in no
# file anywhere. doodba renders them into the config, hence this is only
# read when the config didn't answer.
def _container_env(container: str | None, host: Host = LOCAL) -> dict[str, str]:
    """A container's environment, from its config rather than its process —
    so it answers on a stopped container too, which is exactly when the
    database tabs still have to say something truthful."""
    if container is None:
        return {}

    out = host.run(["docker", "inspect", "-f", "{{json .Config.Env}}", container]).stdout
    try:
        entries = json.loads(out.strip() or "[]") or []
    except (json.JSONDecodeError, ValueError):
        return {}

    env: dict[str, str] = {}
    for entry in entries:
        key, _, value = str(entry).partition("=")
        env[key] = value

    return env


def pg_target_of(inst: Instance, host: Host = LOCAL, parser: configparser.RawConfigParser | None = None) -> PgTarget:
    """The instance's postgres, as the db-tab probes need it.

    Every manager but docker resolves to a port on the local cluster (see
    `db_port_of`). Docker's postgres is a container: the address comes from
    the compose network, the role and password from the odoo config that
    the odoo container is itself connecting with (falling back to its
    environment, which is where the official image keeps them) — so the TUI
    reaches the database exactly the way the instance does.
    """
    if inst["manager"] != "docker":
        return PgTarget(port=db_port_of(inst, host))

    if parser is None:  # `databases_of` already has one; reading it again is a copy out of the container
        _workdir, parser = instance_config(inst, host)

    db_container = inst.get("db_container")
    # A stopped project has no address to connect to -- and leaving PGHOST
    # unset is not "no database", it is *this box's* cluster, so a stale db
    # row would quietly report the host's databases as the container's
    # (exactly the mix-up the old ODOO_ACTIVITY_DOCKER hatch produced).
    # `.invalid` is reserved by RFC 2606 and never resolves, so the attempt
    # fails on the spot, naming the container that isn't up.
    address = _container_ip(db_container, host) or f"{db_container or 'db'}.invalid"

    port = _opt(parser, "db_port")
    user = _opt(parser, "db_user")
    password = _opt(parser, "db_password")
    if not (user and password):  # an official-image config names neither (see _container_env)
        env = _container_env(inst.get("container"), host)
        port = port or env.get("PORT")
        user = user or env.get("USER")
        password = password or env.get("PASSWORD")

    if not (user and password):
        # Neither named on the odoo side: it is falling back to the same
        # defaults its postgres was created with, so the db container's own
        # POSTGRES_* is what it connects as (verified against a compose file
        # that sets the credentials on the db service alone -- the odoo
        # image's entrypoint has always read them from there too, via the
        # legacy DB_ENV_POSTGRES_* link variables).
        env = _container_env(inst.get("db_container"), host)
        user = user or env.get("POSTGRES_USER")
        password = password or env.get("POSTGRES_PASSWORD")

    return PgTarget(host=address, port=port, user=user, password=password)


# `postgres` is the cluster's own maintenance database, never an instance's.
# It only needs excluding since docker arrived: postgres-autoconf makes
# POSTGRES_USER (odoo) the bootstrap superuser, so that role owns `postgres`
# as well as the real databases, where on a host cluster `postgres` belongs
# to the `postgres` role and an instance's own role never matches it.
_DB_BY_ROLE_SQL = (
    "SELECT d.datname FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
    "WHERE r.rolname = :'role' AND NOT d.datistemplate AND d.datname <> 'postgres' ORDER BY 1"
)


def _systemd_workdir(unit: str, host: Host = LOCAL) -> Path:
    """WorkingDirectory of a systemd --user unit (cwd if unset — ours
    locally; a neutral `/` remotely, where our own cwd means nothing)."""
    show = host.run(["systemctl", "--user", "show", unit, "-p", "WorkingDirectory"]).stdout
    if m := re.search(r"WorkingDirectory=(\S+)", show):
        return Path(m.group(1))
    return Path.cwd() if host.is_local else Path("/")


def instance_workdir(inst: Instance, host: Host = LOCAL) -> Path:
    """The instance's working directory (supervisor `directory=`, a
    directly-run instance's own cwd, the systemd unit's WorkingDirectory, or
    $HOME on odoo.sh)."""
    if inst["manager"] in ("supervisor", "local"):
        return Path(inst.get("directory") or ".")

    if inst["manager"] == "docker":
        # The container's own working directory, not the compose project
        # directory on the host: this is the base every *path* here is
        # resolved against (config, logfile, data_dir) and those all live
        # inside. The project directory is `workdir` on the row, used only
        # to run invoke/docker compose (see instance_action).
        #
        # Read off the image rather than /proc/<pid>/cwd: one call instead
        # of a ps plus a readlink, and it answers for a stopped container
        # too, which has no process to ask.
        out = host.on_box.run(["docker", "inspect", "-f", "{{.Config.WorkingDir}}", inst["container"]]).stdout
        return Path(out.strip() or "/")

    if inst["manager"] == "odoosh":
        if host.is_local:
            return Path.home()
        return Path(host.run(["sh", "-c", "echo $HOME"]).stdout.strip() or "/root")

    return _systemd_workdir(inst["name"], host)


# Odoo's CLI mirrors its config keys once `--` is stripped and dashes become
# underscores (`--http-port` -> `http_port`); these are the ones that don't,
# plus the short forms.
_CLI_KEYS = {
    "c": "config",
    "d": "db_name",
    "database": "db_name",
    "r": "db_user",
    "w": "db_password",
    "p": "http_port",
    "D": "data_dir",
    "load": "server_wide_modules",
}


def _cli_options(cmd: str) -> dict[str, str]:
    """Odoo options parsed out of a process's argv, keyed like the config
    file's `[options]`.

    A directly-run instance may have no config file at all — the same sparse
    shape as odoo.sh — so argv is the only place settings like `logfile` or
    `db_port` exist; and where both exist, Odoo's own precedence puts the
    command line on top. Both `--key=value` and `--key value` spellings are in
    live use. Valueless flags (`-s`, `--dev`) are skipped: there's no
    `[options]` value to carry.
    """
    tokens = cmd.split()
    options: dict[str, str] = {}
    i = 0

    while i < len(tokens):
        arg = tokens[i]
        if not arg.startswith("-") or arg == "-":
            i += 1
            continue

        flag, sep, value = arg.partition("=")
        if not sep:
            following = tokens[i + 1] if i + 1 < len(tokens) else ""
            if not following or following.startswith("-"):
                i += 1
                continue
            value = following
            i += 1

        key = flag.lstrip("-")
        options[_CLI_KEYS.get(key, key.replace("-", "_"))] = value
        i += 1

    return options


# Where an odoo container image renders its config, most specific first:
# doodba's generated one, then the official image's packaged default.
_DOCKER_CONFIG_PATHS = ("/opt/odoo/auto/odoo.conf", "/etc/odoo/odoo.conf")


def _config_names(instance_name: str) -> list[str]:
    """Config filenames to try, in order.

    A multi-node instance's name is suffixed `-NN` (e.g. `foo-01`), and its
    config is `odooNN.conf` rather than `odoo.conf` — `server.conf` is never
    node-numbered, so it's only ever tried plain.
    """
    if m := re.search(r"-(\d+)$", instance_name):
        return [f"odoo{m.group(1)}.conf", "odoo.conf", "server.conf"]
    return ["odoo.conf", "server.conf"]


def _container_config(host: Host) -> tuple[Path | None, str | None]:
    """(path, contents) of the odoo config inside `host`'s container, or
    (None, None).

    An image puts the config where it likes, so there is no
    `<workdir>/config/` convention to walk: doodba renders one at
    /opt/odoo/auto/odoo.conf, the official image ships /etc/odoo/odoo.conf,
    and anything else names it on the command line (which `_config_file`
    handles before it gets here).

    Read with `docker cp`, not `docker exec cat`: exec needs the container
    running, and a config is exactly what you want to read when an instance
    won't start (verified: with the container stopped, an exec probe
    reported no config at all).

    Path *and* contents in one go, deliberately: locating the file and
    reading it are the same copy, and doing them separately meant four
    copies out of the container per `databases_of` -- 12 round trips, each
    one an ssh hop on a remote box.
    """
    if host.container is None:
        return None, None

    for candidate in _DOCKER_CONFIG_PATHS:
        text = _read_out_of_container(host.container, candidate, host.on_box)
        if text is not None:
            return Path(candidate), text

    return None, None


def _config_file(inst: Instance, host: Host = LOCAL) -> Path | None:
    """The config file named on a directly-run instance's own command line,
    the fixed `~/.config/odoo/odoo.conf` odoo.sh always writes, or the first
    `<workdir>/config/` file matching `_config_names`."""
    host = container_host(inst, host)

    if path := inst.get("config"):
        return Path(path) if host.is_file(path) else None

    if inst["manager"] == "docker":
        return _container_config(host)[0]

    if inst["manager"] == "odoosh":
        path = instance_workdir(inst, host) / ".config" / "odoo" / "odoo.conf"
        return path if host.is_file(path) else None

    workdir = instance_workdir(inst, host)
    for name in _config_names(inst["name"]):
        path = workdir / "config" / name
        if host.is_file(path):
            return path

    return None


def configfile_of(inst: Instance, host: Host = LOCAL) -> Path | None:
    """The instance's resolved config file path, for tools (e.g. the
    `odoo-config` CLI) that operate on the file directly rather than its
    parsed values."""
    return _config_file(inst, host)


def instance_config(inst: Instance, host: Host = LOCAL) -> tuple[Path, configparser.RawConfigParser | None]:
    """(workdir, parsed odoo config) — the single source of db + log settings.

    The config is the first of `<workdir>/config/` matching `_config_names`;
    returns (workdir, None) when none exists.

    A directly-run instance layers its own argv on top, so its settings read
    back through this one parser whether they came from a file, the command
    line, or — with no config file at all — only the latter.
    """
    workdir = instance_workdir(inst, host)
    host = container_host(inst, host)  # the file lives in the container, if there is one

    if host.container is not None:
        path, text = _container_config(host)  # one copy out, path and contents together
    else:
        path = _config_file(inst, host)
        text = host.read_text(path) if path is not None else None

    if path is None and inst["manager"] not in ("local", "docker"):
        return workdir, None

    parser = configparser.RawConfigParser()  # odoo configs may contain `%`
    if text is not None:
        parser.read_string(text)

    if inst["manager"] in ("local", "docker"):
        # compose's `command:` is argv the same way a shell-run instance's
        # is, and doodba puts real settings there (--workers, --dev), so it
        # layers over the file for both.
        if not parser.has_section("options"):
            parser.add_section("options")
        for key, value in _cli_options(inst.get("command", "")).items():
            parser.set("options", key, value)

    return workdir, parser


def _opt(parser: configparser.RawConfigParser | None, key: str) -> str | None:
    """An [options] value, or None for missing / the odoo 'False'."""
    if parser is None:
        return None
    value = parser.get("options", key, fallback="").strip()
    return value if value and value.lower() != "false" else None


def logfile_of(inst: Instance, host: Host = LOCAL) -> Path | None:
    """The instance's odoo logfile, from the `logfile` key of its config, or
    odoo.sh's fixed `~/logs/odoo.log` (its config is sparse — no `logfile`
    key at all)."""
    if inst["manager"] == "odoosh":
        path = instance_workdir(inst, host) / "logs" / "odoo.log"
        return path if host.is_file(path) else None

    workdir, parser = instance_config(inst, host)
    logfile = _opt(parser, "logfile")
    if logfile is None:
        return _redirected_stdout(inst, host) if inst["manager"] == "local" else None

    path = Path(logfile)
    return path if path.is_absolute() else workdir / path


def _redirected_stdout(inst: Instance, host: Host = LOCAL) -> Path | None:
    """A directly-run instance's stdout when it was redirected to a file
    (`odoo-bin > server.log`) — the closest thing to a `logfile` for a runner
    that never set one.

    None when stdout is a terminal, pipe or socket, which is the normal case
    for something started by hand: there's nothing to tail, so the Log and
    Stacks tabs stay empty for that instance. Not a failure to report — a
    SIGQUIT dump goes to that terminal, somewhere we can't read.
    """
    target = _proc_link(inst.get("pid", ""), "fd/1", host)
    if target is None or not target.startswith("/") or target.startswith("/dev/"):
        return None

    return Path(target)


def db_port_of(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's postgres port from its odoo config, or None for the
    cluster default (instances may run on different clusters)."""
    _, parser = instance_config(inst, host)
    return _opt(parser, "db_port")


def data_dir_of(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's `data_dir`, from its odoo config, or odoo.sh's fixed
    `~/data` (its config has no `data_dir` key -- same reasoning as
    `logfile_of`'s odoosh case)."""
    if inst["manager"] == "odoosh":
        return str(instance_workdir(inst, host) / "data")

    _, parser = instance_config(inst, host)
    return _opt(parser, "data_dir")


def session_dir_of(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's filesystem session store dir (`<data_dir>/sessions`,
    matching Odoo's own `Config.session_dir`), or None if `data_dir` is
    unset."""
    data_dir = data_dir_of(inst, host)
    return f"{data_dir}/sessions" if data_dir else None


def session_count(session_dir: str, host: Host = LOCAL) -> int:
    """Number of stored sessions -- one file per session, sharded as
    `<session_dir>/<2-char-prefix>/<sid>` (Odoo's own
    `FilesystemSessionStore.get_session_filename`; its `vacuum()` scans the
    same `*/*` glob). The 2-char shard dirs themselves are a fixed ~4096-slot
    bucket scheme, not one session each, so they aren't what gets counted.

    A real directory listing (one `ls` round trip, remotely) -- the caller
    should keep this off any timer/auto-refresh path and gate it behind an
    explicit, confirmed action."""
    return len(host.glob(f"{session_dir}/*/*"))


def databases_of(inst: Instance, host: Host = LOCAL) -> tuple[list[str], str | None]:
    """(databases, db_port) for the instance — its authoritative members and
    the postgres port they live on (instances may run on different clusters).

    The odoo config gives both the role (db_user — locally `openerp`, in prod
    the instance's own role) and the db_port, so we query the right role on the
    right postgres cluster.

    odoo.sh is a single env-provided db (`PGDATABASE`), not role-queried —
    there's no `databases_by_role` dance since there's exactly one db.

    Reads `db_port` off the same parser as `db_user` rather than calling
    `db_port_of` (which would re-fetch and re-parse the config from
    scratch) — cheap to duplicate locally, but each fetch is its own ssh
    round trip remotely.
    """
    if inst["manager"] == "odoosh":
        return [inst["db"]], None

    _, parser = instance_config(inst, host)
    port = _opt(parser, "db_port")

    # `-d`/`db_name` pins it to one db; unpinned is genuinely multi-db
    if inst["manager"] == "local" and (pinned := _opt(parser, "db_name")):
        return [name.strip() for name in pinned.split(",") if name.strip()], port

    if inst["manager"] == "docker":
        # ODOO_ACTIVITY_DB_ROLE is about *this box's* cluster convention
        # (locally every db is owned by `openerp`); a container's cluster is
        # its own, and the role odoo connects as is right there in its
        # config -- so the env override deliberately doesn't apply here.
        target = pg_target_of(inst, host, parser)
        if target.host is None or target.host.endswith(".invalid"):
            return [], None  # postgres isn't up; nothing to list, and nothing to mistake for it

        return databases_by_role(target.user or "odoo", target, host), target.port

    role = DB_ROLE or _opt(parser, "db_user") or inst["name"].removesuffix(".service")
    return databases_by_role(role, port, host), port


def databases_by_role(role: str, port: str | PgTarget | None = None, host: Host = LOCAL) -> list[str]:
    """Non-template databases owned by `role`, via psql on `port`. Empty if
    postgres is unreachable (so the UI degrades instead of crashing)."""
    # SQL comes in on stdin (-f -) so psql expands :'role' and quotes it safely;
    # -c does no variable interpolation.
    cmd = PgTarget.of(port).psql("-d", "postgres", "-v", f"role={role}", "-tA", "-f", "-")
    out = host.run(cmd, input_text=_DB_BY_ROLE_SQL).stdout

    return [line.strip() for line in out.splitlines() if line.strip()]


# The eight surfaces beyond `base` that a neutralized database must not
# still have live, each condition copied from that module's own
# `data/neutralize.sql` (Odoo 19; the statements have been stable since
# 16). `(table, what neutralize.sql leaves behind, why it matters)`:
#
# Only tables whose owning module also owns the column in the condition, so
# "the table is here" implies "the column is here". That deliberately
# leaves out the checks that hang off `res_company`/`res_users`
# (sms_twilio, microsoft_calendar, the l10n_* EDI credentials): those
# tables exist on every database while their columns come and go with the
# module, which no static SQL can guard against.
# Hosts that accept mail and never relay it: Odoo's own neutralization
# stub, plus the catchers a dev stack normally runs (doodba ships one).
# Counting a catcher as a live relay would paint every neutralized dev
# database yellow forever -- and `panes/mail.py` already treats them as
# safe, via odoo-db's `is_test_catcher`, so counting them here would have
# the two halves of the app contradicting each other.
_DEAD_END_HOSTS = "'invalid', 'mailhog', 'mailpit', 'maildev'"

_STUB_IDS = f"SELECT id FROM ir_mail_server WHERE smtp_host IN ({_DEAD_END_HOSTS})"  # noqa: S608 -- _DEAD_END_HOSTS is a literal above; nothing external reaches it

_EXTRA_SURFACES = (
    # payment/data/neutralize.sql -- a provider still able to move real money
    ("payment_provider", "state NOT IN ('test', 'disabled')"),
    # the same table before Odoo 16 renamed it. Both are listed because a
    # missing table and a misspelled one look identical to the guard below:
    # dropping the old name would silently retire the highest-value check on
    # every 14/15 database instead of failing loudly.
    ("payment_acquirer", "state NOT IN ('test', 'disabled')"),
    # iap/data/neutralize.sql -- credits that bill the customer (SMS, OCR, snailmail)
    ("iap_account", "account_token NOT LIKE '%+disabled'"),
    # mail/data/neutralize.sql -- still fetching and processing real incoming mail
    ("fetchmail_server", "active"),
    # mail/data/neutralize.sql -- a template pinned to a named relay. The
    # stub is excluded: a template pointing at Odoo's own dead-end relay is
    # exactly as harmless as one pointing nowhere.
    ("mail_template", f"mail_server_id IS NOT NULL AND mail_server_id NOT IN ({_STUB_IDS})"),
    # whatsapp/data/neutralize.sql -- real API credentials, outbound messages
    ("whatsapp_account", "token <> 'dummy_token'"),
    # voip/data/neutralize.sql -- a real telephony provider, not the demo one
    ("voip_provider", "mode <> 'demo'"),
    # account_online_synchronization/data/neutralize.sql -- live bank feed
    ("account_online_link", "client_id <> 'duplicate'"),
    # certificate/data/neutralize.sql -- real signing certificates
    ("certificate_certificate", "pkcs12_password <> 'dummy'"),
)

# Each surface as a row of (table, condition), for the guarded probe below.
_EXTRA_VALUES = ",\n        ".join(
    f"('{table}', '{condition.replace(chr(39), chr(39) * 2)}')" for table, condition in _EXTRA_SURFACES
)

# Neutralization, as evidence rather than the one flag: the flag is what the
# database *claims* (`database.is_neutralized`, written by
# base/data/neutralize.sql), everything else is what it can still *do*. A
# flag inserted by hand -- or a cron switched back on afterwards -- makes a
# live database read as safe, which is the one mistake this whole status
# exists to prevent.
#
# `version` is base's own `latest_version`, because the stub relay only
# exists from Odoo 16 (`odoo/cli/neutralize.py` and the `neutralize.sql`
# files arrive together there). On 14/15 the flag is set by whatever copied
# the database -- odoo.sh's platform, an in-house script -- with no stub to
# find, and `base/models/ir_cron.py` already reads the flag back then.
# Demanding a stub there would paint every correctly neutralized old
# staging yellow forever.
#
# The `base` signals are queried directly: those tables exist on every
# version 14->19. The module surfaces cannot be, since naming a table that
# isn't there fails the whole statement at parse time and would lose the
# answer entirely instead of narrowing it. They go through `query_to_xml`,
# which takes its SQL as a *string* -- so it is only ever parsed when
# `to_regclass` has already confirmed the table exists, and the CASE keeps
# it unevaluated otherwise. `extras` lists only what is still live (a clean
# database answers `{}`), while `checked` lists every surface that was
# actually evaluated -- without it, a misspelled table name and a module
# that isn't installed are indistinguishable, and a typo would retire a
# check permanently with nothing to show for it.
_NEUTRALIZATION_SQL = f"""SELECT json_build_object(
  'flag', (SELECT value FROM ir_config_parameter WHERE key = 'database.is_neutralized'),
  'version', (SELECT latest_version FROM ir_module_module WHERE name = 'base'),
  'stub', (SELECT count(*) FROM ir_mail_server WHERE smtp_host = 'invalid' AND name LIKE 'neutralization%'),
  'live_relays', (SELECT count(*) FROM ir_mail_server
                   WHERE active AND coalesce(smtp_host, '') NOT IN ({_DEAD_END_HOSTS})),
  'live_crons', (SELECT count(*) FROM ir_cron c WHERE c.active AND c.id NOT IN (
      SELECT res_id FROM ir_model_data WHERE model = 'ir.cron' AND name = 'autovacuum_job' AND module = 'base')),
  'checked', (SELECT coalesce(json_agg(tbl ORDER BY tbl), '[]'::json)
                FROM (VALUES
                  {_EXTRA_VALUES}
                ) AS s(tbl, cond) WHERE to_regclass(s.tbl) IS NOT NULL),
  'extras', (
    SELECT coalesce(json_object_agg(name, n), '{{}}'::json) FROM (
      SELECT s.tbl AS name, (xpath('/row/c/text()', CASE WHEN to_regclass(s.tbl) IS NOT NULL THEN
               query_to_xml('SELECT count(*) AS c FROM ' || s.tbl || ' WHERE ' || s.cond, false, true, '')
             END))[1]::text::bigint AS n
      FROM (VALUES
        {_EXTRA_VALUES}
      ) AS s(tbl, cond)
    ) live WHERE n > 0
  )
)"""  # noqa: S608 -- interpolates _EXTRA_SURFACES, a literal in this file; nothing external reaches it

_TRUE = {"true", "t", "1", "yes", "y", "on"}

# The three answers a database gets. PARTIAL is the one that pays for the
# extra signals: it means the two disagree -- treat the db as live until
# someone looks.
NEUTRALIZED = "neutralized"
PARTIAL = "partial"
NOT_NEUTRALIZED = "not_neutralized"


def _major_version(version: str | None) -> int:
    """Base's major version off `latest_version` (`16.0.1.3` -> 16), or 0
    when the database didn't say."""
    head = (version or "").split(".")[0]
    return int(head) if head.isdigit() else 0


def _neutralization_state(row: Mapping) -> str:
    """Classify one database's signals.

    The flag alone is never enough in either direction: claimed-and-clean is
    the only way to NEUTRALIZED, and evidence of the script having run (the
    stub relay) keeps a database off NOT_NEUTRALIZED even with the flag
    missing -- a neutralization that died halfway is not a production
    database, but it is not a safe one either.

    The stub is only *required* from Odoo 16, where it starts existing (see
    `_NEUTRALIZATION_SQL`). An unreadable version is treated as modern:
    that way the uncertainty costs a yellow, never a false green.
    """
    claimed = str(row.get("flag") or "").strip().lower() in _TRUE
    live = row.get("live_relays") or row.get("live_crons") or row.get("extras")

    stub = bool(row.get("stub"))
    stub_expected = _major_version(row.get("version")) >= 16 or not row.get("version")

    if claimed and not live and (stub or not stub_expected):
        return NEUTRALIZED
    if claimed or stub:
        return PARTIAL
    return NOT_NEUTRALIZED


def neutralization_of(dbs: list[str], port: str | PgTarget | None = None, host: Host = LOCAL) -> dict[str, dict]:
    """{db: report} -- one report per database that answered.

    The report is the raw signals plus the `state` read off them
    (NEUTRALIZED / PARTIAL / NOT_NEUTRALIZED). Callers that only paint a tag
    take `state`; the rest is what makes a PARTIAL actionable -- `extras`
    names the surface that is still live, `checked` names the surfaces that
    were evaluated at all -- and it is already in hand, so throwing it away
    would only mean fetching it again later.

    Neutralization is per-database state (rows in each db's own tables), so
    it takes a connection per db -- but they go out as one shell loop, one
    round trip for the whole list, since this runs on every instance
    highlight and remotely each round trip is an ssh hop.

    A db missing from the result is one psql could not read (postgres down,
    no such table -- not an odoo database): unknown, which the callers show
    as no status rather than as a guess in either direction.
    """
    if not dbs:
        return {}

    psql = shlex.join(PgTarget.of(port).psql("-tAc", _NEUTRALIZATION_SQL, "-d"))
    # `&&` so a failed connection prints nothing at all and stays unknown
    loop = f'for db in {shlex.join(dbs)}; do v=$({psql} "$db" 2>/dev/null) && printf \'%s\\t%s\\n\' "$db" "$v"; done'
    out = host.run(["sh", "-c", loop]).stdout

    reports = {}
    for line in out.splitlines():
        db, _, payload = line.partition("\t")
        try:
            row = json.loads(payload)
            reports[db] = {**row, "state": _neutralization_state(row)}
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
            continue  # unparseable is unknown, same as unreachable

    return reports


_LONG_QUERIES_SQL = (
    "SELECT json_agg(t) FROM ("
    "SELECT pid, datname, query_start, age(now(), query_start) AS duration, query "
    "FROM pg_stat_activity "
    "WHERE state != 'idle' AND pid != pg_backend_pid() AND datname = :'db' "
    "ORDER BY duration DESC"
    ") t"
)


def long_queries(db: str, port: str | PgTarget | None = None, host: Host = LOCAL) -> list[dict]:
    """Non-idle queries on `db`, longest-running first, via psql on `port`.

    odoo-db has no equivalent command; this queries pg_stat_activity
    directly instead, the same way databases_by_role reads pg_database.
    """
    cmd = PgTarget.of(port).psql("-d", "postgres", "-v", f"db={db}", "-tA", "-f", "-")
    out = host.run(cmd, input_text=_LONG_QUERIES_SQL).stdout

    try:
        return json.loads(out.strip()) or []
    except (json.JSONDecodeError, ValueError):
        return []


def _psql_json(
    sql: str, db: str, params: dict[str, str], port: str | PgTarget | None, host: Host
) -> tuple[list[dict] | None, str]:
    """Run a `SELECT json_agg(...)` on `db` and decode it.

    `(rows, error)`: rows is None only when the query didn't run — the
    caller shows `error` (postgres's own message, e.g. queue_job's table
    missing on a db without the module) instead of a table. `json_agg` over
    no rows is SQL NULL, which psql prints as nothing, so an empty result is
    `([], "")` rather than a decode failure.

    Values arrive as psql variables (`-v`, expanded by `:'name'`) rather
    than interpolated into the SQL, so a job function or state out of the db
    can't reshape the statement.
    """
    # ON_ERROR_STOP or a failed statement exits 0 with the error on stderr
    # only, and a missing queue_job table would read as "no jobs"
    cmd = PgTarget.of(port).psql("-d", db, "-v", "ON_ERROR_STOP=1")
    for name, value in params.items():
        cmd += ["-v", f"{name}={value}"]

    cmd += ["-tA", "-f", "-"]
    result = host.run(cmd, input_text=sql)
    out = result.stdout.strip()

    if result.returncode != 0:
        return None, result.stderr.strip() or f"psql exited {result.returncode}"

    if not out:
        return [], ""

    try:
        return json.loads(out) or [], ""
    except (json.JSONDecodeError, ValueError):
        return None, out


# `func_string` names the records too (`res.partner(1,).action()`), so it
# groups into near-singletons — model+method is the function itself.
_JOB_FUNCTION = "coalesce(model_name || '.' || method_name, method_name, '(unknown)')"

# queue_job's dates are `timestamp without time zone` holding UTC (odoo's
# convention -- its runner writes `now() at time zone 'utc'`), while `now()`
# is a timestamptz postgres renders in the *session* timezone. Ageing one
# against the other offsets every wait/run by the server's UTC offset: hours
# of phantom "stuck" on a server not set to UTC, negative intervals west of it.
_UTC_NOW = "(now() at time zone 'utc')"

# the only interpolation is _JOB_FUNCTION, a constant defined right above;
# every value the caller passes goes through psql's own `-v` (see _psql_json)
_JOB_GROUPS_SQL = (
    "SELECT json_agg(t) FROM ("  # noqa: S608
    f"SELECT {_JOB_FUNCTION} AS function, state, count(*) AS jobs, "
    "min(date_created) AS oldest, "
    f"max(age({_UTC_NOW}, date_created)) AS waiting, "
    f"max(age({_UTC_NOW}, date_started)) AS running "
    "FROM queue_job GROUP BY 1, 2 ORDER BY 1, 2"
    ") t"
)

_JOBS_IN_GROUP_SQL = (
    "SELECT json_agg(t) FROM ("  # noqa: S608 -- see _JOB_GROUPS_SQL
    "SELECT uuid, name, state, priority, date_created, date_started, "
    f"age({_UTC_NOW}, date_created) AS waiting, age({_UTC_NOW}, date_started) AS running "
    f"FROM queue_job WHERE {_JOB_FUNCTION} = :'function' AND state = :'state' "
    "ORDER BY date_created LIMIT 500"
    ") t"
)

# exactly the states a job can be stuck in: `pending` is already queued and
# `done`/`failed`/`cancelled` are finished, so neither is ours to touch
_REQUEUABLE_STATES = ("started", "enqueued")
_REQUEUABLE_IN = ", ".join(f"'{state}'" for state in _REQUEUABLE_STATES)
# the dates come off with the state, as queue_job's own `set_pending` does:
# a job left with its `date_started` reads as running for as long as it sits
# in the queue, which is the very signal the Jobs tab exists to give
_REQUEUE_SQL = (
    # the only interpolation is _REQUEUABLE_IN, built from the constant above
    "UPDATE queue_job SET state = 'pending', "  # noqa: S608
    "date_started = NULL, date_enqueued = NULL, worker_pid = NULL "
    f"WHERE state IN ({_REQUEUABLE_IN})"
)


# What postgres says when a database never installed queue_job:
# `ERROR:  relation "queue_job" does not exist`, with psql's own
# `psql:<stdin>:1:` prefix and a `LINE 1: ...^` excerpt after it. That is by
# far the most common reason the Jobs tab has nothing to show -- most
# databases don't run the module -- and as phrased it reads like something
# broke. Every other psql error is passed through verbatim: those are rarer,
# and their exact text is the diagnosis.
_NO_QUEUE_JOB_RE = re.compile(r'relation "queue_job" does not exist')
_NO_QUEUE_JOB = "(queue_job is not installed on this database)"


def _job_error(error: str) -> str:
    return _NO_QUEUE_JOB if _NO_QUEUE_JOB_RE.search(error) else error


def job_groups(db: str, port: str | PgTarget | None = None, host: Host = LOCAL) -> tuple[list[dict] | None, str]:
    """queue_job rows on `db` grouped by function and state, with the oldest
    creation date and the longest wait/run in each group.

    odoo-db's `jobs` counts by state alone, which says a queue is backed up
    but not what's stuck in it: `waiting`/`running` are what a job sitting in
    `started` for hours shows up as.
    """
    rows, error = _psql_json(_JOB_GROUPS_SQL, db, {}, port, host)
    return rows, _job_error(error)


def jobs_in_group(
    db: str, function: str, state: str, port: str | PgTarget | None = None, host: Host = LOCAL
) -> tuple[list[dict] | None, str]:
    """The individual jobs behind one `job_groups` row, oldest first (capped
    at 500 — a backed-up queue runs to tens of thousands, and the ones that
    explain it are the oldest)."""
    rows, error = _psql_json(_JOBS_IN_GROUP_SQL, db, {"function": function, "state": state}, port, host)
    return rows, _job_error(error)


def requeue_jobs(db: str, port: str | PgTarget | None = None, host: Host = LOCAL) -> tuple[int, str]:
    """Put every `started`/`enqueued` job back to `pending`, returning
    `(rows, error)`.

    What a job runner does for its own dead jobs on startup, run on demand:
    a worker killed mid-job leaves the row `started` forever, since nothing
    else revisits it.
    """
    cmd = PgTarget.of(port).psql("-d", db, "-v", "ON_ERROR_STOP=1", "-tA", "-f", "-")
    result = host.run(cmd, input_text=_REQUEUE_SQL)

    if result.returncode != 0:
        return 0, _job_error(result.stderr.strip()) or f"psql exited {result.returncode}"

    # psql echoes the tag ("UPDATE 8") for a statement that changed rows
    tag = result.stdout.strip().rpartition(" ")[2]

    return (int(tag) if tag.isdigit() else 0), ""


# Odoo names its postgres connections after the worker that opened them
# (`odoo-<pid>`, service/db.py), which is what makes a backend traceable back
# to a process without lsof. The runner's own connection is then the one
# whose last statement is part of its loop: `LISTEN queue_job`, its named
# `select_jobs` cursor, or the bare `SELECT 1` it keeps the connection alive
# with between polls (`keep_alive` in queue_job's runner — nothing in odoo
# itself issues that).
_JOBRUNNER_SQL = (
    "SELECT json_agg(t) FROM ("
    "SELECT DISTINCT application_name AS app, client_port AS port FROM pg_stat_activity "
    # `odoo-<pid>` from 16.0 on (odoo/odoo f6c13d7); before that odoo never
    # set application_name at all, so an unnamed connection has to stay in --
    # a named one that isn't odoo's (psql, pgAdmin) is what this excludes
    "WHERE (application_name ~ '^odoo-[0-9]+$' OR application_name = '') AND ("
    # the channel it waits on, its own `select_jobs` cursor (unquoted table
    # name -- the ORM quotes its identifiers, so a worker that merely read
    # or enqueued a job doesn't collide), and its keepalive
    "query ILIKE 'listen%queue_job%' "
    "OR query ILIKE '%from queue_job where%' "
    # the keepalive names queue_job nowhere, so it is only a candidate here:
    # pgbouncer and monitoring ping the same way. jobrunner_pids drops
    # whatever doesn't turn out to be an odoo process.
    "OR btrim(query, ' ;') = 'SELECT 1'"
    ")"
    ") t"
)

# `ss -tnpH` prints one connection per line, the holder as
# `users:(("python3",pid=123,fd=7))` -- and only when we may see it (our own
# processes, or running as root), which is the same limit lsof has below.
_SS_CONN_RE = re.compile(r"\S+:(?P<local>\d+)\s+\S+:(?P<peer>\d+)")
_SS_PID_RE = re.compile(r"pid=(?P<pid>\d+)")
_DEFAULT_PG_PORT = "5432"


def _pids_by_client_port(ports: list[str], pg_port: str | PgTarget | None = None, host: Host = LOCAL) -> dict[str, str]:
    """client port -> the OS pid holding it, for each postgres connection in
    `ports`.

    One `ss` call for the lot (a remote host would otherwise pay a round trip
    per port), falling back to `lsof` per port for whatever `ss` couldn't
    answer -- neither is guaranteed to be installed, and a port nobody can
    account for is simply left out.

    Only connections *to postgres* count. A port number means nothing on its
    own: it is unique per host, and one postgres can serve several, so a
    port another host reported may well be live here on something unrelated
    (a browser socket is as likely as anything). Matching the far end keeps
    that from naming the wrong process.
    """
    if not ports:
        return {}

    wanted = set(ports)
    # Docker callers carry all libpq settings in a PgTarget. Socket
    # matching only needs its numeric server port; comparing ss's string
    # output with the PgTarget object would never match and would force the
    # less reliable lsof fallback for every pre-Odoo-16 job runner.
    pg_port = PgTarget.of(pg_port).port or _DEFAULT_PG_PORT
    found: dict[str, str] = {}

    try:
        out = host.run(["ss", "-tnpH"]).stdout
    except FileNotFoundError:
        out = ""

    for line in out.splitlines():
        conn = _SS_CONN_RE.search(line)
        pid = _SS_PID_RE.search(line)
        if conn is None or pid is None:
            continue

        if conn["local"] in wanted and conn["peer"] == pg_port:
            found[conn["local"]] = pid["pid"]

    # lsof names both ends of `-i :port`, and excludes postgres's own side by
    # process name (see odoo_pid_for_port) -- the port pins the connection
    for port in wanted - set(found):
        if (pid_ := odoo_pid_for_port(port, host)) is not None:
            found[port] = pid_

    return found


def jobrunner_pids(port: str | PgTarget | None = None, host: Host = LOCAL) -> set[str]:
    """Pids of the queue_job runner workers, from their postgres backends.

    Odoo only labels a worker in `ps` when `setproctitle` is installed —
    without it every prefork worker carries the master's argv verbatim, so
    argv can't tell a job runner from an HTTP one. Its postgres connection
    can, in two ways:

    - `application_name` is `odoo-<pid>` from Odoo 16.0 on (odoo/odoo
      f6c13d7), which names the pid outright.
    - Before that odoo left it unset, so the connection has to be traced by
      its TCP endpoint instead: postgres reports the client port, and the
      host says which process holds it. Costs a second command, and only
      works over TCP — an instance on a unix socket reports no port, and
      stays unreachable either way.

    Empty when nothing matches (queue_job absent, runner disabled, postgres
    unreachable, or nothing able to account for the port), which just leaves
    those workers under "HTTP".
    """
    rows, _error = _psql_json(_JOBRUNNER_SQL, "postgres", {}, port, host)

    pids = {row["app"].removeprefix("odoo-") for row in rows or [] if row["app"]}
    # `client_port` is -1 for a unix socket and null for a background worker
    ports = [str(row["port"]) for row in rows or [] if not row["app"] and (row["port"] or -1) > 0]
    pids |= set(_pids_by_client_port(ports, port, host).values())

    if not pids:
        return pids

    # last gate, for both routes: the bare `SELECT 1` above is the runner's
    # keepalive but also pgbouncer's and every monitor's ping, and a pid
    # traced through a port is only as good as what is holding that port.
    # Whatever isn't an odoo process was never a job runner.
    by_pid, _children = _ps_snapshot(host)

    return {pid for pid in pids if pid in by_pid and _is_odoo_process(by_pid[pid]["cmd"])}


def _parse_cpu_ticks(data: str) -> int | None:
    try:
        fields = data[data.rindex(")") + 2 :].split()  # skip 'pid (comm)'
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (ValueError, IndexError):
        return None


def proc_cpu_ticks(pid: str, host: Host = LOCAL) -> int | None:
    """utime+stime (CPU jiffies) for a pid, or None if it's gone."""
    try:
        data = host.read_text(f"/proc/{pid}/stat")
    except OSError:
        return None
    return _parse_cpu_ticks(data)


def proc_cpu_ticks_many(pids: list[str], host: Host = LOCAL) -> dict[str, int | None]:
    """Batched `proc_cpu_ticks` -- local stays one read per pid (microseconds
    each, no need to batch), but remote becomes one ssh round trip for every
    pid instead of one round trip per pid: the Top tab was sitting on
    "Loading top..." for several real seconds against a host with more
    than a couple of top, one sequential round trip at a time."""
    if host.is_local or not pids:
        return {pid: proc_cpu_ticks(pid, host) for pid in pids}

    marker = "\x1e"  # ASCII record separator -- won't appear in /proc/*/stat
    script = ";".join(f"echo {marker}{pid}; cat /proc/{pid}/stat 2>/dev/null" for pid in pids)
    out = host.run(["sh", "-c", script]).stdout

    result: dict[str, int | None] = dict.fromkeys(pids)
    for block in out.split(marker)[1:]:
        pid, _, data = block.partition("\n")
        result[pid.strip()] = _parse_cpu_ticks(data)
    return result


def instance_pid(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's master pid, straight from its process manager.

    Not matched by database name: in multi-db/config-only setups the odoo
    process's argv never carries a db name at all (only postgres's own
    backends do, since they connect to a specific db), so a db-name-in-argv
    heuristic both misses the real process and can misfire on postgres.
    """
    if inst["manager"] == "odoosh":
        return _odoosh_master_pid(host)

    if inst["manager"] == "docker":
        # compose has no MainPID to ask for, and the pid is the container's
        # own (see docker_instances) -- so it comes from a ps inside it.
        # Not cached on the row: a container that restarted keeps its name
        # and gets a fresh pid, which a cached one would miss.
        return _odoo_master_in(container_host(inst, host))

    if inst["manager"] == "local":
        # nothing to re-ask for a MainPID; `list_instances` re-runs on a timer,
        # so a restart arrives as a fresh row rather than being tracked here
        return inst.get("pid")

    if inst["manager"] == "supervisor":
        out = host.run(["supervisorctl", "pid", inst["name"]]).stdout.strip()
        return out if out.isdigit() else None

    out = host.run(["systemctl", "--user", "show", inst["name"], "-p", "MainPID"]).stdout
    m = re.search(r"MainPID=(\d+)", out)
    return m.group(1) if m and m.group(1) != "0" else None


def _ps_snapshot(host: Host) -> tuple[dict[str, ProcRow], dict[str, list[str]]]:
    """One `ps -eo` call, indexed by pid and by ppid -> children -- shared by
    every walker below that needs the descendant tree of a known root pid."""
    lines = host.run(["ps", "-eo", "pid,ppid,user,%mem,nice,args"]).stdout.splitlines()[1:]  # drop header

    by_pid: dict[str, ProcRow] = {}
    children: dict[str, list[str]] = {}
    for ln in lines:
        cols = ln.split(maxsplit=5)
        if len(cols) < 6:
            continue

        row: ProcRow = {
            "pid": cols[0],
            "ppid": cols[1],
            "user": cols[2],
            "mem": cols[3],
            "nice": cols[4],
            "cmd": cols[5],
        }
        by_pid[row["pid"]] = row
        children.setdefault(row["ppid"], []).append(row["pid"])

    return by_pid, children


def _descendants(root: str, by_pid: dict[str, ProcRow], children: dict[str, list[str]]) -> list[ProcRow]:
    """`root` plus every pid reachable from it through `children`."""
    keep: list[str] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in keep or pid not in by_pid:
            continue
        keep.append(pid)
        stack.extend(children.get(pid, []))

    return [by_pid[pid] for pid in keep]


def _proc_link(pid: str, name: str, host: Host) -> str | None:
    """Target of one of `/proc/<pid>`'s symlinks (`exe`, `cwd`, `fd/1`).
    None on any failure (permission denied, pid gone, ...) — a
    supervisor-owned process's `cwd` is unreadable to us, for one."""
    if not pid:
        return None

    if host.is_local:
        try:
            return os.readlink(f"/proc/{pid}/{name}")
        except OSError:
            return None

    target = f"/proc/{pid}/{name}"
    out = host.run(["readlink", "-f", target]).stdout.strip()
    # unreadable symlink: `-f` echoes the query back instead of failing
    return out if out and out != target else None


def _exe_of(pid: str, host: Host) -> str | None:
    """Absolute path of `pid`'s running executable -- unlike argv[0], never a
    bare `python3` left over from a `PATH` lookup at launch."""
    return _proc_link(pid, "exe", host)


def _environ_of(pid: str, host: Host) -> dict[str, str]:
    """`pid`'s environment at launch, or {} if unreadable (another user's
    process). NUL-separated `KEY=value`, same shape local or over ssh."""
    try:
        raw = host.read_text(f"/proc/{pid}/environ")
    except OSError:
        return {}

    return dict(entry.split("=", 1) for entry in raw.split("\0") if "=" in entry)


def _resolve_argv0(argv0: str, pid: str, host: Host) -> str:
    """`argv0` as a path that still resolves once pasted into another shell.

    A bare `python3`/`odoo` was found on `PATH` at launch, under
    `$VIRTUAL_ENV/bin` for a venv runner -- which beats `/proc/<pid>/exe`,
    resolved through `bin/python3`'s symlink to the base interpreter and so
    blind to the venv's site-packages. `exe` is the last resort: for a
    console script it names the interpreter, not the script.
    """
    if argv0.startswith("/"):
        return argv0

    if "/" in argv0:  # ./odoo-bin -- cwd-relative, meaningless once pasted
        cwd = _proc_link(pid, "cwd", host)
        return os.path.normpath(f"{cwd}/{argv0}") if cwd else argv0

    venv = _environ_of(pid, host).get("VIRTUAL_ENV")
    if venv and host.is_file(f"{venv}/bin/{argv0}"):
        return f"{venv}/bin/{argv0}"

    return _exe_of(pid, host) or argv0


def shell_command(inst: Instance, host: Host = LOCAL) -> str | None:
    """Return an `odoo-bin shell --no-http` command for a running instance, or None.

    Builds from the live process argv, absolutizing argv[0] to ensure execution
    outside the process context. Appends `shell --no-http` if not already present.
    """
    host = container_host(inst, host)
    procs = procs_of(inst, host)
    if not procs:
        return None

    tokens = procs[0]["cmd"].split()
    tokens[0] = _resolve_argv0(tokens[0], procs[0]["pid"], host)

    split_at = next((i for i, tok in enumerate(tokens) if tok.startswith("-")), len(tokens))
    if "shell" in tokens[:split_at]:
        return " ".join(tokens)

    return " ".join([*tokens[:split_at], "shell", "--no-http", *tokens[split_at:]])


def try_local_clipboard(text: str) -> bool:
    """Copy `text` to the clipboard of the machine this process runs on.

    Only meaningful for a local run -- over SSH this would write to the
    remote host's clipboard, invisible to the user, so it's skipped whenever
    the standard `SSH_TTY`/`SSH_CONNECTION` env vars mark this odoo-activity
    process itself as running inside an ssh session (the caller should use
    `App.copy_to_clipboard`'s OSC 52 instead, which does survive SSH). False
    on any failure (no clipboard mechanism installed, SSH session, etc.) so
    the caller can fall back.

    Independent of the `--host` target: this is about the terminal
    odoo-activity itself is drawn in, not which host `shell_command` was
    read from -- see `Host.shell_invocation` for that side.
    """
    if os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"):
        return False

    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException:
        return False
    else:
        return True


def procs_of(inst: Instance, host: Host = LOCAL) -> list[ProcRow]:
    """The instance's master process plus every descendant (prefork
    workers), read purely from ps by walking the ppid tree down from the
    manager-reported master pid."""
    host = container_host(inst, host)
    master = instance_pid(inst, host)
    if master is None:
        return []

    by_pid, children = _ps_snapshot(host)
    return _descendants(master, by_pid, children)


def instance_procs(inst: Instance, host: Host = LOCAL) -> tuple[list[ProcRow], list[ProcRow]]:
    """(odoo_top, postgres_backends) from one `ps` call, halving the
    system-wide `ps` fork+parse the Top tab used to do twice a tick.

    Postgres process titles vary by cluster configuration, so backends are
    matched by db-name membership rather than a fixed token position.
    """
    dbs = set(databases_of(inst, host)[0])

    odoo_at = container_host(inst, host)
    master = instance_pid(inst, odoo_at)
    by_pid, children = _ps_snapshot(odoo_at)
    odoo_rows = _descendants(master, by_pid, children) if master is not None else []

    # postgres lives in its own container under docker, so its backends are
    # in a different ps entirely -- one more call there, and only there
    # (db_container_host is a no-op for every other manager, which would
    # otherwise pay a second identical `ps`).
    pg_at = db_container_host(inst, host)
    pg_by_pid = by_pid if pg_at == odoo_at else _ps_snapshot(pg_at)[0]
    pg_rows = [
        row
        for row in pg_by_pid.values()
        if dbs and row["cmd"].startswith("postgres:") and dbs.intersection(row["cmd"].split())
    ]

    return odoo_rows, pg_rows


def instance_workers(inst: Instance, host: Host = LOCAL) -> tuple[str | None, list[ProcRow]]:
    """(master_pid, odoo_top) for the Processes tab's worker tree --
    same ppid-walk as instance_procs' odoo side, without paying for its
    postgres-backend matching (unused here)."""
    host = container_host(inst, host)
    master = instance_pid(inst, host)
    if master is None:
        return None, []

    by_pid, children = _ps_snapshot(host)
    return master, _descendants(master, by_pid, children)


_PG_CLIENT_PORT_RE = re.compile(r"\((\d+)\)")


def pg_client_port(cmd: str) -> str | None:
    """The client TCP port out of a postgres backend's `ps` title, or None
    over a unix socket (`[local]`, no parenthesized port at all)."""
    m = _PG_CLIENT_PORT_RE.search(cmd)
    return m.group(1) if m else None


def odoo_pid_for_port(port: str, host: Host = LOCAL) -> str | None:
    """The pid on the other end of the TCP connection whose port is `port`
    (a postgres backend's client port), via `lsof` — traces a postgres
    backend back to the Odoo worker that opened it. `lsof -i :port` matches
    the connection from either endpoint, so postgres's own accepting side
    is excluded by name; None if `lsof` is missing or nothing else matches."""
    try:
        out = host.run(["lsof", "-Pni", f":{port}"]).stdout
    except FileNotFoundError:
        return None

    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 1 and cols[0] != "postgres" and cols[1].isdigit():
            return cols[1]

    return None


def signal_process(pid: str, sig: int, host: Host = LOCAL) -> None:
    """Send `sig` to `pid`; a pid that's already gone, or owned by another
    user (e.g. a postgres backend), is not an error."""
    if host.is_local:
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.kill(int(pid), sig)
        return

    host.run(["kill", f"-{int(sig)}", pid])


# Matches Odoo's standard log header for SIGQUIT dumps to extract worker PIDs:
# "<date> <time> <pid> <level> <db> odoo.tools.misc: "
_DUMP_HEADER_RE = re.compile(r"^\S+ \S+ (?P<pid>\d+) \S+ \S+ odoo\.tools\.misc:[ \t]*$", re.MULTILINE)
# Odoo's dumpstacks() (odoo/tools/misc.py) emits this shape for every thread,
# request or not — db/uid/url/qc/qt/pt are "n/a" when the thread isn't
# mid-request. qt = query_time (SQL time so far); pt = python_time, Odoo's
# own name for wall-clock-since-request-start minus qt (time spent outside
# the DB) — not a running total SIGQUIT accumulates, just what's true at
# this one signal. db/uid/url predate qc/qt/pt, which only exist from Odoo
# 17+; that trailing group is optional so pre-17 dumps still parse, just
# without query-count/time context.
_THREAD_RE = re.compile(
    r"^# Thread: (?P<name>.*?) \(db:(?P<db>[^)]*)\) \(uid:(?P<uid>[^)]*)\) \(url:(?P<url>[^)]*)\)"
    r"(?: \(qc:(?P<qc>\S*) qt:(?P<qt>\S*) pt:(?P<pt>\S*)\))?$",
    re.MULTILINE,
)
_FRAME_RE = re.compile(r'^File: "(?P<file>[^"]*)", line (?P<line>\d+), in (?P<func>\S+)$', re.MULTILINE)

# Innermost frame functions that indicate an idle thread (event loops, waits,
# and faulthandler frames).
_IDLE_FRAME_FUNCS = {"select", "poll", "sleep", "wait", "dumpstacks", "extract_stack"}

# Vendor-specific path override for Sentry's background thread, which is
# always present but constantly idle.
_IDLE_FRAME_PATH_MARKERS = ("/sentry_sdk/",)


class _InstanceRequired(TypedDict):
    name: str
    status: str
    uptime: str
    manager: str


class Instance(_InstanceRequired, total=False):
    """`command`/`directory` come from supervisor or a directly-run
    instance's own argv/cwd, `db`/`version` from odoo.sh, `config`/`pid` only
    from a directly-run one — each manager sets its own subset."""

    command: str
    directory: str
    db: str
    version: str
    config: str
    pid: str
    # docker only: the odoo container to run probes in, the postgres one to
    # reach its cluster, and the compose project directory on the host
    # (where `invoke`/`docker compose` have to be run from)
    container: str
    db_container: str
    workdir: str


class ProcRow(TypedDict):
    pid: str
    ppid: str
    user: str
    mem: str
    nice: str
    cmd: str


class Worker(TypedDict):
    pid: str
    threads: list[Thread]


class Thread(TypedDict):
    name: str
    db: str | None
    uid: str | None
    url: str | None
    query_count: int | None
    query_time: float | None
    python_time: float | None
    frames: list[Frame]
    idle: bool


class Frame(TypedDict):
    file: str
    line: int
    func: str


def parse_stack_dump(text: str) -> list[Worker]:
    """A slice of log text containing one or more SIGQUIT dumps into
    `[{"pid": ..., "threads": [Thread, ...]}, ...]`.

    Each `Thread` carries its request context (`db`/`uid`/`url`/`query_count`/
    `query_time`/`python_time`) when Odoo attached one — see `Thread` and the
    `_THREAD_RE` comment for what `query_time`/`python_time` mean. `frames` is
    outermost-first (as printed); a thread is `idle` iff its *innermost*
    (last) frame's function is in `_IDLE_FRAME_FUNCS`.
    """
    markers = list(_DUMP_HEADER_RE.finditer(text))
    workers: list[Worker] = []

    for i, dm in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        workers.append({"pid": dm["pid"], "threads": _parse_threads(text[dm.end() : end])})

    return workers


def _parse_threads(block_text: str) -> list[Thread]:
    headers = list(_THREAD_RE.finditer(block_text))
    threads: list[Thread] = []

    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(block_text)
        frames: list[Frame] = [
            {"file": fm["file"], "line": int(fm["line"]), "func": fm["func"]}
            for fm in _FRAME_RE.finditer(block_text[m.end() : end])
        ]
        idle = bool(frames) and (
            frames[-1]["func"] in _IDLE_FRAME_FUNCS
            or any(marker in frames[-1]["file"] for marker in _IDLE_FRAME_PATH_MARKERS)
        )
        threads.append({
            "name": m["name"].strip(),
            "db": _na(m["db"]),
            "uid": _na(m["uid"]),
            "url": _na(m["url"]),
            "query_count": _opt_int(m["qc"]),
            "query_time": _opt_float(m["qt"]),
            "python_time": _opt_float(m["pt"]),
            "frames": frames,
            "idle": idle,
        })

    return threads


def _na(value: str | None) -> str | None:
    """Odoo prints "n/a" for a thread attribute that isn't set (not
    mid-request); normalize that to None. `value` is also None outright on
    pre-17 dumps, where `_THREAD_RE`'s qc/qt/pt group didn't match at all."""
    return None if value in (None, "n/a") else value


def _opt_int(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _opt_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _read_new_bytes(proc: subprocess.Popen) -> bytes:
    """Whatever's already buffered on a streaming child's stdout, without
    blocking — for polling a `tail -f` child instead of a blocking read."""
    if proc.stdout is None or not select.select([proc.stdout], [], [], 0)[0]:
        return b""
    return os.read(proc.stdout.fileno(), 65536)


def stacks_by_activity(workers: list[Worker]) -> list[Worker]:
    """`workers` reordered busy-first for display: workers by busy-thread
    count descending, threads within each busy-before-idle, and each
    thread's `frames` reversed to innermost-first -- py-spy's order, so the
    concerning frame is the one a reader (human or agent) sees first rather
    than last."""
    by_busy = sorted(workers, key=lambda w: sum(not t["idle"] for t in w["threads"]), reverse=True)
    return [
        {
            **w,
            "threads": [
                {**t, "frames": list(reversed(t["frames"]))} for t in sorted(w["threads"], key=lambda t: t["idle"])
            ],
        }
        for w in by_busy
    ]


def _read_until_dumped(reader: subprocess.Popen, expected: set[str]) -> str:
    """Read `reader` until every pid in `expected` has a dump header, or ~2s
    passes — the streaming counterpart to the local branch's poll loop in
    `dump_and_parse_stacks`. Doesn't kill `reader`; its caller owns it."""
    data = b""
    text = ""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        data += _read_new_bytes(reader)
        text = _untty(data.decode(errors="replace"))
        seen = {m["pid"] for m in _DUMP_HEADER_RE.finditer(text)}
        if expected <= seen:
            break
        time.sleep(0.1)

    return text


def _dump_via_stream(inst: Instance, procs: list[ProcRow], host: Host = LOCAL) -> tuple[str, list[Worker]]:
    """`dump_and_parse_stacks` for an instance whose log is a stream, not a
    file (see log_stream).

    The reader is attached *before* the signal: there is no offset to seek
    back to afterwards, so output produced before it starts is simply gone
    -- the opposite of the file branches, which read from a saved size.
    """
    reader = log_stream(inst, host)
    if reader is None:
        return "(no log stream)", []

    at = container_host(inst, host)  # whose pids these are, so whose namespace the signal goes to
    try:
        for proc in procs:
            signal_process(proc["pid"], signal.SIGQUIT, at)
        text = _read_until_dumped(reader, {proc["pid"] for proc in procs})
    finally:
        reader.kill()

    if not text.strip():
        return "(dump did not appear in the log)", []

    return "", parse_stack_dump(text)


def dump_and_parse_stacks(inst: Instance, host: Host = LOCAL) -> tuple[str, list[Worker]]:
    """SIGQUIT all instance top, read new log output, and parse stack dumps.

    Sends SIGQUIT to master and descendant workers, reading the log file from the
    pre-signal offset. Polls up to ~2s for all expected PID headers to appear.

    Returns (error, workers); error is non-empty (workers `[]`) only when there's
    truly nothing to show (no workers, no logfile, or nothing arrived at
    all)."""
    procs = procs_of(inst, host)
    if not procs:
        return "(no workers alive)", []

    at = container_host(inst, host)  # whose pids these are, so whose namespace the signal goes to
    expected = {proc["pid"] for proc in procs}
    text = ""

    if inst["manager"] == "docker":
        return _dump_via_stream(inst, procs, host)

    path = logfile_of(inst, host)
    if path is None or not host.is_file(path):
        return "(no logfile configured)", []

    before = host.stat_size(path) or 0
    for proc in procs:
        signal_process(proc["pid"], signal.SIGQUIT, at)

    if host.is_local:
        for _ in range(20):  # ~2s budget
            # seek to the pre-signal offset instead of rereading the whole
            # file each poll — on a multi-GB log that reread alone made the
            # dump take ages regardless of how small the actual new output was.
            with path.open("rb") as f:
                f.seek(before)
                text = f.read().decode(errors="replace")
            seen = {m["pid"] for m in _DUMP_HEADER_RE.finditer(text)}
            if expected <= seen:
                break
            time.sleep(0.1)
    else:
        # one round trip to start a streaming reader from the pre-signal
        # offset, instead of up to 20 separate ssh round trips for the poll
        # loop above -- see the local branch's own comment on why re-reading
        # from the start would be bad on a multi-GB log, remote or not.
        proc = host.popen(["tail", "-f", "-c", f"+{before + 1}", str(path)], stderr=subprocess.DEVNULL, text=False)
        try:
            text = _read_until_dumped(proc, expected)
        finally:
            proc.kill()

    if not text.strip():
        return "(dump did not appear in the log)", []

    return "", parse_stack_dump(text)


def instance_version(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's Odoo version, via the `odoo-addons-path` CLI (layout/
    addons-path detection lives there, not here) — or straight from
    odoo.sh's own `$ODOO_VERSION` env var, captured at discovery time."""
    if inst["manager"] == "odoosh":
        return inst.get("version")

    if inst["manager"] == "docker":
        # odoo-addons-path is one of our own tools, installed on the box and
        # not in the image -- and the layout it would inspect is inside the
        # container anyway. odoo can just say what it is.
        out = container_host(inst, host).run(["odoo", "--version"]).stdout
        m = re.search(r"(\d+\.\d+)", out)
        return m.group(1) if m else None

    try:
        out = host.run(["odoo-addons-path", str(instance_workdir(inst, host)), "--verbose", "--format", "json"]).stdout
    except FileNotFoundError:
        return None

    try:
        return json.loads(out).get("version")
    except (json.JSONDecodeError, ValueError):
        return None


def render_config(config: Path, version: str | None, mode: str, host: Host = LOCAL) -> str:
    """`odoo-config <mode> <config>` output — plain ini text (compact = only
    keys differing from odoo's default; expand = every valid option filled
    in). `version` is omitted when unknown; odoo-config then falls back to
    its newest schema."""
    if host.container is not None:
        # odoo-config is one of our own tools: installed on the box, never
        # in the image, and it takes a path rather than stdin -- so the
        # container's config is copied out to the box for the length of the
        # call. `docker cp` works on a stopped container too, which matters:
        # a config is exactly what you want to read when an instance won't
        # start.
        box = host.on_box
        tmp = _copy_out_of_container(host.container, config, box)
        if tmp is None:
            return f"(could not read {config} out of {host.container})"

        try:
            return render_config(Path(tmp), version, mode, box)
        finally:
            box.run(["rm", "-f", tmp])

    cmd = ["odoo-config", mode, str(config)]
    if version:
        cmd += ["--version", version]

    try:
        out = host.run(cmd)
    except FileNotFoundError:
        return "(odoo-config not found on PATH)"

    return out.stdout.strip() or out.stderr.strip() or f"(odoo-config exit {out.returncode})"


_TAIL_CHUNK = 64 * 1024


def _untty(text: str) -> str:
    """Drop the carriage returns a container's log picks up from its tty.

    docker allocates a pty when the service asks for one (doodba's
    common.yaml sets `tty: true`), and a pty turns every `\n` odoo writes
    into `\r\n`. Left in, the CR sits at the end of every line and
    `_DUMP_HEADER_RE`'s end-of-line anchor never matches, so a stack dump
    parses as zero workers (caught live: the dump was in the log, the
    Stacks tab was empty).
    """
    return text.replace("\r\n", "\n")


def log_snapshot(inst: Instance, host: Host = LOCAL, lines: int = 200) -> str | None:
    """The last `lines` of the instance's log, or None when it hasn't got
    one to read.

    A container writes to stdout, not to a file: odoo's own `logfile` is
    unset in every odoo image (doodba's included), and docker keeps the
    stream itself. So the log is `docker logs`, not a path — which is also
    why `logfile_of` returning None can't mean "no log" for this manager.
    """
    if inst["manager"] == "docker":
        # Both streams: odoo logs to stderr, docker keeps the two apart, and
        # a Logs tab showing only stdout would be empty on every instance.
        # Concatenated rather than interleaved -- there is no shell here to
        # `2>&1` with -- which is right for odoo (everything is on stderr)
        # and the reason the follow stream below merges them properly.
        out = host.run(["docker", "logs", "--tail", str(lines), inst["container"]])
        return _untty(out.stderr + out.stdout)

    path = logfile_of(inst, host)
    return None if path is None else tail(path, lines, host)


def log_stream(inst: Instance, host: Host = LOCAL) -> subprocess.Popen | None:
    """A child streaming the instance's *new* log output, or None when the
    caller should poll a local file instead (see detail.py's `poll`).

    Callers must `.kill()` it. `--tail 0` (like `tail -f -n 0`) because the
    lines already on screen came from `log_snapshot` -- without it every
    follow would replay the whole buffer into the pane.
    """
    if inst["manager"] == "docker":
        return host.popen(
            ["docker", "logs", "-f", "--tail", "0", inst["container"]],
            stderr=subprocess.STDOUT,
            text=False,
        )

    path = logfile_of(inst, host)
    if path is None or host.is_local:
        return None  # a local file is polled by size, no child needed

    return host.popen(["tail", "-f", "-n", "0", str(path)], stderr=subprocess.DEVNULL, text=False)


def tail(path: Path, lines: int = 200, host: Host = LOCAL) -> str:
    """Last `lines` of a file, or a short note if it can't be read.

    Local: reads backward in chunks from the end instead of scanning the
    whole file — a multi-GB logfile shouldn't cost more than a few reads.
    Remote: one `tail -n` call does the same job server-side.
    """
    if not host.is_local:
        result = host.run(["tail", "-n", str(lines), str(path)])
        return result.stdout if result.returncode == 0 else f"(no log: {result.stderr.strip()})"

    try:
        with path.open("rb") as f:
            end = f.seek(0, 2)
            pos = end
            data = b""

            while pos > 0 and data.count(b"\n") <= lines:
                pos = max(0, pos - _TAIL_CHUNK)
                f.seek(pos)
                data = f.read(end - pos)

            text = data.decode(errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except OSError as exc:
        return f"(no log: {exc})"


# odoo-db commands whose `--all` reveals hidden rows, mapped to the column
# that flags them (the key `--all` adds to that command's rows). The one
# place that needs to know both facts -- start_odoo_db (which flag to pass)
# and detail.py (which column to filter on) both key off this dict.
ALL_ROW_FLAGS: dict[str, str] = {"crons": "active", "modules": "installed", "users": "active"}


def start_odoo_db(
    command: str,
    db: str,
    port: str | PgTarget | None = None,
    host: Host = LOCAL,
    *,
    include_sensitive_information: bool = False,
    include_inactive: bool = False,
) -> subprocess.Popen[str] | None:
    """Start `odoo-db --output-format json <command> <db>` against `port` —
    a plain port, or a full `PgTarget` when the cluster isn't just a port on
    this box (docker's is a container with its own address and credentials).
    odoo-db has no connection flags of its own, but honors PGHOST/PGPORT/
    PGUSER/PGPASSWORD like any libpq client — set via an `env` prefix rather
    than subprocess's `env=` kwarg, so it works the same whether `odoo-db`
    runs locally or over ssh.

    Returns the live process rather than waiting on it, so a caller can
    `.kill()` it if abandoned (e.g. the tab driving it was switched away
    from) instead of blocking behind a slow query. Note this only stops *our*
    client and its thread — odoo-db opens a plain psycopg connection with no
    SIGTERM handling, so Postgres notices the dropped connection and cancels
    the backend query on its own schedule, not instantly
    (see odoo-db/db.py connect()).

    None if `odoo-db` isn't on PATH (degrade like render_config does for
    odoo-config, instead of crashing the app on a host that lacks it).

    `include_sensitive_information` mirrors odoo-db's own `--include-sensitive-information`
    flag name and drops its masking of secret-looking values (params'
    `********`), so the caller gets plaintext. The TUI always sets it — its
    reader is a human who already has a shell on this host. `oa-mcp`/
    `oa-mcp-multi` default it to False — a tool call has no such reader, and
    the plaintext would land in the agent's context — but expose it as a
    per-call opt-in.

    `include_inactive` asks the commands that have it for the rows they hide
    by default (uninstalled modules, inactive crons/users) plus the status
    column that tells them apart, so the TUI's `A` can toggle client-side.
    """
    cmd = PgTarget.of(port).env_prefix
    cmd += ["odoo-db", "--output-format", "json"]
    if include_sensitive_information:
        # odoo-db's global PII master switch -- must precede the subcommand.
        cmd += ["--include-sensitive-information"]
    cmd += [command]
    if command == "crons":
        # show scheduled actions' code
        cmd += ["--include-code"]
    if include_inactive and command in ALL_ROW_FLAGS:
        # the rows odoo-db filters out by default, plus the status column
        # (see ALL_ROW_FLAGS) to filter them on ourselves
        cmd += ["--all"]
    cmd += [db]

    try:
        return host.popen(cmd)
    except FileNotFoundError:
        return None


def parse_odoo_db_output(stdout: str, stderr: str) -> tuple[list[dict] | None, str]:
    """(rows, raw) from a `start_odoo_db` process's captured output.

    `rows` is None when the output isn't JSON (e.g. a plain message like
    "queue_job module not installed."); `raw` is then the message to show
    as-is.
    """
    raw = stdout.strip() or stderr.strip()

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, raw

    if isinstance(data, dict):
        data = [data]

    return data, raw


def table_columns(rows: list[dict]) -> list[str]:
    """Union of keys across rows, preserving first-seen order (columns vary
    per odoo-db command)."""
    columns: list[str] = []

    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    return columns


def stringify(value: object, max_cell: int = 80) -> str:
    """Render a cell: nested values (dict/list, e.g. `attachments`) as compact
    JSON, clipped to `max_cell`; `None` (a field a row genuinely has but left
    unset, e.g. crons' `code` when there's none, or every one of Mail's
    `config_parameters` rows for a key that isn't in `ir_config_parameter`)
    as a blank cell rather than the literal string "None" -- caught live
    against a real host's Mail tab, where every unset config-parameter row
    read "None" instead of blank."""
    if value is None:
        return ""

    text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else str(value)

    return text if len(text) <= max_cell else text[: max_cell - 1] + "…"


def row_matches(row: Mapping[str, object], needle: str) -> bool:
    """True if any of `row`'s cell values contains `needle`, case-insensitively.

    Values only, never the column names -- matching those would make a search
    for "value" hit every row of a key/value table.
    """
    needle = needle.lower()
    return any(needle in str(value).lower() for value in row.values())


_LOGO = r"""
   /ss/                              :so
   /hh/                              -hho
 --/hho--.     .-----.    .----.     -hho  .-----.
 +hhhhhhh+   ./yhhhhhhs+oyhhhhhhyo-  -hhs+shhhhhhhy++ossssssssss+'
   /hh/     /yhy+-' '/yhho/-...:shhs.-hhhhy/-...-/yhyo:----:sss/
   :hh/    /hhs'    -hhy.        -yhy/hhh+'       '/hhs   'oss:
   :hh/    shy.     shh-          -hhyhhs           shh- .oss-
   :hh/   'hhy      shh.          -hhyhhs           ohh:-sso.
   :hh/   'hhy      :hho'        .shy-shh/         /hhy/ss+'
   -hhs'  'hhy       /yhy+-'  '-+yhy: 'shhs/.' '.:shhsoss+'
    +yhhyhohhy        '/shhhyhhhys/'    -oyhhhyhhhyo:ossssssssssss+'
     '-///:///          '.-://:-'          .:///:.' -:::::::::::::::
"""


# ---------------------------------------------------------------- odooly ---
# odooly connects to an instance over the network, so its config is read from
# *this* machine even when the instances being watched are on a remote host:
# `oa openerp@somehost` still runs odooly locally, against the same envs the
# user has in their own `~/odooly.ini`.
ODOOLY_CONFIG = Path("~/odooly.ini").expanduser()

# Trobz names instances after the environment they serve, and odooly envs
# after the same thing abbreviated (or not) -- `openerp-acme18-integration`
# is configured as `acme18-integration` or `acme18-int`.
_ENV_ABBREVIATIONS = {"integration": "int", "staging": "stag", "production": "prod"}
# what a manager prefixes an instance name with, which no odooly env repeats
_INSTANCE_PREFIXES = ("openerp-", "odoo-")


class OdoolyEnv(TypedDict):
    """One section of `odooly.ini`: the env name, and the database it pins
    (absent when the section leaves `database` unset)."""

    name: str
    db: str


def read_odooly_envs(path: Path = ODOOLY_CONFIG) -> list[OdoolyEnv]:
    """Every environment in `path`, as (name, database).

    Read with configparser rather than `odooly.read_config`, which returns
    the password too: matching only needs the name and the database, and
    what isn't read can't be leaked into a log or a screen.

    Empty when the file is missing or unparseable -- odooly support is
    opt-in and best-effort, and a broken ini shouldn't take the app down.
    """
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(path.read_text())
    except (OSError, configparser.Error):
        return []

    return [{"name": name, "db": parser.get(name, "database", fallback="")} for name in parser.sections()]


def _name_variants(name: str) -> set[str]:
    """`name` as it may appear on either side of the match: as written, and
    with each environment word abbreviated or spelled out."""
    variants = {name}

    for long, short in _ENV_ABBREVIATIONS.items():
        variants |= {variant.replace(long, short) for variant in variants if long in variant}
        variants |= {variant.replace(short, long) for variant in variants if short in variant}

    return variants


def instance_env_name(instance_name: str) -> str:
    """`instance_name` stripped of what only a process manager adds --
    `openerp-acme18-integration.service` is the `acme18-integration` an
    odooly env would be named after."""
    name = instance_name.removesuffix(".service")

    for prefix in _INSTANCE_PREFIXES:
        name = name.removeprefix(prefix)

    return name


def match_odooly_env(instance_name: str, db: str, envs: list[OdoolyEnv]) -> str | None:
    """The odooly env serving `db` on `instance_name`, or None.

    An env qualifies when its name matches the instance's -- exactly, in
    either spelling (`-integration` / `-int`), or as that name plus a suffix,
    since a multi-db instance is usually configured one env per database
    (`acme18-int-db1`). The database has to match exactly whenever the env
    names one, which is what keeps those per-db envs apart.

    Ranked, best first: an env that names this database beats one that names
    none, and among equals the closest name wins. Ties are broken by name so
    the answer doesn't depend on the ini's ordering.
    """
    wanted = _name_variants(instance_env_name(instance_name))
    matches = []

    for env in envs:
        if env["db"] and env["db"] != db:
            continue

        names = _name_variants(env["name"])
        exact = bool(names & wanted)
        prefixed = any(name.startswith(f"{want}-") for name in names for want in wanted)
        if not (exact or prefixed):
            continue

        # a db-pinned env first, then an exact name, then the shortest suffix
        matches.append((not env["db"], not exact, len(env["name"]), env["name"]))

    return min(matches)[3] if matches else None


def run_odooly_script(script: str, env: str, *extra_args: str, timeout: int = 300) -> str:
    """Run one of `odoo_activity.scripts` against odooly env `env`, and
    return what it printed (stdout, then stderr).

    Deliberately local and never over `Host`: odooly reaches the instance
    over the network using this machine's `~/odooly.ini`, which the watched
    host neither has nor should be asked for.

    `sys.executable -m` rather than a console script, so it is the
    interpreter running odoo-activity -- the one odooly is installed in --
    whatever `PATH` says.

    `extra_args` is appended verbatim after `--env <env>`, for a script
    that needs more than the env to run (e.g. send_test_mail's `--to`).
    """
    argv = [sys.executable, "-m", f"odoo_activity.scripts.{script}", "--env", env, *extra_args]

    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return f"({script} timed out after {timeout}s)"
    except OSError as exc:
        return f"(cannot run {script}: {exc})"

    return "\n".join(part for part in (done.stdout.strip(), done.stderr.strip()) if part)


def about_text() -> str:
    """Static overview shown when the host pane is focused."""
    return (
        f"{_LOGO}\nhost: {socket.gethostname()}\n{platform.platform()}\n\nodoo-activity — local Odoo instance monitor"
    )
