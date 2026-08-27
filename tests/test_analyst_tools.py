"""The analyst's live warehouse queries.

Three separate concerns, and the first is the one that matters most: a model
writes the SQL, so the connection it writes against has to be incapable of
doing damage however the SQL is phrased.
"""
from __future__ import annotations

import datetime

import pytest

duckdb = pytest.importorskip("duckdb")

from btc_dashboard import analyst, providers                      # noqa: E402
from btc_dashboard.config import Config                           # noqa: E402
from btc_dashboard.sources import Tool, warehouse                 # noqa: E402


@pytest.fixture
def db(tmp_path):
    """A small warehouse shaped like the real one."""
    path = tmp_path / "market.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE btc(date DATE PRIMARY KEY, close DOUBLE, volume DOUBLE)")
    con.execute("CREATE TABLE onchain(date DATE PRIMARY KEY, fee_subsidy DOUBLE, "
                "hash_rate_ehs DOUBLE)")
    start = datetime.date(2024, 1, 1)
    for i in range(400):
        d = start + datetime.timedelta(days=i)
        con.execute("INSERT INTO btc VALUES (?, ?, ?)", [d, 40000.0 + i * 10, 1e9])
        con.execute("INSERT INTO onchain VALUES (?, ?, ?)", [d, 0.5 + (i % 7) * 0.1, 600.0])
    con.close()
    return path


class TestTheQueryConnectionCannotDoDamage:
    """A model composes these statements. Every guard here assumes it will
    eventually compose the worst one."""

    def test_a_plain_select_works(self, db):
        r = warehouse.run_query(db, "SELECT count(*) AS n FROM btc")
        assert r.rows == [(400,)] and r.columns == ["n"]

    @pytest.mark.parametrize("statement", [
        "DELETE FROM btc",
        "INSERT INTO btc VALUES ('2030-01-01', 1, 1)",
        "UPDATE btc SET close = 0",
        "DROP TABLE btc",
        "CREATE TABLE evil(a INT)",
        "PRAGMA database_list",
        "SET enable_external_access=true",
        "ATTACH 'other.duckdb'",
    ])
    def test_anything_that_is_not_a_single_read_is_refused(self, db, statement):
        """Not pattern-matched: wrapping in `SELECT * FROM (...)` makes DuckDB's
        own parser the authority on what counts as one read, so no blocklist has
        to anticipate the next statement type."""
        with pytest.raises(warehouse.QueryError):
            warehouse.run_query(db, statement)

    def test_a_second_statement_cannot_ride_along(self, db):
        with pytest.raises(warehouse.QueryError):
            warehouse.run_query(db, "SELECT 1; DROP TABLE btc")

    def test_a_trailing_semicolon_is_fine(self, db):
        """Models write them. Rejecting one would be a confusing failure."""
        assert warehouse.run_query(db, "SELECT 1 AS a;").rows == [(1,)]

    def test_the_filesystem_is_unreachable(self, db, tmp_path):
        """read_only stops writes to the database; it does not stop DuckDB
        reading the rest of the disk. This is the guard that does."""
        secret = tmp_path / "secret.csv"
        secret.write_text("a\n1\n")
        with pytest.raises(warehouse.QueryError, match="Permission|disabled"):
            warehouse.run_query(db, f"SELECT * FROM read_csv('{secret}')")

    def test_a_file_cannot_be_written_out(self, db, tmp_path):
        out = tmp_path / "exfiltrated.csv"
        with pytest.raises(warehouse.QueryError):
            warehouse.run_query(db, f"COPY (SELECT * FROM btc) TO '{out}'")
        assert not out.exists()

    def test_extensions_cannot_be_installed(self, db):
        """httpfs would turn a query tool into an outbound network client."""
        with pytest.raises(warehouse.QueryError):
            warehouse.run_query(db, "INSTALL httpfs")

    def test_the_database_is_intact_afterwards(self, db):
        for bad in ("DELETE FROM btc", "DROP TABLE btc", "UPDATE btc SET close=0"):
            with pytest.raises(warehouse.QueryError):
                warehouse.run_query(db, bad)
        r = warehouse.run_query(db, "SELECT count(*) FROM btc")
        assert r.rows == [(400,)], "a refused write must not have half-applied"

    def test_an_empty_query_says_so(self, db):
        with pytest.raises(warehouse.QueryError, match="empty"):
            warehouse.run_query(db, "   ")


class TestResultsAreBounded:
    def test_rows_are_capped(self, db):
        r = warehouse.run_query(db, "SELECT date FROM btc", limit=10)
        assert len(r.rows) == 10 and r.truncated

    def test_a_result_exactly_at_the_cap_is_not_called_truncated(self, db):
        """The query asks for one more than the cap so the two cases separate;
        a full page wrongly marked truncated teaches the model to distrust it."""
        r = warehouse.run_query(db, "SELECT date FROM btc LIMIT 10", limit=10)
        assert len(r.rows) == 10 and not r.truncated

    def test_truncation_is_stated_in_the_text(self, db):
        text = warehouse.format_rows(warehouse.run_query(db, "SELECT date FROM btc"))
        assert "cap" in text, "a model shown part of a series must be told so"

    def test_a_long_result_states_how_much_is_shown(self, db):
        text = warehouse.format_rows(
            warehouse.run_query(db, "SELECT date FROM btc", limit=100), preview=5)
        assert "first 5 shown" in text and "100 rows" in text

    def test_no_rows_is_not_an_error(self, db):
        r = warehouse.run_query(db, "SELECT date FROM btc WHERE close < 0")
        assert r.rows == [] and warehouse.format_rows(r) == "0 rows."

    def test_nulls_are_not_rendered_as_empty(self, db):
        """An empty cell reads as zero. NULL is not zero — the rule the whole
        project turns on."""
        text = warehouse.format_rows(warehouse.run_query(db, "SELECT NULL AS x"))
        assert "NULL" in text

    def test_a_runaway_query_is_interrupted(self, db):
        """DuckDB has no statement timeout, so one is imposed from a timer."""
        with pytest.raises(warehouse.QueryError):
            warehouse.run_query(
                db, "SELECT count(*) FROM range(100000000000) a, range(10000) b",
                timeout=1)

    def test_the_warehouse_still_works_after_an_interrupt(self, db):
        with pytest.raises(warehouse.QueryError):
            warehouse.run_query(
                db, "SELECT count(*) FROM range(100000000000) a, range(10000) b",
                timeout=1)
        assert warehouse.run_query(db, "SELECT 1 AS a").rows == [(1,)]


class TestTheSchemaComesFromTheDatabase:
    def test_tables_and_columns_are_listed(self, db):
        con = warehouse._connect_sandboxed(db)
        try:
            text = warehouse.schema_text(con)
        finally:
            con.close()
        assert "btc(" in text and "onchain(" in text
        assert "close double" in text and "fee_subsidy double" in text

    def test_coverage_is_stated(self, db):
        """Whether a question is answerable is usually a coverage question."""
        con = warehouse._connect_sandboxed(db)
        try:
            text = warehouse.schema_text(con)
        finally:
            con.close()
        assert "2024-01-01" in text and "400 rows" in text

    def test_a_new_column_appears_without_a_code_change(self, db):
        """The ingester owns this schema and adds columns without asking. A
        hardcoded list would have the model writing SQL against a stale one."""
        con = duckdb.connect(str(db))
        con.execute("ALTER TABLE btc ADD COLUMN realised_cap DOUBLE")
        con.close()
        con = warehouse._connect_sandboxed(db)
        try:
            assert "realised_cap" in warehouse.schema_text(con)
        finally:
            con.close()


class TestTheToolIsOfferedOnlyWhenItWorks:
    def test_offered_when_the_warehouse_exists(self, db):
        tools = warehouse.analyst_tools(Config.from_env(db_path=db))
        assert [t.name for t in tools] == [warehouse.TOOL_NAME]

    def test_not_offered_when_there_is_no_warehouse(self, tmp_path):
        """The Mac case, and a Pi whose database has moved."""
        cfg = Config.from_env(db_path=tmp_path / "absent.duckdb")
        assert warehouse.analyst_tools(cfg) == []

    def test_the_schema_travels_with_the_tool(self, db):
        tool = warehouse.analyst_tools(Config.from_env(db_path=db))[0]
        assert "btc(" in tool.description and "onchain(" in tool.description

    def test_the_measurement_caveats_travel_with_it(self, db):
        """The model is about to compute figures the sources normally qualify
        for it. The qualifiers have to reach it or they are lost."""
        text = warehouse.analyst_tools(Config.from_env(db_path=db))[0].description
        assert "365" in text and "252" in text, "volatility annualisation"
        assert "weekend" in text or "weekly cycle" in text, "the fee_subsidy cycle"
        assert "complete UTC day" in text.replace("COMPLETE", "complete")

    def test_a_failing_query_comes_back_as_text_not_an_exception(self, db):
        """A bad query is a turn the model can recover from, if it is told."""
        tool = warehouse.analyst_tools(Config.from_env(db_path=db))[0]
        out = tool.run(sql="SELECT nope FROM btc")
        assert out.startswith("QUERY FAILED") and "nope" in out


class TestTheAnalystSaysWhatItCanReach:
    def _snap(self):
        return {"schema_version": 1, "generated_at": "2026-08-27T00:00:00+00:00",
                "asset": "btc", "sources": {}}

    def test_tools_are_gathered_from_the_sources(self, db):
        cfg = Config.from_env(db_path=db)
        assert [t.name for t in analyst.gather_tools(cfg)] == [warehouse.TOOL_NAME]

    def test_a_source_whose_hook_raises_costs_only_its_own_tool(self, db, monkeypatch):
        """Same fail-soft rule as collection: --ask must not die because a
        database moved."""
        monkeypatch.setattr(warehouse, "analyst_tools",
                            lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
        assert analyst.gather_tools(Config.from_env(db_path=db)) == []

    def test_the_prompt_says_so_when_there_are_tools(self, db, monkeypatch):
        seen = {}
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        monkeypatch.setattr(providers, "complete",
                            lambda *a, **k: seen.update(system=a[2], tools=k.get("tools"))
                            or providers.Completion("ok", "m"))
        analyst.ask(self._snap(), "q", Config.from_env(
            provider="deepseek", model="deepseek-chat", db_path=db))
        assert seen["tools"], "the tool should be offered"
        assert "tools that read live data" in seen["system"]

    def test_the_prompt_says_so_when_there_are_none(self, tmp_path, monkeypatch):
        """A model that believes it can query history and cannot will answer
        from the snapshot while sounding like it checked."""
        seen = {}
        monkeypatch.setattr(providers, "complete",
                            lambda *a, **k: seen.update(system=a[2], tools=k.get("tools"))
                            or providers.Completion("ok", "m"))
        analyst.ask(self._snap(), "q", Config.from_env(
            provider="deepseek", model="deepseek-chat",
            db_path=tmp_path / "absent.duckdb"))
        assert not seen["tools"]
        assert "No live query tool is available" in seen["system"]
        assert "tools that read live data" not in seen["system"]

    def test_no_tools_can_be_forced_off(self, db, monkeypatch):
        seen = {}
        monkeypatch.setattr(providers, "complete",
                            lambda *a, **k: seen.update(tools=k.get("tools"))
                            or providers.Completion("ok", "m"))
        analyst.ask(self._snap(), "q", Config.from_env(
            provider="deepseek", model="deepseek-chat", db_path=db), use_tools=False)
        assert not seen["tools"]

    def test_the_calls_reach_the_result(self, db, monkeypatch):
        call = providers.ToolCall("query_warehouse", {"sql": "SELECT 1"}, "1")
        monkeypatch.setattr(providers, "complete",
                            lambda *a, **k: providers.Completion(
                                "ok", "m", 1, 2, (call,)))
        r = analyst.ask(self._snap(), "q", Config.from_env(
            provider="deepseek", model="deepseek-chat", db_path=db))
        assert r.tool_calls == (call,)


class TestTheDispatcherNeverRaises:
    """An exception here would discard every round already paid for."""

    def test_an_unknown_tool_is_reported_to_the_model(self):
        run = analyst._dispatcher([])
        assert "no tool named" in run("nope", {})

    def test_wrong_arguments_are_reported_to_the_model(self):
        tool = Tool("t", "d", {}, lambda sql="": sql)
        assert "wrong arguments" in analyst._dispatcher([tool])("t", {"bogus": 1})

    def test_a_raising_tool_is_reported_to_the_model(self):
        def boom(**kw):
            raise RuntimeError("kaboom")
        tool = Tool("t", "d", {}, boom)
        out = analyst._dispatcher([tool])("t", {})
        assert "TOOL FAILED" in out and "kaboom" in out

    def test_a_working_tool_returns_its_text(self):
        tool = Tool("t", "d", {}, lambda sql="": f"ran {sql}")
        assert analyst._dispatcher([tool])("t", {"sql": "x"}) == "ran x"


class TestTheReaderIsToldWhenThereWasNoTool:
    """The model being told is not enough. It answers from the snapshot without
    complaint, and the reader cannot tell that apart from an answer that
    checked. Over `--from`, snapshot-only is the normal case."""

    def _snap(self):
        return {"schema_version": 1, "generated_at": "2026-08-27T00:00:00+00:00",
                "asset": "btc", "sources": {}}

    def _ask(self, monkeypatch, db_path, **kw):
        monkeypatch.setattr(providers, "complete",
                            lambda *a, **k: providers.Completion("ok", "m"))
        return analyst.ask(self._snap(), "q", Config.from_env(
            provider="deepseek", model="deepseek-chat", db_path=db_path), **kw)

    def test_no_warehouse_is_reported_on_the_result(self, tmp_path, monkeypatch):
        r = self._ask(monkeypatch, tmp_path / "absent.duckdb")
        assert r.no_tools_reason == analyst.NO_TOOLS_UNAVAILABLE

    def test_turning_them_off_says_so_differently(self, db, monkeypatch):
        """A choice the operator made is not the same as a missing warehouse,
        and reporting both the same way makes the flag look like a fault."""
        r = self._ask(monkeypatch, db, use_tools=False)
        assert r.no_tools_reason == analyst.NO_TOOLS_DISABLED

    def test_nothing_is_said_when_a_tool_was_available(self, db, monkeypatch):
        r = self._ask(monkeypatch, db)
        assert r.no_tools_reason is None

    def test_an_unused_tool_is_not_a_missing_one(self, db, monkeypatch):
        """The model had the option and declined it. That is a different fact
        from having had no option, and must not be reported as one."""
        monkeypatch.setattr(providers, "complete",
                            lambda *a, **k: providers.Completion("ok", "m", 1, 2, ()))
        r = analyst.ask(self._snap(), "q", Config.from_env(
            provider="deepseek", model="deepseek-chat", db_path=db))
        assert r.tool_calls == () and r.no_tools_reason is None
