"""oa-mcp — MCP server exposing odoo-activity's read-only capacities.

POC. Every tool is a thin wrapper over :mod:`odoo_activity.probes` — no
logic lives here (see that module for the actual system probes, shared with
the TUI). The server itself always runs locally. `oa-mcp [host]` pins every
tool call to that one target (local if omitted) -- the counterpart to `oa
[host]`. `oa-mcp-multi` instead leaves the target per-call, gated by
--host-filter.

`--enable-odooly` adds the one non-read-only exception: list_odooly_envs,
instance_odooly_env and odooly_run_script, matching a database against
~/odooly.ini and running the packaged scripts -- the same actions the TUI's
`oa --enable-odooly` offers a human through the Toolbox, now offered to the
agent directly.
"""

import functools
import inspect
import re
import subprocess
import time
from pathlib import Path
from typing import Annotated, Literal

import typer
from mcp.server.fastmcp import FastMCP
from typing_extensions import TypedDict

from odoo_activity import probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance, ProcRow, Worker

mcp = FastMCP("odoo-activity")
app = typer.Typer(add_completion=False)
app_multi = typer.Typer(add_completion=False)


# oa-mcp and oa-mcp-multi are "pinned vs multi" -- oa-mcp locks every tool
# to whatever host it was launched against (_pinned_target set), the
# counterpart to `oa host`; oa-mcp-multi leaves _pinned_target unset and
# gates per-call host with --host-filter instead.
_DEFAULT_HOST_FILE = Path.home() / ".ssh" / "config"
_WILDCARD = re.compile(r"[*?]")
_host_filter: re.Pattern[str] | None = None
_host_file: Path = _DEFAULT_HOST_FILE
_pinned_target: Host | None = None
# Launch-time only, never a per-call tool argument -- an agent must not be
# able to flip this on itself. Set solely by --include-sensitive-information
# on the oa-mcp/oa-mcp-multi command line, i.e. by the human starting the
# server.
_include_sensitive_information = False
# Same launch-time-only spirit as _include_sensitive_information, but the
# risk here is action rather than disclosure: odooly logs in and can write
# to the database it matches. Set solely by --enable-odooly.
_enable_odooly = False


def _allowed(alias: str | None) -> bool:
    # local is always allowed; unset filter allows everything (odoo
    # dbfilter's "empty means no restriction" spirit). fullmatch, not
    # odoo's match: this gates real ssh access, a prefix match would let
    # "prod" filter through "prod-evil.example.com".
    return alias is None or _host_filter is None or bool(_host_filter.fullmatch(alias))


def _check_host(alias: str | None) -> None:
    if not _allowed(alias):
        msg = f"host {alias!r} rejected by --host-filter"
        raise ValueError(msg)


def _check_odooly_enabled() -> None:
    if not _enable_odooly:
        msg = "odooly support disabled -- restart the server with --enable-odooly"
        raise ValueError(msg)


def _resolve_host(host: str | None, ssh_port: int | None) -> Host:
    if _pinned_target is not None:
        if host is not None and host != _pinned_target.alias:
            msg = f"host {host!r} rejected: this server is pinned to {_pinned_target.alias!r}"
            raise ValueError(msg)
        return _pinned_target

    _check_host(host)
    return Host(alias=host, port=ssh_port)


def _pinned_host(fn):
    """Resolve host/ssh_port into `target` for `fn`. `fn` takes `target: Host`
    keyword-only; rebuilds the public signature so FastMCP's schema still shows
    host/ssh_port."""
    sig = inspect.signature(fn, eval_str=True)
    params = [p for name, p in sig.parameters.items() if name != "target"]
    params += [
        inspect.Parameter("host", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None),
        inspect.Parameter("ssh_port", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=int | None),
    ]

    @functools.wraps(fn)
    def wrapper(*args, host=None, ssh_port=None, **kwargs):
        return fn(*args, target=_resolve_host(host, ssh_port), **kwargs)

    setattr(wrapper, "__signature__", sig.replace(parameters=params))  # noqa: B010
    return wrapper


def _ssh_config_aliases(path: Path) -> list[str]:
    """Literal (non-wildcard, non-negated) aliases from `Host` entries in an
    ssh config file. No `Include` following -- point --host-file at the
    file that actually lists them if it matters."""
    if not path.is_file():
        return []

    aliases = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        parts = line.split()
        if not parts or parts[0].lower() != "host":
            continue
        aliases.extend(p for p in parts[1:] if not p.startswith("!") and not _WILDCARD.search(p))

    return aliases


class HostStats(TypedDict):
    mem_pct: float
    swap_pct: float
    load_avg: tuple[float, float, float]
    uptime: float


class InstanceDatabases(TypedDict):
    databases: list[str]
    db_port: str | None
    neutralized: dict[str, dict]


class InstanceTop(TypedDict):
    odoo: list[ProcRow]
    postgres: list[ProcRow]


class StackDump(TypedDict):
    error: str
    workers: list[Worker]


DbQueryCommand = Literal["modules", "crons", "jobs", "users", "locks", "params"]
OdoolyScript = Literal["create_test_job", "restore_app_icons", "send_test_mail"]


# Discovery is 8 ssh round trips (~620ms remote) behind every tool. Only the
# sweep is cached; status is re-probed per call. uptime rides along, so it
# can be this stale.
_DISCOVERY_TTL = 30.0
_discovered: dict[tuple[str | None, int | None], tuple[float, list[Instance]]] = {}


def _instances(host: Host) -> list[Instance]:
    key = (host.alias, host.port)
    cached = _discovered.get(key)
    now = time.monotonic()

    if cached is None or now - cached[0] >= _DISCOVERY_TTL:
        _discovered[key] = (now, probes.list_instances(host))

    return _discovered[key][1]


def _with_status(inst: Instance, host: Host) -> Instance:
    return {**inst, "status": probes.instance_status(inst, host)}


def _find(name: str, host: Host) -> Instance | None:
    """The instance named `name` on `host`, or None — the lookup every
    per-instance tool below starts with."""
    return next((i for i in _instances(host) if i["name"] == name), None)


@mcp.tool()
@_pinned_host
def list_instances(*, target: Host) -> list[Instance]:
    """Every Odoo instance on `host` (systemd --user, supervisor, odoo.sh),
    with each instance's status corrected for a live process behind an
    ambiguous manager-reported "stopped".

    Args:
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    return [_with_status(inst, target) for inst in _instances(target)]


@mcp.tool()
@_pinned_host
def get_instance(name: str, *, target: Host) -> Instance | None:
    """A single instance by name (as reported by `list_instances`), or None
    if no instance with that name is currently known.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    return _with_status(inst, target) if inst else None


@mcp.tool()
@_pinned_host
def list_top(name: str, *, target: Host) -> list[ProcRow]:
    """The instance's master process plus every descendant worker, or an
    empty list if the instance isn't found or has no live process.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    return probes.procs_of(inst, target) if inst else []


@mcp.tool()
def list_hosts() -> list[str]:
    """Host aliases available for the `host` argument of the other tools:
    literal `Host` entries from --host-file (default ~/.ssh/config, wildcard
    and negated patterns skipped), narrowed by --host-filter."""
    if _pinned_target is not None:
        return [_pinned_target.alias] if _pinned_target.alias else []

    return [alias for alias in _ssh_config_aliases(_host_file) if _allowed(alias)]


@mcp.tool()
@_pinned_host
def host_stats(*, target: Host) -> HostStats | None:
    """Memory/swap/load/uptime for `host` itself, or None on a failed remote
    probe (a bad connection, not a parse bug — just skip this call).

    Args:
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    stats = probes.read_host_stats(target)
    if stats is None:
        return None

    _cpu_times, (mem_pct, swap_pct), load_avg, uptime = stats
    return {"mem_pct": mem_pct, "swap_pct": swap_pct, "load_avg": load_avg, "uptime": uptime}


@mcp.tool()
@_pinned_host
def instance_databases(name: str, *, target: Host) -> InstanceDatabases | None:
    """The instance's databases and the postgres port they live on, or None
    if the instance isn't found.

    `neutralized` maps each database to a report: the raw signals it was
    read from, plus the `state` those signals add up to.

    - `neutralized` — claimed and confirmed: nothing on it can reach the
      outside (a staging/test copy, safe to act on).
    - `partial` — the signals disagree: a flag written by hand, a
      neutralization that died halfway, or a cron switched back on
      afterwards. Treat as live until a human checks.
    - `not_neutralized` — a live database, where every action is production.

    The signals behind it: `flag` is `database.is_neutralized` as the db
    claims it, `stub` counts Odoo's dead-end relay (only written from Odoo
    16, so it is not required on older `version`s), `live_relays` and
    `live_crons` count what can still fire, `extras` names the module
    surfaces still live (payment providers, IAP credits, bank feeds, …)
    with a count each, and `checked` lists the surfaces that were evaluated
    at all — a surface absent from `checked` on every database means its
    table was never found, not that it passed.

    A database missing from the map is one psql could not read (postgres
    down, or not an odoo database) — unknown, never a guess either way.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return None

    databases, db_port = probes.databases_of(inst, target)
    # pg_target_of, not db_port: a docker instance's postgres also needs its
    # container address and credentials to be reachable at all.
    neutralized = probes.neutralization_of(databases, probes.pg_target_of(inst, target), target) if databases else {}
    return {"databases": databases, "db_port": db_port, "neutralized": neutralized}


@mcp.tool()
@_pinned_host
def instance_top(name: str, *, target: Host) -> InstanceTop | None:
    """The instance's odoo top and its postgres backends, or None if
    the instance isn't found. Additive to `list_top`, which stays
    odoo-only.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return None

    odoo, postgres = probes.instance_procs(inst, target)
    return {"odoo": odoo, "postgres": postgres}


@mcp.tool()
@_pinned_host
def instance_version(name: str, *, target: Host) -> str | None:
    """The instance's Odoo version, or None if it's not found or the version
    can't be determined.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    return probes.instance_version(inst, target) if inst else None


@mcp.tool()
@_pinned_host
def instance_config(
    name: str,
    mode: Literal["compact", "expand"] = "compact",
    *,
    target: Host,
) -> str:
    """The instance's rendered odoo config: `compact` (only keys differing
    from odoo's default) or `expand` (every valid option filled in).

    Args:
        name: instance name as `list_instances` reports it.
        mode: `compact` or `expand`.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    path = probes.configfile_of(inst, target)
    if path is None:
        return "(no config file found)"

    version = probes.instance_version(inst, target)
    return probes.render_config(path, version, mode, target)


@mcp.tool()
@_pinned_host
def instance_log_tail(name: str, lines: int = 200, *, target: Host) -> str:
    """The last `lines` of the instance's odoo logfile.

    Args:
        name: instance name as `list_instances` reports it.
        lines: number of trailing lines to return.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    path = probes.logfile_of(inst, target)
    if path is None:
        return "(no logfile configured)"

    return probes.tail(path, lines, target)


@mcp.tool()
@_pinned_host
def instance_shell_command(name: str, *, target: Host) -> str | None:
    """The command to open an interactive `odoo shell` for the instance,
    reusing its live process's own venv/config -- to run yourself or
    suggest to the user as a next step. None if the instance isn't found
    or isn't running.

    ssh-wrapped when `host` is remote, so it's runnable as returned rather
    than needing `host` prefixed onto it by hand.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return None

    cmd = probes.shell_command(inst, target)
    return target.shell_invocation(cmd) if cmd else None


@mcp.tool()
@_pinned_host
def instance_dump_stacks(name: str, *, target: Host) -> StackDump:
    """SIGQUIT every process of the instance and parse the resulting stack
    dumps out of its log — a live thread/frame snapshot for diagnosing a
    stuck or spinning worker. `error` is non-empty (`workers` empty) only
    when there's nothing to show at all (no logfile, no workers, or nothing
    arrived).

    Each thread carries db/uid/url plus, on Odoo 17+, qc/qt/pt — a live
    snapshot at signal time, not an accumulator:
      qc = query_count, number of SQL queries so far this request.
      qt = query_time, SQL time so far this request, in seconds.
      pt = python_time, Odoo's own name for wall-clock-since-request-start
        minus qt — time spent outside the DB. Not "processing time".
    All are None when the thread isn't mid-request, or on pre-17 instances
    where qc/qt/pt were never reported.

    Check `idle` before reading qc/qt/pt: Odoo never clears a thread's
    request fields when it goes idle, so a thread back in its accept/wait
    loop (`idle: true` — innermost frame is a select/poll/sleep/wait) still
    shows its *last* request's numbers, not a live one. A large `pt` only
    means something on a non-idle thread.

    `workers`/`threads` are sorted busy-first (idle ones last), and each
    thread's `frames` are innermost-first — read frames[0], not frames[-1],
    for what a thread is doing right now.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    # idle detection: probes._IDLE_FRAME_FUNCS / _IDLE_FRAME_PATH_MARKERS
    inst = _find(name, target)
    if inst is None:
        return {"error": "(no such instance)", "workers": []}

    error, workers = probes.dump_and_parse_stacks(inst, target)
    return {"error": error, "workers": probes.stacks_by_activity(workers)}


@mcp.tool()
@_pinned_host
def long_queries(db: str, port: str | None = None, *, target: Host) -> list[dict]:
    """Non-idle queries on `db`, longest-running first.

    Args:
        db: database name.
        port: postgres port, if the instance's cluster isn't the default one.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    return probes.long_queries(db, port, target)


@mcp.tool()
@_pinned_host
def db_query(
    db: str,
    command: DbQueryCommand,
    port: str | None = None,
    *,
    target: Host,
) -> list[dict] | str:
    """Run an `odoo-db` diagnostic command against `db` — a scoped subset:
    modules, crons, jobs, users, locks, params. Stats/bloat/attachments/studio
    and the audit-oriented commands (they write a `$db.json` file to disk) are
    out of scope here. `params` returns `ir_config_parameter` rows, with
    secret-looking values masked by odoo-db unless the server was started
    with `--include-sensitive-information` — a launch-time-only choice, not
    a per-call argument here: no tool call can enable it on its own.

    Args:
        db: database name.
        command: modules, crons, jobs, users, locks, or params.
        port: postgres port, if the instance's cluster isn't the default one.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    proc = probes.start_odoo_db(command, db, port, target, include_sensitive_information=_include_sensitive_information)
    if proc is None:
        return "(odoo-db not found on PATH)"

    try:
        result = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "(odoo-db timed out after 90s)"

    rows, raw = probes.parse_odoo_db_output(*result)
    return rows if rows is not None else raw


@mcp.tool()
@_pinned_host
def mail_audit(db: str, port: str | None = None, *, target: Host) -> dict | str:
    """Outbound mail config audit for `db`: whether the database is
    neutralized (the most common reason mail never leaves one), config
    parameters, per-company alias domains, company/OdooBot/admin addresses,
    outgoing mail servers (flagged if a known test-mail catcher or a known
    managed relay), and mass_mailing's install state — see odoo-db's own
    `mail` command.

    Kept out of `db_query`'s scoped command set rather than added there:
    odoo-db's `mail` answers one nested object, not the flat row list every
    `db_query` command shares — the same reason the TUI renders it through
    its own `panes/mail.py` instead of the generic table pane. Secret-looking
    values (`smtp_user`/`smtp_pass`) are masked by odoo-db unless the server
    was started with `--include-sensitive-information` — launch-time only,
    same rule `db_query`'s `params` follows.

    Args:
        db: database name.
        port: postgres port, if the instance's cluster isn't the default one.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    proc = probes.start_odoo_db("mail", db, port, target, include_sensitive_information=_include_sensitive_information)
    if proc is None:
        return "(odoo-db not found on PATH)"

    try:
        result = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "(odoo-db timed out after 90s)"

    rows, raw = probes.parse_odoo_db_output(*result)
    if rows is None:
        return raw
    return rows[0] if rows else {}


@mcp.tool()
def list_odooly_envs() -> list[str]:
    """Every environment section name in ~/odooly.ini -- what `env` on
    `odooly_run_script` accepts, and the set `instance_odooly_env` matches
    against. Requires --enable-odooly.
    """
    _check_odooly_enabled()
    return [env["name"] for env in probes.read_odooly_envs()]


@mcp.tool()
def instance_odooly_env(name: str, db: str) -> str | None:
    """The odooly env (a section of ~/odooly.ini) that serves `db` on
    instance `name`, or None if none matches. Requires --enable-odooly.

    Always resolved locally against this machine's own ~/odooly.ini,
    independent of any `host`/`ssh_port` used to discover the instance
    itself -- odooly reaches the instance over the network, not over ssh.

    Args:
        name: instance name as `list_instances` reports it.
        db: database name, as `instance_databases` reports it.
    """
    _check_odooly_enabled()
    return probes.match_odooly_env(name, db, probes.read_odooly_envs())


@mcp.tool()
def odooly_run_script(script: OdoolyScript, env: str, to: str | None = None) -> str:
    """Run one of odoo-activity's packaged odooly scripts against `env`, and
    return what it printed (stdout, then stderr). Requires --enable-odooly.

    create_test_job: queue one of queue_job's own test jobs, to check
        whether a runner picks it up.
    restore_app_icons: rewrite the apps-menu icons a restore-without-
        filestore leaves blank.
    send_test_mail: send one real email through mail.mail, to check outbound
        mail actually reaches an inbox. Needs `to`.

    Always local, even against a remote `host`: odooly reaches the instance
    over the network from wherever this server runs, using its own
    ~/odooly.ini -- never over ssh.

    Args:
        script: create_test_job, restore_app_icons, or send_test_mail.
        env: odooly env, e.g. from `instance_odooly_env` or
            `list_odooly_envs`.
        to: recipient address -- required for send_test_mail, ignored
            otherwise.
    """
    _check_odooly_enabled()
    if script == "send_test_mail":
        if not to:
            msg = "send_test_mail needs `to` (recipient address)"
            raise ValueError(msg)
        return probes.run_odooly_script(script, env, "--to", to)

    return probes.run_odooly_script(script, env)


@app.command()
def main(
    host: str | None = typer.Argument(
        None, help="[user@]host to pin this server to, e.g. openerp@demo. Omit for local."
    ),
    port: int | None = typer.Option(None, "--port", "-p", help="ssh port, for a non-default one."),
    transport: Literal["stdio", "streamable-http"] = typer.Option(
        "stdio", "--transport", help="stdio (spawned by the client) or streamable-http (network server)"
    ),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="streamable-http only"),
    bind_port: int = typer.Option(8000, "--bind-port", help="streamable-http only"),
    include_sensitive_information: bool = typer.Option(
        False,
        "--include-sensitive-information",
        help="Let db_query's `params` command return secret-looking values unmasked. Launch-time only "
        "-- no tool call can turn this on itself; set it only if every caller of this server should see "
        "plaintext secrets.",
    ),
    enable_odooly: bool = typer.Option(
        False,
        "--enable-odooly",
        help="Expose list_odooly_envs/instance_odooly_env/odooly_run_script, matching databases against "
        "~/odooly.ini the same way `oa --enable-odooly` does. Launch-time only -- no tool call can turn "
        "this on itself; set it only if the agent should be able to log in and act on a matched database.",
    ),
) -> None:
    """oa-mcp — MCP server exposing odoo-activity's read-only capacities,
    pinned to a single host: the counterpart to `oa host`, so a human on
    `oa host` and their agent on `oa-mcp host` are always looking at the
    same target. Omit `host` to pin to local, matching bare `oa`."""
    global _pinned_target, _include_sensitive_information, _enable_odooly
    _pinned_target = Host(alias=host, port=port)
    _include_sensitive_information = include_sensitive_information
    _enable_odooly = enable_odooly

    if transport == "streamable-http":
        mcp.settings.host = bind_host
        mcp.settings.port = bind_port

    mcp.run(transport=transport)


@app_multi.callback(invoke_without_command=True)
def main_multi(
    transport: Literal["stdio", "streamable-http"] = typer.Option(
        "stdio", "--transport", help="stdio (spawned by the client) or streamable-http (network server)"
    ),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="streamable-http only"),
    bind_port: int = typer.Option(8000, "--bind-port", help="streamable-http only"),
    host_filter: str = typer.Option(
        "--host-filter",
        help="regex (odoo dbfilter-style); only ssh targets it fullmatches are reachable. Unset: no restriction.",
    ),
    host_file: Annotated[
        Path,
        typer.Option("--host-file", help="ssh config to read literal Host aliases from, for list_hosts()."),
    ] = _DEFAULT_HOST_FILE,
    include_sensitive_information: bool = typer.Option(
        False,
        "--include-sensitive-information",
        help="Let db_query's `params` command return secret-looking values unmasked. Launch-time only "
        "-- no tool call can turn this on itself; set it only if every caller of this server should see "
        "plaintext secrets.",
    ),
    enable_odooly: bool = typer.Option(
        False,
        "--enable-odooly",
        help="Expose list_odooly_envs/instance_odooly_env/odooly_run_script, matching databases against "
        "~/odooly.ini the same way `oa --enable-odooly` does. Launch-time only -- no tool call can turn "
        "this on itself; set it only if the agent should be able to log in and act on a matched database.",
    ),
) -> None:
    """oa-mcp-multi — same tools as oa-mcp, capped by --host-filter."""
    global _host_filter, _host_file, _include_sensitive_information, _enable_odooly
    if transport == "streamable-http":
        mcp.settings.host = bind_host
        mcp.settings.port = bind_port

    _host_filter = re.compile(host_filter) if host_filter else None
    _host_file = host_file
    _include_sensitive_information = include_sensitive_information
    _enable_odooly = enable_odooly

    mcp.run(transport=transport)


if __name__ == "__main__":
    # Stay pointing at single-host, dev-run default
    app()
