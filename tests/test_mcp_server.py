import inspect
import re

import pytest

from odoo_activity import mcp_server


def test_host_filter_fullmatch_not_prefix(monkeypatch):
    monkeypatch.setattr(mcp_server, "_host_filter", re.compile("prod"))
    assert mcp_server._allowed("prod") is True
    assert mcp_server._allowed("prod-evil.example.com") is False
    assert mcp_server._allowed(None) is True  # local always allowed


def test_ssh_config_aliases_skips_wildcards_and_negation(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host demo\nHost *.internal\nHost !skip real\n")
    assert mcp_server._ssh_config_aliases(cfg) == ["demo", "real"]


def test_db_query_has_no_include_sensitive_information_argument():
    """No tool call can ask for plaintext -- the parameter must not exist on
    db_query's callable signature (what FastMCP turns into the tool schema
    an agent sees), only on the launch-time CLI flag."""
    assert "include_sensitive_information" not in inspect.signature(mcp_server.db_query).parameters


def test_db_query_honors_launch_time_flag_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **kw: captured.update(kw) or None)

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", False)
    mcp_server.db_query("demo", "params")
    assert captured["include_sensitive_information"] is False

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", True)
    mcp_server.db_query("demo", "params")
    assert captured["include_sensitive_information"] is True


def test_mail_audit_has_no_include_sensitive_information_argument():
    assert "include_sensitive_information" not in inspect.signature(mcp_server.mail_audit).parameters


def test_mail_audit_honors_launch_time_flag_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **kw: captured.update(kw) or None)

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", False)
    mcp_server.mail_audit("demo")
    assert captured["include_sensitive_information"] is False

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", True)
    mcp_server.mail_audit("demo")
    assert captured["include_sensitive_information"] is True


def test_mail_audit_unwraps_the_single_nested_object(monkeypatch):
    # odoo-db's `mail` answers one nested object, not a flat row list -- kept
    # out of db_query's scoped command set for exactly this reason. The
    # subprocess layer still wraps it in a one-element list (every odoo-db
    # command does, see parse_odoo_db_output), so this must unwrap it rather
    # than handing the caller a single-item list like db_query would.
    class _FakeProc:
        def communicate(self, timeout=None):
            return '{"is_neutralized": false, "mail_servers": []}', ""

    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: _FakeProc())

    result = mcp_server.mail_audit("demo")
    assert result == {"is_neutralized": False, "mail_servers": []}


def test_mail_audit_returns_empty_dict_for_an_empty_row_list(monkeypatch):
    class _FakeProc:
        def communicate(self, timeout=None):
            return "[]", ""

    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: _FakeProc())

    assert mcp_server.mail_audit("demo") == {}


def test_mail_audit_returns_raw_text_for_non_json_output(monkeypatch):
    class _FakeProc:
        def communicate(self, timeout=None):
            return "", "database does not exist"

    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: _FakeProc())

    assert mcp_server.mail_audit("demo") == "database does not exist"


def test_mail_audit_reports_when_odoo_db_is_not_on_path(monkeypatch):
    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: None)

    assert mcp_server.mail_audit("demo") == "(odoo-db not found on PATH)"


def test_odooly_tools_refuse_without_the_launch_time_flag(monkeypatch):
    """No tool call can turn odooly support on itself -- only the
    --enable-odooly CLI flag can, mirroring _include_sensitive_information."""
    monkeypatch.setattr(mcp_server, "_enable_odooly", False)

    with pytest.raises(ValueError, match="--enable-odooly"):
        mcp_server.list_odooly_envs()
    with pytest.raises(ValueError, match="--enable-odooly"):
        mcp_server.instance_odooly_env("openerp-acme18-integration", "acme18_int")
    with pytest.raises(ValueError, match="--enable-odooly"):
        mcp_server.odooly_run_script("restore_app_icons", "acme18-int")


def test_odooly_tools_delegate_to_probes_once_enabled(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enable_odooly", True)
    monkeypatch.setattr(mcp_server.probes, "read_odooly_envs", lambda: [{"name": "acme18-int", "db": "acme18_int"}])

    assert mcp_server.list_odooly_envs() == ["acme18-int"]
    assert mcp_server.instance_odooly_env("openerp-acme18-integration", "acme18_int") == "acme18-int"
    assert mcp_server.instance_odooly_env("openerp-acme18-integration", "nope") is None

    captured = {}
    monkeypatch.setattr(
        mcp_server.probes,
        "run_odooly_script",
        lambda script, env, *extra: captured.update(script=script, env=env, extra=extra) or "ok",
    )
    assert mcp_server.odooly_run_script("create_test_job", "acme18-int") == "ok"
    assert captured == {"script": "create_test_job", "env": "acme18-int", "extra": ()}

    assert mcp_server.odooly_run_script("send_test_mail", "acme18-int", to="me@example.com") == "ok"
    assert captured == {"script": "send_test_mail", "env": "acme18-int", "extra": ("--to", "me@example.com")}


def test_send_test_mail_needs_a_recipient(monkeypatch):
    """The script's own `--to` is mandatory -- catch a missing recipient here
    rather than let it fail one subprocess hop away from a clear error."""
    monkeypatch.setattr(mcp_server, "_enable_odooly", True)

    with pytest.raises(ValueError, match="`to`"):
        mcp_server.odooly_run_script("send_test_mail", "acme18-int")


def test_mcp_tools_do_not_crash():
    """Smoke test: each tool runs end-to-end (real probes, no mocking) and
    returns the expected shape — a regression guard against future changes
    to probes.py breaking the MCP wrappers, not a check of probe values."""
    instances = mcp_server.list_instances()
    assert isinstance(instances, list)

    if instances:
        name = instances[0]["name"]
        assert mcp_server.get_instance(name) is not None
        assert isinstance(mcp_server.list_top(name), list)
        assert isinstance(mcp_server.instance_databases(name), dict)
        assert isinstance(mcp_server.instance_top(name), dict)
        assert isinstance(mcp_server.instance_config(name), str)
        assert isinstance(mcp_server.instance_log_tail(name), str)

        dbs = mcp_server.instance_databases(name)
        if dbs and dbs.get("databases"):
            assert isinstance(mcp_server.mail_audit(dbs["databases"][0]), (dict, str))

    stats = mcp_server.host_stats()
    assert stats is None or isinstance(stats, dict)

    assert mcp_server.get_instance("__no_such_instance__") is None
    assert mcp_server.list_top("__no_such_instance__") == []
    # not exercised against a real instance: SIGQUIT is a live signal, not a
    # read — only the not-found path is safe to smoke-test unconditionally.
    assert mcp_server.instance_dump_stacks("__no_such_instance__") == {"error": "(no such instance)", "workers": []}


def test_instance_databases_reports_neutralization_per_database(monkeypatch):
    """The agent gets the same three states the TUI shows a human — and a db
    psql couldn't answer for is simply absent from the map (unknown), never
    reported as a live database."""
    inst = {"name": "b.service", "status": "running", "uptime": "-", "manager": "systemd"}
    mcp_server._discovered.clear()  # the discovery cache outlives a single test
    monkeypatch.setattr(mcp_server.probes, "list_instances", lambda *_a, **_k: [inst])
    monkeypatch.setattr(mcp_server.probes, "databases_of", lambda *_a, **_k: (["staging", "prod"], "5432"))
    monkeypatch.setattr(mcp_server.probes, "pg_target_of", lambda *_a, **_k: mcp_server.probes.PgTarget())
    report = {"state": mcp_server.probes.NEUTRALIZED, "extras": {}, "checked": ["iap_account"]}
    monkeypatch.setattr(mcp_server.probes, "neutralization_of", lambda *_a, **_k: {"staging": report})

    assert mcp_server.instance_databases("b.service") == {
        "databases": ["staging", "prod"],
        "db_port": "5432",
        # the raw signals ride along with the state: `extras` is what makes a
        # partial actionable, `checked` is what proves a surface ran at all
        "neutralized": {"staging": report},
    }
