"""
Tests for the table registry the NL-to-SQL system prompt is assembled from.

Drift here is silent rather than loud: a clock pointed at a table the model was
never shown, or a mart that quietly stops leading the list, produces a
confident wrong answer instead of an error.
"""
import importlib.util
import re

import pytest

from conftest import REPO_ROOT, load_module

analytics_config = load_module("services/analytics_api/config.py", name="analytics_config")

TABLES = analytics_config.TABLES
WAREHOUSE = analytics_config.WAREHOUSE

TABLE_IDS = [table.name for table in TABLES]
BY_SHORT_NAME = {table.name.rsplit(".", 1)[-1]: table for table in TABLES}


def _short_name(table) -> str:
    return table.name.rsplit(".", 1)[-1]


def _index_of(short_name: str) -> int:
    return TABLE_IDS.index(f"{WAREHOUSE}.{short_name}")


@pytest.mark.parametrize("table", TABLES, ids=TABLE_IDS)
def test_every_table_is_fully_described(table):
    # Each field lands in the prompt verbatim; an empty one is a table the
    # model is shown but told nothing about.
    for field in ("name", "grain", "use", "schema", "time_column"):
        assert getattr(table, field).strip(), f"{table.name}.{field} is empty"


@pytest.mark.parametrize("table", TABLES, ids=TABLE_IDS)
def test_every_table_is_qualified_by_the_warehouse_database(table):
    # The service connects with a default database, but the generated SQL is
    # read by humans and copied elsewhere, so the prompt shows it qualified.
    assert table.name.startswith(f"{WAREHOUSE}.")
    assert _short_name(table)


def test_table_names_are_unique():
    assert len(TABLE_IDS) == len(set(TABLE_IDS))


def test_the_four_expected_marts_are_registered():
    assert set(BY_SHORT_NAME) == {
        "fct_posts",
        "agg_activity_by_minute",
        "dim_authors",
        "stg_bsky_records",
    }


# The ordering is the routing: the model picks from this list, and `fct_posts`
# winning over `stg_bsky_records` is what makes the one-row-in-eight
# undercount unrepresentable rather than warned against.
def test_fct_posts_is_offered_before_stg_bsky_records():
    assert _index_of("fct_posts") < _index_of("stg_bsky_records")


def test_fct_posts_leads_the_list():
    assert _index_of("fct_posts") == 0


def test_stg_bsky_records_is_offered_last():
    assert _index_of("stg_bsky_records") == len(TABLES) - 1


_CLOCK_RE = re.compile(r"SELECT max\((\w+)\) FROM (\S+)")

CLOCKS = [
    ("mart", analytics_config.MART_CLOCK),
    ("stream", analytics_config.STREAM_CLOCK),
]


@pytest.mark.parametrize("clock_sql", [sql for _, sql in CLOCKS], ids=[name for name, _ in CLOCKS])
def test_clock_reads_a_table_the_model_was_told_about(clock_sql):
    # A clock over an unregistered table would still return a timestamp, and
    # the model would anchor "the last ten minutes" on a table it cannot see.
    match = _CLOCK_RE.search(clock_sql)
    assert match, f"clock is not a recognisable max() probe: {clock_sql!r}"
    assert match.group(2) in TABLE_IDS


@pytest.mark.parametrize("clock_sql", [sql for _, sql in CLOCKS], ids=[name for name, _ in CLOCKS])
def test_clock_reads_a_column_that_table_actually_has(clock_sql):
    column, table_name = _CLOCK_RE.search(clock_sql).groups()
    schema = BY_SHORT_NAME[table_name.rsplit(".", 1)[-1]].schema
    assert re.search(rf"^\s*{column}\b", schema, re.MULTILINE)


@pytest.mark.parametrize("clock_sql", [sql for _, sql in CLOCKS], ids=[name for name, _ in CLOCKS])
def test_clock_reads_the_time_column_the_registry_advertises_for_that_table(clock_sql):
    # The clock is the anchor for "the last ten minutes", so it has to advance
    # on the same column the model is told to filter by. Probing a different
    # timestamp on the same table would drift with no error anywhere.
    column, table_name = _CLOCK_RE.search(clock_sql).groups()
    table = BY_SHORT_NAME[table_name.rsplit(".", 1)[-1]]
    assert column == table.time_column


def test_the_two_clocks_read_different_tables():
    # One number for both would make "the last ten minutes" mean something
    # different depending on which table the model happened to pick.
    tables = {_CLOCK_RE.search(sql).group(2) for _, sql in CLOCKS}
    assert len(tables) == len(CLOCKS)


def _declares_column(schema: str, column: str) -> bool:
    # Anchored at line start so a column named in a trailing `--` comment does
    # not read as a declaration.
    return bool(re.search(rf"^\s*{re.escape(column)}\b", schema, re.MULTILINE))


@pytest.mark.parametrize("table", TABLES, ids=TABLE_IDS)
def test_declared_time_column_is_a_column_of_that_table(table):
    # The invariant the per-table `time_column` exists for. The prompt renders
    # this field verbatim, so as long as it names a column the table declares,
    # the prompt cannot send the model to a column that is not there -- which
    # is exactly what a single global "use event_at" did to dim_authors.
    assert _declares_column(table.schema, table.time_column), (
        f"{table.name} declares time_column {table.time_column!r}, "
        "which is not in its own schema"
    )


EXPECTED_TIME_COLUMNS = {
    "fct_posts": "event_at",
    "agg_activity_by_minute": "minute",
    "dim_authors": "last_seen_at",
    "stg_bsky_records": "event_at",
}


@pytest.mark.parametrize("short_name, column", sorted(EXPECTED_TIME_COLUMNS.items()))
def test_each_table_pins_its_expected_time_column(short_name, column):
    assert BY_SHORT_NAME[short_name].time_column == column


def test_the_tables_that_defeat_a_global_time_instruction_keep_their_own_columns():
    # These two are the reason the field exists rather than a sentence in the
    # prompt, so unifying them on `event_at` is the regression, not a tidy-up.
    assert BY_SHORT_NAME["dim_authors"].time_column != "event_at"
    assert BY_SHORT_NAME["agg_activity_by_minute"].time_column != "event_at"


def test_dim_authors_has_no_event_at_column():
    # The fact underneath the whole fix: one row per account means there is no
    # per-event timestamp to name, so any instruction naming one globally is
    # wrong here no matter how emphatically the prompt states it.
    assert "event_at" not in BY_SHORT_NAME["dim_authors"].schema


def test_dim_authors_routes_recency_questions_to_fct_posts():
    # Half the fix is routing, and routing lives in prose: a correct
    # time_column still answers the wrong question if the model stays here.
    assert "fct_posts" in BY_SHORT_NAME["dim_authors"].use


@pytest.mark.parametrize("table", TABLES, ids=TABLE_IDS)
def test_no_table_anchors_on_the_authors_own_clock(table):
    # The prompt forbids client_created_at outright -- those clocks run hours
    # wrong -- so a table declaring it would make the prompt contradict itself.
    assert table.time_column != "client_created_at"


# The two checks below read llm_sql.py as text rather than importing it: the
# module does `from executor import get_clocks`, and executor imports
# clickhouse_connect, which is deliberately not a test dependency. Reading the
# source is the only way to catch the registry and the prompt drifting apart,
# so both assertions are kept to a single string each.
_LLM_SQL_SOURCE = (REPO_ROOT / "services/analytics_api/llm_sql.py").read_text()


def _function_source(name: str) -> str:
    start = _LLM_SQL_SOURCE.index(f"\ndef {name}(")
    end = _LLM_SQL_SOURCE.find("\ndef ", start + 1)
    return _LLM_SQL_SOURCE[start:] if end == -1 else _LLM_SQL_SOURCE[start:end]


def test_the_prompt_no_longer_names_one_time_column_for_every_table():
    # The contradicting prose has to be gone, not merely outvoted by the
    # per-table lines: the model has no way to tell which one wins.
    assert not re.search(r"TIME-RELATED,?\s+USE\s+event_at", _function_source("generate_sql"))


def test_the_prompt_renders_each_tables_declared_time_column():
    # A registry field nothing reads is not a fix -- the prompt would simply
    # name no time column at all.
    assert "table.time_column" in _function_source("_render_tables")


@pytest.mark.parametrize("provider", sorted(analytics_config.PROVIDER_MODELS))
def test_every_provider_offers_at_least_one_model(provider):
    models = analytics_config.PROVIDER_MODELS[provider]
    assert models and all(model.strip() for model in models)


def test_default_provider_is_one_of_the_offered_providers():
    # The UI seeds the model dropdown from PROVIDER_MODELS[DEFAULT_PROVIDER];
    # an unknown default is a KeyError at import, before the page renders.
    assert analytics_config.DEFAULT_PROVIDER in analytics_config.PROVIDER_MODELS


CLICKHOUSE_ENV_VARS = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
)


def _config_with_env(monkeypatch, **overrides):
    """Re-execute config.py so its os.getenv defaults are evaluated fresh."""
    for name in CLICKHOUSE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    path = REPO_ROOT / "services/analytics_api/config.py"
    spec = importlib.util.spec_from_file_location("_analytics_config_env_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_clickhouse_user_defaults_to_the_readonly_user(monkeypatch):
    # The readonly user IS the guardrail (deployment/clickhouse/users.xml), so
    # defaulting to the read/write `dagster` user would hand an LLM the grants
    # that let it drop the warehouse.
    fresh = _config_with_env(monkeypatch)
    assert fresh.CLICKHOUSE_USER == "analytics_ro"
    assert fresh.CLICKHOUSE_USER != "dagster"


def test_clickhouse_defaults_target_the_analytics_database(monkeypatch):
    fresh = _config_with_env(monkeypatch)
    assert fresh.CLICKHOUSE_DATABASE == "analytics"
    assert fresh.WAREHOUSE == fresh.CLICKHOUSE_DATABASE


def test_clickhouse_settings_are_overridable_from_the_environment(monkeypatch):
    fresh = _config_with_env(
        monkeypatch,
        CLICKHOUSE_HOST="warehouse.internal",
        CLICKHOUSE_PORT="9000",
        CLICKHOUSE_DATABASE="staging",
        CLICKHOUSE_USER="someone_else",
    )
    assert fresh.CLICKHOUSE_HOST == "warehouse.internal"
    assert fresh.CLICKHOUSE_PORT == 9000
    assert fresh.CLICKHOUSE_USER == "someone_else"
    # Table names are built from WAREHOUSE at import, so a database override
    # has to reach the registry or the prompt names tables in the wrong schema.
    assert fresh.WAREHOUSE == "staging"
    assert all(table.name.startswith("staging.") for table in fresh.TABLES)
