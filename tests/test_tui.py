import asyncio
import json
import signal
from types import SimpleNamespace
from typing import cast

import pytest
from textual.widgets import DataTable

from odoo_activity import probes, tui
from odoo_activity.host import Host
from odoo_activity.panes import detail as detail_mod


@pytest.fixture(autouse=True)
def _no_neutralization_probe(monkeypatch):
    """Keep the db-row neutralization probe off this file's real shell.

    Every app test stubs `databases_of`; without this the follow-up probe
    would still run `systemctl` and `psql` against whatever box the suite
    happens to run on -- slow, and answering about the developer's own
    databases. A test that is about the tag just sets its own stubs on top.
    """
    monkeypatch.setattr(tui, "pg_target_of", lambda *_a, **_k: probes.PgTarget())
    monkeypatch.setattr(tui, "neutralization_of", lambda *_a, **_k: {})


def test_bar_colors_by_htop_thresholds():
    assert "[green]" in tui._bar(49)
    assert "[yellow]" in tui._bar(50)
    assert "[yellow]" in tui._bar(79)
    assert "[red]" in tui._bar(80)


def test_parse_odoo_db_output_falls_back_to_raw_for_non_json():
    rows, raw = probes.parse_odoo_db_output("queue_job module not installed.\n", "")
    assert rows is None
    assert raw == "queue_job module not installed."


def test_parse_odoo_db_output_wraps_a_single_json_object():
    rows, _raw = probes.parse_odoo_db_output('{"db": "demo"}', "")
    assert rows == [{"db": "demo"}]


def test_mail_server_creds_cell_masked_shows_set():
    from odoo_activity.panes.mail import _mail_server_creds_cell

    assert _mail_server_creds_cell("********", "********") == "set"
    assert _mail_server_creds_cell("********", None) == "set"


def test_mail_server_creds_cell_revealed_shows_real_values():
    from odoo_activity.panes.mail import _mail_server_creds_cell

    assert _mail_server_creds_cell("svc@acme.com", "s3cr3t") == "svc@acme.com/s3cr3t"
    assert _mail_server_creds_cell("svc@acme.com", None) == "svc@acme.com"
    assert _mail_server_creds_cell(None, None) == ""


def test_render_mail_writes_one_table_per_nonempty_section():
    """odoo-db's `mail` command answers one nested object -- each section
    its own list, with its own columns -- rather than the flat row list
    every other db-tab command returns. render_mail (not the generic table
    renderer) turns that into one Rich Table per non-empty section, so the
    TUI reads the same way the CLI's own text output does; a `section`
    column mashing everything into one table (the earlier design) read as
    mostly blank cells on a real host."""
    from rich.console import Group
    from rich.table import Table

    from odoo_activity.panes.mail import render_mail

    audit = {
        "config_parameters": [{"key": "mail.catchall.domain", "value": "example.com", "explanation": ""}],
        "alias_domains": None,
        "addresses": [{"partner_id": 1, "label": "Company", "email": "a@example.com", "is_default": False}],
        "mail_servers": [],
        "modules": [{"name": "mass_mailing", "state": "installed"}],
    }

    class _FakeBody:
        def __init__(self):
            self.written = None

        def clear(self):
            self.written = None

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    assert isinstance(body.written, Group)
    renderables = list(body.written.renderables)
    # config_parameters, addresses, mail_servers ("(none defined...)" text), modules --
    # alias_domains is None (pre-Odoo 17) so it contributes nothing at all.
    titles = [r.title for r in renderables if isinstance(r, Table)]
    assert titles == ["Config parameters", "Relevant addresses", "Relevant modules"]


def test_render_mail_alias_domain_missing_reads_as_normal_on_a_clean_install():
    """alias_domain_id IS NULL alone is the documented state of a clean
    17+ install (mail installed, nothing to migrate) -- not a
    misconfiguration, so this must not read as an alarm: it flags nearly
    every real database otherwise."""
    from rich.table import Table

    from odoo_activity.panes.mail import render_mail

    audit = {
        "is_legacy_mail_config_configured": False,
        "alias_domains": [
            {
                "company_id": 1,
                "company_name": "Acme",
                "alias_domain_id": None,
                "alias_domain": None,
                "bounce_email": None,
                "catchall_email": None,
                "default_from_email": None,
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    tables = [r for r in list(body.written.renderables) if isinstance(r, Table)]
    alias_table = next(t for t in tables if t.title == "Alias domains (Odoo 17+, authoritative)")
    cell = str(next(iter(alias_table.columns[1].cells)))
    assert cell == "(not set -- normal for a clean 17+ install)"
    assert "stuck" not in cell.lower()


def test_render_mail_alias_domain_missing_warns_when_legacy_config_survives():
    """The genuinely broken case: a leftover pre-17 ICP value means the
    v16-to-17 alias-domain migration never ran (or failed)."""
    from rich.table import Table

    from odoo_activity.panes.mail import render_mail

    audit = {
        "is_legacy_mail_config_configured": True,
        "alias_domains": [
            {
                "company_id": 1,
                "company_name": "Acme",
                "alias_domain_id": None,
                "alias_domain": None,
                "bounce_email": None,
                "catchall_email": None,
                "default_from_email": None,
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    tables = [r for r in list(body.written.renderables) if isinstance(r, Table)]
    alias_table = next(t for t in tables if t.title == "Alias domains (Odoo 17+, authoritative)")
    cell = str(next(iter(alias_table.columns[1].cells)))
    assert "stuck v16-to-17 migration" in cell


def test_render_mail_tolerates_a_host_whose_odoo_db_predates_is_legacy_mail_config_configured():
    """Same graceful-degradation convention as is_test_catcher: an older
    odoo-db's bundle simply lacks this key, and must not crash."""
    from odoo_activity.panes.mail import render_mail

    audit = {
        "alias_domains": [
            {
                "company_id": 1,
                "company_name": "Acme",
                "alias_domain_id": None,
                "alias_domain": None,
                "bounce_email": None,
                "catchall_email": None,
                "default_from_email": None,
            }
        ],
        # no "is_legacy_mail_config_configured" key -- an older odoo-db's shape
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type] -- must not raise KeyError


def test_render_mail_of_an_empty_bundle_shows_the_no_server_placeholder():
    """An empty/missing bundle still shows one thing: the mail_servers
    section always contributes something (a table or this placeholder),
    so the group is never actually empty."""
    from rich.console import Group
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, {})  # ty: ignore[invalid-argument-type]

    assert isinstance(body.written, Group)
    renderables = list(body.written.renderables)
    assert len(renderables) == 1
    assert isinstance(renderables[0], Text)
    assert str(renderables[0]) == "Outgoing mail servers: (none defined -- odoo will use localhost:25)"


def test_render_mail_flags_a_test_catcher_server_with_an_explicit_warning():
    """A test-mail catcher (mailhog, ...) accepts mail and Odoo marks it
    sent -- indistinguishable from a working relay anywhere else, so this
    tab is the one place that spells out why a real send never arrives
    (odoo-db's own `is_test_catcher` flag, see get_mail_servers)."""
    from rich.table import Table
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    audit = {
        "mail_servers": [
            {
                "sequence": 10,
                "name": "mailhog",
                "smtp_host": "mailhog-acme18-staging",
                "smtp_port": 2025,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "none",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                "is_test_catcher": True,
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    renderables = list(body.written.renderables)
    assert isinstance(renderables[0], Table)
    assert next(iter(renderables[0].columns[1].cells)) == "mailhog"
    assert [c.header for c in renderables[0].columns] == [
        "seq",
        "name",
        "host:port",
        "creds",
        "encryption/auth",
        "from_filter",
        "active",
    ]
    assert isinstance(renderables[1], Text)
    assert "mailhog" in str(renderables[1])  # names the offending server, not just "server(s) above"
    assert "test-mail catcher" in str(renderables[1])
    assert "never relay it anywhere real" in str(renderables[1])
    assert renderables[1].style == "bold red"


def test_mail_servers_table_folds_token_shaped_columns_instead_of_truncating_them():
    """host:port and creds hold single unbreakable tokens -- rich's default
    ellipsis overflow drops characters off a hostname as soon as the pane is
    narrow (smtp.sendg... vs smtp.sendgrid.net.evil.example is exactly the
    distinction this tab exists to preserve), reproduced live at a 120-column
    terminal. name stays on ellipsis: it has spaces to wrap on, so folding
    it mid-word would only make it uglier for no benefit."""
    from rich.table import Table

    from odoo_activity.panes.mail import render_mail

    audit = {
        "mail_servers": [
            {
                "sequence": 1,
                "name": "Production relay",
                "smtp_host": "smtp.sendgrid.net",
                "smtp_port": 587,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "starttls",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                "is_test_catcher": False,
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    table = next(r for r in list(body.written.renderables) if isinstance(r, Table))
    overflow_by_header = {c.header: c.overflow for c in table.columns}
    assert overflow_by_header["host:port"] == "fold"
    assert overflow_by_header["creds"] == "fold"
    assert overflow_by_header["name"] == "ellipsis"
    assert overflow_by_header["from_filter"] == "ellipsis"


def test_render_mail_flags_a_known_production_relay_positively():
    """The positive counterpart to is_test_catcher: not being flagged as a
    test catcher is an absence, not a confirmation -- known_production_relay
    (odoo-db's own flag, matched on host+port against Google/M365) is a real
    "yes, this is a real managed relay" signal, shown in green rather than
    the test-catcher's red warning."""
    from rich.table import Table
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    audit = {
        "mail_servers": [
            {
                "sequence": 1,
                "name": "Primary",
                "smtp_host": "smtp-relay.gmail.com",
                "smtp_port": 587,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "starttls",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                "is_test_catcher": False,
                "known_production_relay": "Google Workspace SMTP relay",
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    renderables = list(body.written.renderables)
    assert isinstance(renderables[0], Table)
    assert next(iter(renderables[0].columns[1].cells)) == "Primary"
    assert isinstance(renderables[1], Text)
    assert "KNOWN RELAY" in str(renderables[1])
    assert "Google Workspace SMTP relay" in str(renderables[1])
    assert renderables[1].style == "bold green"


def test_render_mail_has_no_warning_when_no_server_is_a_test_catcher():
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    audit = {
        "mail_servers": [
            {
                "sequence": 1,
                "name": "Real Relay",
                "smtp_host": "smtp.sendgrid.net",
                "smtp_port": 587,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "starttls",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                "is_test_catcher": False,
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    renderables = list(body.written.renderables)
    assert not any(isinstance(r, Text) and "TEST CATCHER" in str(r) for r in renderables)


def test_render_mail_leads_with_a_neutralization_banner_when_is_neutralized():
    """database.is_neutralized (set on every odoo.sh staging build) is the
    single most common reason mail never leaves an Odoo database -- surfaced
    as the very first renderable so a reader doesn't have to piece it
    together from an inactive relay and an unfamiliar stub row."""
    from rich.table import Table
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    audit = {"is_neutralized": True, "mail_servers": []}

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    renderables = list(body.written.renderables)
    assert isinstance(renderables[0], Text)
    assert "NEUTRALIZED" in str(renderables[0])
    assert renderables[0].style == "bold red"
    # mail_servers=[] still contributes its own placeholder right after.
    assert isinstance(renderables[1], Text)
    assert "none defined" in str(renderables[1])
    assert not any(isinstance(r, Table) for r in renderables)


def test_render_mail_has_no_neutralization_banner_when_not_neutralized():
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    audit = {"is_neutralized": False, "mail_servers": []}

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    renderables = list(body.written.renderables)
    assert not any(isinstance(r, Text) and "NEUTRALIZED" in str(r) for r in renderables)


def test_render_mail_flags_the_neutralization_stub_server_with_a_summary_line():
    """base/data/neutralize.sql inserts exactly this (name, host) row after
    disabling every pre-existing relay -- flagged so it isn't mistaken for a
    real, working server, and so the *other*, now-inactive row isn't
    mistaken for a broken one."""
    from rich.table import Table
    from rich.text import Text

    from odoo_activity.panes.mail import render_mail

    audit = {
        "is_neutralized": True,
        "mail_servers": [
            {
                "sequence": None,
                "name": "neutralization - disable emails",
                "smtp_host": "invalid",
                "smtp_port": 1025,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "none",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                "is_test_catcher": False,
                "known_production_relay": None,
                "is_neutralization_stub": True,
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    renderables = list(body.written.renderables)
    assert isinstance(renderables[0], Text)
    assert "NEUTRALIZED" in str(renderables[0])
    assert isinstance(renderables[1], Table)
    stub_lines = [r for r in renderables if isinstance(r, Text) and "NEUTRALIZATION STUB" in str(r)]
    assert len(stub_lines) == 1
    assert stub_lines[0].style == "bold yellow"


def test_render_mail_tolerates_a_host_whose_odoo_db_predates_is_neutralization_stub():
    """Same graceful-degradation convention as is_test_catcher: an older
    odoo-db's mail_servers rows simply lack this key, and must not crash."""
    from odoo_activity.panes.mail import render_mail

    audit = {
        "mail_servers": [
            {
                "sequence": 1,
                "name": "Primary",
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "starttls",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                # no "is_neutralization_stub" key -- an older odoo-db's shape
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type] -- must not raise KeyError


def test_render_mail_shows_record_missing_instead_of_dropping_the_row():
    """odoo-db's get_mail_addresses now emits a row with missing: True
    (xmlid unresolved or dangling) rather than silently dropping it --
    "not listed" must not read as "no admin problem"."""
    from rich.table import Table

    from odoo_activity.panes.mail import render_mail

    audit = {
        "addresses": [
            {"partner_id": None, "label": "Admin Email", "email": None, "is_default": False, "missing": True},
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type]

    tables = [r for r in list(body.written.renderables) if isinstance(r, Table)]
    addr_table = next(t for t in tables if t.title == "Relevant addresses")
    assert str(next(iter(addr_table.columns[0].cells))) == ""
    assert str(next(iter(addr_table.columns[2].cells))) == "(record missing)"


def test_render_mail_tolerates_a_host_whose_odoo_db_predates_missing_addresses():
    """Same graceful-degradation convention as is_test_catcher: an older
    odoo-db's addresses rows simply lack this key, and must not crash."""
    from odoo_activity.panes.mail import render_mail

    audit = {
        "addresses": [
            {"partner_id": 1, "label": "Company Email", "email": "a@example.com", "is_default": False},
            # no "missing" key -- an older odoo-db's shape
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type] -- must not raise KeyError


def test_render_mail_tolerates_a_host_whose_odoo_db_predates_is_test_catcher():
    """Caught live against a real host still running an older odoo-db: its
    mail_servers rows have no `is_test_catcher` key at all, and a hard
    `m["is_test_catcher"]` index crashed the whole tab (KeyError) instead of
    just not flagging anything -- same graceful-degradation convention the
    --all flag uses for an old host elsewhere in this app."""
    from odoo_activity.panes.mail import render_mail

    audit = {
        "mail_servers": [
            {
                "sequence": 1,
                "name": "mailhog",
                "smtp_host": "mailhog-acme18-staging",
                "smtp_port": 2025,
                "smtp_user": None,
                "smtp_pass": None,
                "smtp_encryption": "none",
                "smtp_authentication": "login",
                "from_filter": None,
                "active": True,
                # no "is_test_catcher" key -- an older odoo-db's shape
            }
        ],
    }

    class _FakeBody:
        def clear(self):
            pass

        def write(self, renderable):
            self.written = renderable

    body = _FakeBody()
    render_mail(body, audit)  # ty: ignore[invalid-argument-type] -- must not raise KeyError


def test_stringify_none_is_blank_not_the_literal_word():
    """Caught live against a real host: Mail's config_parameters rows for an
    unset key carry value: None, and str(None) rendered as the literal text
    "None" in the cell before this -- every field a row genuinely has but
    left unset should read blank instead."""
    assert probes.stringify(None) == ""
    assert probes.stringify(0) == "0"  # falsy but not None -- must stay visible
    assert probes.stringify(False) == "False"


def test_proc_cpu_ticks_many_batches_a_remote_host_into_one_call(monkeypatch):
    # regression: this used to be one ssh round trip per pid (proc_cpu_ticks
    # in a loop) -- slow enough with more than a couple of top that
    # the Top tab sat on "Loading top..." for real seconds.
    calls = []

    def fake_run(self, argv, **_):
        calls.append(argv)
        # pid "2" is gone -- cat's stderr is redirected, stdout for it is empty
        out = (
            "\x1e1\n1 (odoo) S 0 1 1 0 -1 0 0 0 0 0 111 222 0 0 20 0 1 0 1 0 0 0 0\n"
            "\x1e2\n"
            "\x1e3\n3 (postgres) S 0 1 1 0 -1 0 0 0 0 0 5 5 0 0 20 0 1 0 1 0 0 0 0\n"
        )
        return SimpleNamespace(stdout=out)

    monkeypatch.setattr(Host, "run", fake_run)
    host = Host(alias="x")

    result = probes.proc_cpu_ticks_many(["1", "2", "3"], host)

    assert result == {"1": 111 + 222, "2": None, "3": 5 + 5}
    assert len(calls) == 1  # one round trip, not three


def test_instance_action_routes_by_manager(monkeypatch):
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    assert probes.instance_action("demo.service", "restart") == ""
    probes.instance_action("openerp-odoo-staging", "stop", manager="supervisor")
    assert calls == [
        ["systemctl", "--user", "restart", "demo.service"],
        ["supervisorctl", "stop", "openerp-odoo-staging"],
    ]


def test_instance_action_odoosh_restarts_both_services(monkeypatch):
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    assert probes.instance_action("demo", "restart", manager="odoosh") == ""
    assert calls == [
        ["odoosh-restart", "http"],
        ["odoosh-restart", "cron"],
    ]
    assert probes.instance_action("demo", "start", manager="odoosh") != ""


def test_rebuild_instances_sorts_by_status_and_nests_dbs(monkeypatch):
    instances = [
        {"name": "c.service", "status": "stopped", "uptime": "-", "manager": "systemd"},
        {"name": "a.service", "status": "failed", "uptime": "-", "manager": "systemd"},
        {"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"},
    ]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(
        tui, "databases_of", lambda inst, *_: (["demo"], None) if inst["name"] == "b.service" else ([], None)
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            keys = [item.name for item in pilot.app.query_one("#instances", tui.ListView).children]
            # running first, its db nested right under it, then failed, then stopped
            assert keys == [
                "systemd:b.service",
                "systemd:b.service::db::demo",
                "systemd:a.service",
                "systemd:c.service",
            ]

    asyncio.run(go())


def test_instance_action_waits_for_confirmation(monkeypatch):
    # regression guard: s/r and the buttons must not act until the user
    # confirms — this is the whole point of ConfirmScreen
    calls = []
    monkeypatch.setattr(
        tui,
        "list_instances",
        lambda *_: [{"name": "a.service", "status": "running", "uptime": "-", "manager": "systemd"}],
    )
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda _inst, *_: ([], None))
    monkeypatch.setattr(
        tui, "instance_action", lambda name, action, manager, *_: calls.append((name, action, manager)) or ""
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 30)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("s")  # running -> toggles to stop, opens ConfirmScreen first
            await pilot.pause()
            assert calls == []
            assert isinstance(pilot.app.screen, tui.ConfirmScreen)

            await pilot.click("#confirm-yes")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == [("a.service", "stop", "systemd")]

    asyncio.run(go())


def test_instance_only_shortcuts_are_hidden_in_database_mode(monkeypatch):
    # regression guard: a db row resolves to its owning instance, so D/S have
    # an instance to act on there too -- mode is what must keep them hidden
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert pilot.app.check_action("dumpstacks", ()) is True
            assert pilot.app.check_action("copy_shell_command", ()) is True

            await pilot.press("down")  # onto the nested db row -> database mode
            await pilot.pause()
            assert pilot.app.check_action("dumpstacks", ()) is False
            assert pilot.app.check_action("copy_shell_command", ()) is False

            await pilot.press("up")
            await pilot.pause()
            assert pilot.app.check_action("dumpstacks", ()) is True

    asyncio.run(go())


def test_db_tab_search_and_show_all_gating(monkeypatch):
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: None)  # no real odoo-db call

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)

            # instance mode has no db table to widen
            assert pilot.app.check_action("toggle_show_all", ()) is False

            await pilot.press("down")  # database mode
            await pilot.pause()

            pane.select_tab_by_name("Modules")
            await pilot.pause()
            assert pilot.app.check_action("search", ()) is True
            assert pilot.app.check_action("toggle_show_all", ()) is True

            pane.select_tab_by_name("Locks")
            await pilot.pause()
            assert pilot.app.check_action("search", ()) is True  # every db table searches
            assert pilot.app.check_action("toggle_show_all", ()) is False

    asyncio.run(go())


def test_db_tab_retries_without_all_on_an_older_odoo_db(monkeypatch):
    # a host whose odoo-db predates `--all` on this command answers with a
    # usage error, not json -- the tab must still end up showing its rows
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    calls = []

    class _FakeProc:
        def __init__(self, out):
            self._out = out

        def communicate(self, timeout=None):
            return (self._out, "")

        def kill(self):
            pass

    def fake_start(command, db, port=None, host=None, *, include_sensitive_information=False, include_inactive=False):
        calls.append((command, include_inactive))
        if include_inactive:
            return _FakeProc("Error: No such option: --all\n")

        return _FakeProc('[{"name": "sale", "version": "16.0.1.0"}]')

    monkeypatch.setattr(detail_mod, "start_odoo_db", fake_start)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Modules")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert calls[-2:] == [("modules", True), ("modules", False)]
            assert pane._dbtab.rows == [{"name": "sale", "version": "16.0.1.0"}]
            # no `installed` key on those rows, so `A` just shows them all
            assert [i for i, _row in pane._visible_db_rows()] == [0]

            assert pane._dbtab.no_all is True
            notified = []
            monkeypatch.setattr(pilot.app, "notify", lambda msg, **_: notified.append(msg))
            pane.toggle_show_all()  # explains itself instead of silently doing nothing
            assert notified and "odoo-db" in notified[0]

    asyncio.run(go())


def test_db_tab_does_not_retry_on_an_unrelated_error(monkeypatch):
    """Only a "no such option" reply means this odoo-db predates `--all` --
    anything else (wrong db, no permission, ...) must surface as-is instead
    of being retried and doubling the wait before the user sees it."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    calls = []

    class _FakeProc:
        def __init__(self, out):
            self._out = out

        def communicate(self, timeout=None):
            return (self._out, "")

        def kill(self):
            pass

    def fake_start(command, db, port=None, host=None, *, include_sensitive_information=False, include_inactive=False):
        calls.append((command, include_inactive))
        return _FakeProc('database "demo" does not exist\n')

    monkeypatch.setattr(detail_mod, "start_odoo_db", fake_start)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Modules")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert calls == [("modules", True)]  # no second, plain-flag attempt
            assert pane._dbtab.rows == []
            assert pane._dbtab.no_all is False

    asyncio.run(go())


def test_visible_db_rows_filters_by_active_flag_and_query(monkeypatch):
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: None)  # no real odoo-db call

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Crons")
            await pilot.pause()

            pane._dbtab.rows = [
                {"name": "Mail: Fetchmail", "active": True},
                {"name": "Base: Auto-vacuum", "active": False},
                {"name": "Mail: Notify", "active": True},
            ]

            assert pane._visible_db_rows() == [(0, pane._dbtab.rows[0]), (2, pane._dbtab.rows[2])]

            pane.toggle_show_all()
            await pilot.pause()
            assert [i for i, _row in pane._visible_db_rows()] == [0, 1, 2]

            # indexes stay the ones into _dbtab.rows, so enter still opens the
            # right row's raw json
            pane._filters["Crons"] = "vacuum"
            assert [i for i, _row in pane._visible_db_rows()] == [1]

            pane._show_all = False
            assert pane._visible_db_rows() == []

    asyncio.run(go())


def test_selected_process_follows_the_filtered_table(monkeypatch):
    """`K`/`L` signal whatever selected_process() returns, so under a Top
    filter it must read the cursor's row *key*, not its position — those
    differ as soon as a row is filtered out."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: ([], None))

    def _row(pid: str, cmd: str) -> probes.ProcRow:
        return {"pid": pid, "ppid": "1", "user": "odoo", "mem": "0.1", "nice": "0", "cmd": cmd}

    # the Top tab's own periodic refresh (tick()) re-fetches on a real timer,
    # so it must return the same fixed rows every time -- anything live (the
    # real `ps`) would make the table's contents, and this test, a coin flip
    odoo_rows = [_row("101", "odoo-bin"), _row("103", "odoo-bin")]
    pg_rows = [_row("102", "postgres: demo")]
    monkeypatch.setattr(detail_mod, "instance_procs", lambda *_: (odoo_rows, pg_rows))
    monkeypatch.setattr(detail_mod, "proc_cpu_ticks_many", lambda *_: {})

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            assert pane.is_top_active()
            await pilot.app.workers.wait_for_complete()  # the Top tab's own initial fetch
            await pilot.pause()

            pane.open_search()
            await pilot.pause()
            await pilot.press(*"postgres")
            await pilot.press("enter")
            await pilot.pause()

            table = pilot.app.query_one("#actable", DataTable)
            assert table.row_count == 1
            table.move_cursor(row=0)
            selected = pane.selected_process()
            assert selected is not None
            assert selected["pid"] == "102"

    asyncio.run(go())


_MAIL_AUDIT = {
    "config_parameters": [{"key": "mail.catchall.domain", "value": "example.com", "explanation": ""}],
    "alias_domains": None,
    "addresses": [{"partner_id": 1, "label": "Company", "email": "info@example.com", "is_default": False}],
    "mail_servers": [],
    "modules": [],
}


def test_mail_tab_renders_via_the_log_body_and_shows_only_check_port_25_without_odooly(monkeypatch):
    """The Mail tab's fetch has to unwrap odoo-db's nested audit object and
    hand it to render_mail (see panes/mail.py), which writes it into the
    RichLog body rather than the generic table renderer -- the DataTable
    stays hidden, and `_dbtab.rows` stays empty so a leftover `/` search
    from a prior tab can't clobber it (see _show_datatable's early return).
    Check port 25 is a plain network probe, not an authenticated Odoo
    action, so it shows regardless of odooly; Send test mail only shows up
    once odooly can reach this database -- unmatched here (see
    test_odooly.py for the matched case)."""
    _params_setup(monkeypatch)  # instance/db plumbing only; overridden below
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeOdooDbProc(_MAIL_AUDIT))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Mail")
            await pilot.pause()
            await _settle(pilot)

            assert pane._dbtab.rows == []
            assert pilot.app.query_one("#acbody", detail_mod.RichLog).display is True
            assert pilot.app.query_one("#actable", DataTable).display is False
            assert pane.query_one("#acactions", detail_mod.Horizontal).display is True
            assert pilot.app.query_one("#check-port-25", detail_mod.Button)
            assert list(pane.query(f"#{detail_mod._SEND_TEST_MAIL_ACTION[0]}")) == []

    asyncio.run(go())


def test_check_port_25_button_runs_nc_with_a_scan_and_a_short_timeout(monkeypatch):
    """-z (scan, no data exchange) plus a short timeout: without them, a
    successful connection to an *open* port would leave `nc` sitting there
    waiting for input, and this action hung with no result ever shown --
    worse than the failure case it exists to diagnose."""
    _params_setup(monkeypatch)
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeOdooDbProc(_MAIL_AUDIT))

    calls = []

    def fake_run(_self, argv, input_text=None):
        # The running app also polls instance status (systemctl, ...) through
        # Host.run in the background -- only nc is this test's concern.
        if argv[0] == "nc":
            calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(Host, "run", fake_run)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Mail")
            await pilot.pause()
            await _settle(pilot)

            pane.query_one("#check-port-25", detail_mod.Button).press()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(go())

    assert calls == [["nc", "-z", "-w", "3", "localhost", "25"]]


def test_check_port_25_button_tolerates_nc_missing_from_path(monkeypatch):
    """Same graceful-degradation convention the rest of this app uses for a
    missing binary -- must not crash the worker."""
    _params_setup(monkeypatch)
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeOdooDbProc(_MAIL_AUDIT))

    def fake_run(_self, argv, input_text=None):
        if argv[0] == "nc":
            raise FileNotFoundError
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(Host, "run", fake_run)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Mail")
            await pilot.pause()
            await _settle(pilot)

            pane.query_one("#check-port-25", detail_mod.Button).press()
            await pilot.app.workers.wait_for_complete()  # must not raise
            await pilot.pause()

    asyncio.run(go())


def test_leaving_a_late_db_tab_for_an_instance_row(monkeypatch):
    # regression guard: database mode has more tabs than instance mode, so
    # navigating off one of the extra ones (Crons) back up to an instance row
    # left _tab pointing past the end of the instance tabs
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # onto the nested db row -> database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Crons")
            await pilot.pause()

            await pilot.press("up")  # back to the instance row
            await pilot.pause()

            assert pane._mode == "instance"
            assert pane._active_tab() in pane.TABS["instance"]

    asyncio.run(go())


_PARAMS_ROWS = [
    {"key": "base.url", "value": "http://example"},
    {"key": "database.secret", "value": "********"},
]


class _FakeOdooDbProc:
    """Stands in for the odoo-db subprocess.Popen `start_odoo_db` returns --
    `_fetch_db_tab` only ever calls .communicate()/.kill() on it. `payload`
    is a list of rows for every command but Mail, whose odoo-db output is
    one nested object instead (see panes.mail.render_mail)."""

    def __init__(self, payload: list[dict] | dict) -> None:
        self._rows = payload

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return json.dumps(self._rows), ""

    def kill(self) -> None:
        pass


def _params_setup(monkeypatch):
    """One instance with one db, `odoo-db params` stubbed to _PARAMS_ROWS."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "pg_target_of", lambda *_a, **_k: probes.PgTarget())

    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeOdooDbProc(_PARAMS_ROWS))


async def _settle(pilot) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def _open_params(pilot) -> "detail_mod.ActivityPane":
    await _settle(pilot)
    await pilot.press("down")  # onto the nested db row -> database mode
    await pilot.pause()
    pane = pilot.app.query_one(tui.ActivityPane)
    pane.select_tab_by_name("Params")
    await pilot.pause()
    await _settle(pilot)  # the fetch worker, then the call_after_refresh that populates the table
    return pane


def test_params_filter_keeps_original_row_index_as_key(monkeypatch):
    """Regression guard: _populate_datatable used to key rows by their
    position in the *filtered* list, so filtering down to one row and
    opening it could show the wrong row's raw json."""
    _params_setup(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            pane = await _open_params(pilot)

            pane.open_search()
            await pilot.pause()
            await pilot.press(*"secret")  # matches only _PARAMS_ROWS[1]
            await pilot.press("enter")
            await pilot.pause()

            table = pilot.app.query_one("#actable", DataTable)
            assert table.row_count == 1
            assert next(iter(table.rows)).value == "1"  # index into the unfiltered list, not 0

            shown = []
            orig_show_raw = pane._show_raw
            monkeypatch.setattr(pane, "_show_raw", lambda row: (shown.append(row), orig_show_raw(row)))

            await pilot.press("enter")  # open the surviving row's raw json
            await pilot.pause()

            assert shown == [_PARAMS_ROWS[1]]  # row 1, not row 0
            assert pane._showing_raw is True

    asyncio.run(go())


def test_on_resize_does_not_clobber_instance_log_with_leftover_params_filter(monkeypatch):
    """Regression guard for the on_resize hazard: _DbTab.abandon() (called by
    show_instance) drops .proc/.ident but not .rows, so a Params filter left
    over from an earlier database-mode visit stays around. Without the
    `self._mode == "database"` guard, on_resize would re-run
    _populate_datatable on that leftover state and, finding no match, call
    _log_body("(no match: ...)") -- clobbering the Logs/Config body on every
    terminal resize while sitting in instance mode."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane._mode = "instance"
            pane._tab = pane.TABS["instance"].index("Logs")

            # leftover from an earlier Params visit that no longer matches --
            # abandon() doesn't clear _dbtab.rows, and _filters["Params"] is
            # only popped by _load_db_tab, which instance mode never calls.
            pane._dbtab.rows = [{"key": "a", "value": "1"}]
            pane._filters["Params"] = "zzz"
            pane._showing_raw = False

            calls = []
            monkeypatch.setattr(pane, "_log_body", lambda text: calls.append(text))

            await pilot.resize_terminal(80, 30)
            await pilot.pause()

            assert calls == []  # on_resize must leave #acbody alone outside database mode

    asyncio.run(go())


def test_p_selects_params_in_database_mode_and_top_in_instance_mode(monkeypatch):
    """`p` is bound to both select_tab('Top') and select_tab('Params') --
    check_action's has_tab() gate (see tui.py) is what makes only one of them
    actually fire per mode, the same fallthrough Logs/Locks (`l`) and
    Config/Crons (`c`) already rely on."""
    _params_setup(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            app = pilot.app
            pane = app.query_one(tui.ActivityPane)

            assert app.check_action("select_tab", ("Top",)) is True
            assert app.check_action("select_tab", ("Params",)) is False

            await pilot.press("down")  # onto the nested db row -> database mode
            await pilot.pause()

            assert app.check_action("select_tab", ("Top",)) is False
            assert app.check_action("select_tab", ("Params",)) is True

            await pilot.press("p")
            await pilot.pause()
            assert pane._active_tab() == "Params"
            assert pane.has_search() is True

            pane.select_tab_by_name("Jobs")
            await pilot.pause()
            assert pane.has_search() is True


async def _settle(pilot):
    """Let the pane's workers finish.

    Not `workers.wait_for_complete()`: these flows deliberately supersede
    their own workers (a tab reload cancels the fetch in flight), and that
    call re-raises the cancellation as a failure.
    """
    for _ in range(20):
        await pilot.pause()


def _db_pilot(monkeypatch):
    """An app on a running instance with one db, ready to be walked into
    database mode with a single `down`."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "pg_target_of", lambda *_: probes.PgTarget())


def test_jobs_tab_groups_by_function_then_drills_into_one_group(monkeypatch):
    """The point of the tab: a state count says the queue is backed up, the
    group rows say which function it's backed up on, and opening one says
    which jobs — with the dates that show how long they've been sitting."""
    groups = [{"function": "res.partner.export", "state": "started", "jobs": 2, "waiting": "02:00:00"}]
    members = [
        {"uuid": "aaa", "state": "started", "date_created": "2026-08-11T09:00:00", "date_started": None},
        {"uuid": "bbb", "state": "started", "date_created": "2026-08-11T09:05:00", "date_started": None},
    ]
    asked = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: (groups, ""))
    monkeypatch.setattr(
        detail_mod, "jobs_in_group", lambda db, function, state, *_: asked.append((function, state)) or (members, "")
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")  # database mode
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)
            await pilot.pause()

            assert pane._dbtab.rows == groups
            assert pane._jobs_group is None

            # numbered so a long list can be counted by eye, and only data:
            # the tab's action lives in its own strip, not in the table
            table = pane.query_one("#actable", detail_mod.DataTable)
            assert [str(col.label) for col in table.columns.values()][:2] == ["#", "FUNCTION"]
            assert table.row_count == len(groups)
            assert str(table.get_row_at(0)[0]) == "1"

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()  # escape below only reaches the pane from inside it
            table.action_select_cursor()  # enter on the group row
            await _settle(pilot)
            await pilot.pause()

            assert asked == [("res.partner.export", "started")]
            assert pane._jobs_group == ("res.partner.export", "started")
            assert pane._dbtab.rows == members

            await pilot.press("escape")  # back out to the groups
            await _settle(pilot)
            await pilot.pause()

            assert pane._jobs_group is None
            assert pane._dbtab.rows == groups

    asyncio.run(go())


def test_requeue_is_offered_on_an_empty_queue_and_waits_for_confirmation(monkeypatch):
    """The action is the reason to open the tab when everything is stuck, so
    it is offered with no data above it — and, like every other mutating
    action, does nothing until confirmed. It sits in its own strip rather
    than in the table: it acts on the database, not on a row."""
    calls = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: ([], ""))
    monkeypatch.setattr(detail_mod, "requeue_jobs", lambda db, *_: calls.append(db) or (7, ""))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)
            await pilot.pause()

            bar = pane.query_one("#acactions", detail_mod.Horizontal)
            buttons = list(bar.query(detail_mod.Button))
            assert bar.display is True and len(buttons) == 1

            # reloading the tab must not rebuild the buttons under the cursor
            pane._render_actions()
            assert list(bar.query(detail_mod.Button)) == buttons

            buttons[0].press()
            await pilot.pause()

            assert calls == []
            assert isinstance(pilot.app.screen, tui.ConfirmScreen)

            await pilot.click("#confirm-yes")
            await _settle(pilot)
            await pilot.pause()

            assert calls == ["demo"]

            pilot.app.query_one("#instances", tui.ListView).focus()  # `up` moves rows, not the cursor
            await pilot.press("up")  # back to the instance row
            await _settle(pilot)
            assert pane.query_one("#acactions", detail_mod.Horizontal).display is False

    asyncio.run(go())


def test_kill_works_from_the_processes_tab_on_the_pid_under_the_cursor(monkeypatch):
    """`K` was Top-only, so the job runner — the process you actually want to
    restart on purpose — could be seen but not killed."""
    workers = [
        {"pid": "10", "ppid": "1", "user": "odoo", "mem": "1.0", "nice": "0", "cmd": "odoo-bin"},
        {"pid": "11", "ppid": "10", "user": "odoo", "mem": "1.0", "nice": "0", "cmd": "odoo-bin"},
    ]
    killed = []
    snapshots = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(
        detail_mod, "instance_workers", lambda *_: snapshots.append(1) or ("10", workers[: 2 - len(killed)])
    )
    monkeypatch.setattr(detail_mod, "jobrunner_pids", lambda *_: {"11"})
    monkeypatch.setattr(tui, "signal_process", lambda pid, sig, *_: killed.append((pid, sig)))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Processes")
            await _settle(pilot)
            await pilot.pause()

            tree = pane.query_one("#acprocesses", detail_mod.Tree)
            labels = [str(node.label) for node in tree.root.children]
            assert labels == ["Main (1)", "Job Runner (queue_job) (1)"]

            # cursor onto the job runner's leaf, then kill it
            runner_leaf = tree.root.children[1].children[0]
            tree.select_node(runner_leaf)
            tree.cursor_line = runner_leaf.line
            selected = pane.selected_process()
            assert selected is not None and selected["pid"] == "11"

            taken_before = len(snapshots)
            await pilot.press("K")
            await pilot.pause()
            assert isinstance(pilot.app.screen, tui.ConfirmScreen)

            await pilot.click("#confirm-yes")
            await _settle(pilot)
            await pilot.pause()

            assert killed == [("11", signal.SIGKILL)]

            # the tree is a snapshot, so killing has to re-take it: otherwise
            # the dead pid sits on screen until the tab is reopened
            assert len(snapshots) > taken_before
            assert [str(node.label) for node in tree.root.children] == ["Main (1)"]

    asyncio.run(go())


def test_requeue_clears_the_dates_that_say_a_job_is_running(monkeypatch):
    """A row left with its `date_started` reads as running for as long as it
    then sits in the queue — the Jobs tab's `running` column is
    `age(now(), date_started)`. queue_job's own `set_pending` clears them,
    and so does this."""
    sent = []
    monkeypatch.setattr(
        Host,
        "run",
        lambda _self, _cmd, input_text=None: (
            sent.append(input_text) or SimpleNamespace(returncode=0, stdout="UPDATE 3", stderr="")
        ),
    )

    assert probes.requeue_jobs("demo") == (3, "")
    (sql,) = sent
    assert "state = 'pending'" in sql
    assert "date_started = NULL" in sql and "date_enqueued = NULL" in sql and "worker_pid = NULL" in sql
    # only the two states a job can be stuck in, from the one constant
    assert sql.endswith("WHERE state IN ('started', 'enqueued')")


def test_escape_after_leaving_database_mode_does_not_reopen_jobs(monkeypatch):
    """`escape` backs out of a Jobs group. Arrowing up to the instance row
    leaves that group behind, and escape there used to reload the Jobs tab
    over whatever instance tab was showing — querying the last database and
    rendering job rows under a heading like "Top"."""
    groups = [{"function": "res.partner.export", "state": "started", "jobs": 1}]
    fetched = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: fetched.append("groups") or (groups, ""))
    monkeypatch.setattr(detail_mod, "jobs_in_group", lambda *_: fetched.append("members") or ([], ""))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()
            table.action_select_cursor()  # drill in
            await _settle(pilot)
            assert pane._jobs_group is not None

            pilot.app.query_one("#instances", tui.ListView).focus()  # `up` moves rows, not the cursor
            await pilot.press("up")  # instance mode
            await _settle(pilot)
            assert pane._mode == "instance"
            assert pane._jobs_group is None

            before = list(fetched)
            await pilot.press("escape")
            await _settle(pilot)

            assert pane._mode == "instance"
            assert fetched == before  # no database was queried

    asyncio.run(go())


def _ps_of(pids: dict[str, str], monkeypatch):
    """Stub the ps snapshot jobrunner_pids checks its candidates against."""
    by_pid = {
        pid: {"pid": pid, "ppid": "1", "user": "odoo", "mem": "0.1", "nice": "0", "cmd": cmd}
        for pid, cmd in pids.items()
    }
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, {}))


def test_jobrunner_named_by_application_name_on_odoo_16_and_up(monkeypatch):
    """From 16.0 (odoo/odoo f6c13d7) odoo names its connections `odoo-<pid>`,
    which names the pid outright — no port to trace."""
    calls = []

    def fake_run(_self, argv, input_text=None):
        calls.append(argv)
        rows = '[{"app": "odoo-4242", "port": 50482}]'
        return SimpleNamespace(returncode=0, stdout=rows, stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    _ps_of({"4242": "/venv/bin/python /venv/bin/odoo -d demo"}, monkeypatch)

    assert probes.jobrunner_pids() == {"4242"}
    assert all(argv[0] == "psql" for argv in calls)  # no ss/lsof needed


def test_jobrunner_traced_by_client_port_when_odoo_never_named_it(monkeypatch):
    """13.0-15.0 never set application_name, so every backend reports ''. The
    connection is still identifiable by its statements; the pid behind it has
    to come from the TCP endpoint instead."""
    ss_out = (
        'ESTAB 0 0 127.0.0.1:50482 127.0.0.1:5432 users:(("python3",pid=4242,fd=12))\n'
        "ESTAB 0 0 127.0.0.1:5432 127.0.0.1:50482\n"  # postgres's own side, no holder
        'ESTAB 0 0 127.0.0.1:60000 10.0.0.9:443 users:(("firefox",pid=9999,fd=7))\n'
    )

    def fake_run(_self, argv, input_text=None):
        if argv[0] == "psql":
            # 60000 is another host's client port on this shared postgres,
            # and -1 is a unix socket: neither is ours to resolve
            rows = '[{"app": "", "port": 50482}, {"app": "", "port": 60000}, {"app": "", "port": -1}]'
            return SimpleNamespace(returncode=0, stdout=rows, stderr="")

        assert argv[:2] == ["ss", "-tnpH"]
        return SimpleNamespace(returncode=0, stdout=ss_out, stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    monkeypatch.setattr(probes, "odoo_pid_for_port", lambda *_: None)  # no lsof either
    _ps_of({"4242": "/venv/bin/python /venv/bin/odoo -d demo", "9999": "firefox"}, monkeypatch)

    # 60000 is live here, but on a browser socket to :443 -- not postgres
    assert probes.jobrunner_pids() == {"4242"}


def test_jobrunner_accepts_a_worker_odoo_renamed_with_setproctitle(monkeypatch):
    """setproctitle overwrites argv wholesale (`odoo: WorkerJobRunner 4242`),
    leaving neither entry point nor flag for `_looks_like_odoo` to match. The
    pid gate has to read that title as odoo — it is odoo naming itself — or
    it throws away the very process it was asked about."""

    def fake_run(_self, argv, input_text=None):
        assert argv[0] == "psql"
        return SimpleNamespace(returncode=0, stdout='[{"app": "odoo-4242", "port": 50482}]', stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    _ps_of({"4242": "odoo: WorkerJobRunner 4242"}, monkeypatch)

    assert probes.jobrunner_pids() == {"4242"}


def test_jobrunner_drops_a_pid_that_is_not_an_odoo_process(monkeypatch):
    """The bare `SELECT 1` in the query is the runner's keepalive, but also
    pgbouncer's and every monitor's ping. Whatever the connection turns out
    to belong to has to actually be odoo."""

    def fake_run(_self, argv, input_text=None):
        if argv[0] == "psql":
            return SimpleNamespace(returncode=0, stdout='[{"app": "", "port": 50482}]', stderr="")

        return SimpleNamespace(
            returncode=0,
            stdout='ESTAB 0 0 127.0.0.1:50482 127.0.0.1:5432 users:(("pgbouncer",pid=77,fd=9))\n',
            stderr="",
        )

    monkeypatch.setattr(Host, "run", fake_run)
    _ps_of({"77": "/usr/sbin/pgbouncer /etc/pgbouncer.ini"}, monkeypatch)

    assert probes.jobrunner_pids() == set()


def test_client_ports_fall_back_to_lsof_and_tolerate_neither(monkeypatch):
    """`ss` isn't everywhere, and neither is `lsof`. A port nobody can
    account for is left out rather than guessed at."""
    monkeypatch.setattr(Host, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError))
    monkeypatch.setattr(probes, "odoo_pid_for_port", lambda port, _host=None: "7" if port == "50482" else None)

    assert probes._pids_by_client_port(["50482", "60000"]) == {"50482": "7"}

    monkeypatch.setattr(probes, "odoo_pid_for_port", lambda *_: None)
    assert probes._pids_by_client_port(["50482"]) == {}


def test_a_jobs_reload_keeps_its_action_strip_and_empties_its_rows(monkeypatch):
    """Two things a reload used to get wrong: it tore the strip down and
    rebuilt it (the blink `_render_actions` exists to avoid, and a race with
    the un-awaited removal), and an empty result left the previous depth's
    rows in the cache, which a resize or a search would then repaint inside
    the drilled-in view."""
    groups = [{"function": "res.partner.export", "state": "started", "jobs": 2}]
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: (groups, ""))
    monkeypatch.setattr(detail_mod, "jobs_in_group", lambda *_: ([], ""))  # the group emptied meanwhile

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)

            bar = pane.query_one("#acactions", detail_mod.Horizontal)
            button = next(iter(bar.query(detail_mod.Button)))

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()
            table.action_select_cursor()  # drill in
            await _settle(pilot)

            assert list(bar.query(detail_mod.Button)) == [button]  # same widget, not a rebuild
            assert pane._dbtab.rows == []  # not the group rows the tab was showing

    asyncio.run(go())


def test_refresh_stays_in_the_group_it_was_refreshing(monkeypatch):
    """`R` re-fetches what is on screen. Drilled into a group, that is the
    group — backing out to the list is what `escape` is for."""
    fetched = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(
        detail_mod, "job_groups", lambda *_: fetched.append("groups") or ([{"function": "a.b", "state": "done"}], "")
    )
    monkeypatch.setattr(detail_mod, "jobs_in_group", lambda *_: fetched.append("members") or ([{"uuid": "x"}], ""))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()
            table.action_select_cursor()
            await _settle(pilot)
            assert pane._jobs_group is not None

            fetched.clear()
            pane.refresh_active()
            await _settle(pilot)

            assert fetched == ["members"]  # not "groups"
            assert pane._jobs_group is not None

    asyncio.run(go())


def _nav_pilot(monkeypatch):
    """One running instance with one db nested under it — the shape that
    makes the walk interesting, since the instance row is not the last."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))


def test_enter_opens_the_tabs_of_the_row_it_is_pressed_on(monkeypatch):
    """A row's own tabs have to be reachable from that row. `down` can't be
    the way in: an instance with databases nested under it is never the last
    row, so `down` there belongs to the row below — which is a database, and
    carries the other mode's tabs."""
    _nav_pilot(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            strip = pane.query_one("#actabs", detail_mod.TabStrip)

            # on the instance row (not the last one) -> its own tabs
            await pilot.press("enter")
            await pilot.pause()
            assert strip.has_focus and pane._mode == "instance"

            # and back down the list for the database row's tabs
            await pilot.press("up")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert strip.has_focus and pane._mode == "database"

    asyncio.run(go())


def test_arrows_walk_the_three_zones_and_the_tabs(monkeypatch):
    """list -> strip -> body downwards, body -> strip -> list back up, and
    left/right between tabs while the strip holds focus."""
    _nav_pilot(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            strip = pane.query_one("#actabs", detail_mod.TabStrip)
            instances = pilot.app.query_one("#instances", tui.InstanceList)

            await pilot.press("enter")  # into the strip
            await pilot.pause()
            first = pane._active_tab()

            await pilot.press("right")
            await pilot.pause()
            assert pane._active_tab() != first
            await pilot.press("left")
            await pilot.pause()
            assert pane._active_tab() == first

            await pilot.press("down")  # into the tab body
            await pilot.pause()
            assert not strip.has_focus
            body = pilot.app.focused
            assert body is not None and pane in body.ancestors

            await pilot.press("up")  # at the body's top edge -> back to the strip
            await pilot.pause()
            assert strip.has_focus

            await pilot.press("up")  # and up out of the pane again
            await pilot.pause()
            assert instances.has_focus

    asyncio.run(go())


def test_down_off_the_last_row_reaches_the_strip(monkeypatch):
    """The one row with nothing below it: `down` has nowhere else to go."""
    _nav_pilot(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            await pilot.press("down")  # onto the db row, the last one
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            assert pane.query_one("#actabs", detail_mod.TabStrip).has_focus

    asyncio.run(go())


def test_focus_follows_the_body_a_late_fetch_swaps_in(monkeypatch):
    """Every async tab shows the "Loading …" log first and reveals its real
    body a moment later. A user who arrowed into the body in between was
    holding the log, and the reveal threw focus back out of the pane — so the
    documented way in silently failed on the slow (remote) path.

    Driven through `_use` rather than keys: Top re-renders on a timer, which
    would swap the body from under the assertions on a slow enough machine.
    """
    _nav_pilot(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Stacks")  # no refresh timer of its own
            await pilot.pause()

            pane._log_body("Loading stacks…")  # what a tab shows while fetching
            log = pane.query_one("#acbody", detail_mod.BodyLog)
            pilot.app.set_focus(log)
            await pilot.pause()
            assert log.has_focus

            pane._use("stacks")  # what the fetch does when its result lands
            await pilot.pause()

            assert pane.query_one("#acstacks", detail_mod.BodyTree).has_focus


def test_the_strip_does_not_hand_focus_to_a_maximized_away_list(monkeypatch):
    """`f` maximizes the pane, which leaves the instances list with no region
    on screen. Focusing it anyway gave the user a widget they cannot see:
    arrows moved a hidden highlight, and `s`/`r` acted on that hidden row."""
    _nav_pilot(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            strip = pane.query_one("#actabs", detail_mod.TabStrip)

            await pilot.press("enter")
            await pilot.press("f")  # maximize the pane
            await pilot.pause()
            assert pilot.app.screen.maximized is not None

            await pilot.press("up")
            await pilot.pause()
            assert strip.has_focus  # stayed put; `f` is what leaves

            await pilot.press("f")  # and once minimized it works again
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert pilot.app.query_one("#instances", tui.InstanceList).has_focus

    asyncio.run(go())


def test_down_waits_for_the_databases_still_being_fetched(monkeypatch):
    """The last row is only last until an instance's dbs land — 3-4 ssh round
    trips away. Hopping to the strip anyway skipped the rows about to appear,
    making one keypress mean two things depending on fetch latency."""
    _nav_pilot(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            app = cast("tui.OdooActivity", pilot.app)
            pane = app.query_one(tui.ActivityPane)
            app._db_cache.clear()  # as if the fetch were still in flight

            await pilot.press("down")
            await pilot.pause()
            assert not pane.query_one("#actabs", detail_mod.TabStrip).has_focus


def test_a_kill_on_a_container_instance_is_sent_inside_it(monkeypatch):
    """The pid on screen came out of the container's pid namespace, where
    the master is 1 -- signalling `1` on the box would be an error at best
    and init at worst, so the signal has to go back in through the same
    door the pid came out of."""
    instances = [
        {
            "name": "acme",
            "status": "running",
            "uptime": "0:01:00",
            "manager": "docker",
            "container": "acme-odoo-1",
            "workdir": "/srv/acme",
        }
    ]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(tui, "databases_of", lambda *_: ([], None))
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(
        detail_mod,
        "instance_procs",
        lambda *_: ([{"pid": "1", "ppid": "0", "user": "odoo", "mem": "1.0", "nice": "0", "cmd": "odoo"}], []),
    )
    monkeypatch.setattr(detail_mod, "proc_cpu_ticks_many", lambda *_: {})
    signalled: list[tuple[str, int, object]] = []
    monkeypatch.setattr(tui, "signal_process", lambda pid, sig, host: signalled.append((pid, sig, host)))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Top")
            await _settle(pilot)

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.move_cursor(row=0)
            await pilot.press("K")
            await pilot.pause()
            await pilot.click("#confirm-yes")
            await _settle(pilot)

            assert signalled == [("1", signal.SIGKILL, Host().in_container("acme-odoo-1"))]

    asyncio.run(go())


def test_the_logs_tab_streams_docker_logs_for_a_container_instance(monkeypatch):
    """A container has no logfile at all, so the Logs tab can't key off one:
    it takes a snapshot and follows a stream instead, and "no path" stops
    meaning "(no logfile configured)"."""
    instances = [
        {
            "name": "acme",
            "status": "running",
            "uptime": "0:01:00",
            "manager": "docker",
            "container": "acme-odoo-1",
            "workdir": "/srv/acme",
        }
    ]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(tui, "databases_of", lambda *_: ([], None))
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(detail_mod, "logfile_of", lambda *_: None)
    monkeypatch.setattr(detail_mod, "log_snapshot", lambda *_a, **_k: "INFO devel odoo: ready\n")
    followed: list[object] = []
    monkeypatch.setattr(detail_mod, "log_stream", lambda inst, host: followed.append(inst["container"]) or None)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Logs")
            await _settle(pilot)

            body = pane.query_one("#acbody", detail_mod.RichLog)
            assert [line.text for line in body.lines if line.text] == ["INFO devel odoo: ready"]
            assert followed == ["acme-odoo-1"]

    asyncio.run(go())


def test_an_empty_docker_db_list_is_retried_by_the_poll(monkeypatch):
    """First fetch can land while the `db` container is still `health:
    starting`: postgres is unreachable, the list degrades to `[]`, and that
    empty result used to stay cached for the rest of the session."""
    instances = [
        {
            "name": "acme",
            "status": "running",
            "uptime": "0:01:00",
            "manager": "docker",
            "container": "acme-odoo-1",
            "workdir": "/srv/acme",
        },
        {
            "name": "idle",
            "status": "stopped",
            "uptime": "-",
            "manager": "docker",
            "container": "idle-odoo-1",
            "workdir": "/srv/idle",
        },
    ]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "instance_status", lambda *_: "running")
    fetches: list[str] = []

    def fake_databases_of(inst, *_):
        fetches.append(inst["name"])
        return (["devel"], "5432") if len(fetches) > 1 else ([], None)

    monkeypatch.setattr(tui, "databases_of", fake_databases_of)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            app = cast("tui.OdooActivity", pilot.app)
            assert app._db_cache["docker:acme"] == ([], None)
            # a stopped project's empty list is the right answer, not a race
            app._db_cache["docker:idle"] = ([], None)

            app.poll_instances()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert app._db_cache["docker:acme"] == (["devel"], "5432")
            assert fetches == ["acme", "acme"]
            keys = [item.name for item in app.query_one("#instances", tui.ListView).children]
            assert keys == ["docker:acme", "docker:acme::db::devel", "docker:idle"]

    asyncio.run(go())


def test_db_rows_carry_their_neutralization_status(monkeypatch):
    """The whole point of the tag: a live database is called out in red, a
    confirmed-neutralized one in green, one whose signals disagree (flag set
    but a cron still live) in yellow, and a db postgres could not answer for
    carries no tag at all rather than a guess."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["staging", "reheated", "prod", "unknown"], None))
    monkeypatch.setattr(tui, "pg_target_of", lambda *_: probes.PgTarget())
    monkeypatch.setattr(
        tui,
        "neutralization_of",
        lambda *_: {
            "staging": {"state": probes.NEUTRALIZED},
            "reheated": {"state": probes.PARTIAL, "extras": {"iap_account": 1}},
            "prod": {"state": probes.NOT_NEUTRALIZED},
        },
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(120, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            labels = {
                item.name: str(next(iter(item.query(tui.Label))).content)
                for item in pilot.app.query_one("#instances", tui.ListView).children
            }
            assert "[green]NEUTRALIZED[/]" in str(labels["systemd:b.service::db::staging"])
            assert "[yellow]PARTIALLY NEUTRALIZED[/]" in str(labels["systemd:b.service::db::reheated"])
            assert "[red]NOT NEUTRALIZED[/]" in str(labels["systemd:b.service::db::prod"])
            assert "NEUTRALIZED" not in str(labels["systemd:b.service::db::unknown"])

    asyncio.run(go())
