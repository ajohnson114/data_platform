"""
Unit tests for the Spark consumer's metric helpers.

These four functions compute every number the README's benchmark tables
report -- offset lag, addBatch share, rows/sec. None of them can fail loudly:
a slip in the arithmetic produces a plausible-looking measurement that gets
published as fact. That is the whole reason they are worth testing apart from
the streaming job they instrument.

They would be more naturally testable if they lived in a `metrics.py` importing
nothing but the standard library. They do not -- they share consumer.py with
the job, so reaching them means dragging in pyspark. The stubbing below is a
consequence of that file layout, not of the helpers themselves. The source is
deliberately left alone; this note is the finding, not a TODO acted on here.
"""
import importlib.util
import json
import math
import os
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

from conftest import REPO_ROOT, load_module

CONSUMER_PATH = "spark_consumer/consumer.py"

# consumer.py imports pyspark at module scope and builds ENVELOPE_SCHEMA /
# RECORD_SCHEMA by *calling* StructType and friends while the module body runs.
# BatchMetricsListener also subclasses StreamingQueryListener at class-creation
# time. So the import cannot be deferred or caught -- pyspark has to resolve to
# something before the file will execute at all.
#
# Installing throwaway modules under these names is what makes that resolution
# succeed without a ~300MB dependency that none of the code under test touches:
# the helpers here are pure standard library, and the schema objects the stubs
# fake out are never read by anything asserted below.
#
# WHAT THIS DOES NOT COVER, and it is a real gap: everything that actually
# talks to Spark. The schemas are not checked against the producer's envelope,
# the column projections in main() are not exercised, and write_batch_to_postgres
# is not called. A stub cannot tell you that from_json was handed the wrong
# struct. Those need a real SparkSession and belong in an integration suite.
_STUBBED_MODULES = (
    "pyspark",
    "pyspark.sql",
    "pyspark.sql.functions",
    "pyspark.sql.streaming",
    "pyspark.sql.types",
)


def _unused(*args, **kwargs):
    return None


def _build_pyspark_stubs() -> dict[str, ModuleType]:
    pyspark = ModuleType("pyspark")
    pyspark.StorageLevel = SimpleNamespace(MEMORY_AND_DISK=object())

    sql = ModuleType("pyspark.sql")
    sql.SparkSession = type("SparkSession", (), {})

    functions = ModuleType("pyspark.sql.functions")
    for fn in ("coalesce", "col", "from_json", "lit", "to_timestamp", "transform", "translate"):
        setattr(functions, fn, _unused)

    streaming = ModuleType("pyspark.sql.streaming")

    # A real class, not a Mock: BatchMetricsListener subclasses this, and a
    # MagicMock is not a valid base.
    class StreamingQueryListener:
        pass

    streaming.StreamingQueryListener = StreamingQueryListener

    types_module = ModuleType("pyspark.sql.types")
    for name in ("ArrayType", "BooleanType", "LongType", "StringType", "StructField", "StructType"):
        setattr(types_module, name, _unused)

    pyspark.sql = sql
    sql.functions = functions
    sql.streaming = streaming
    sql.types = types_module

    return {
        "pyspark": pyspark,
        "pyspark.sql": sql,
        "pyspark.sql.functions": functions,
        "pyspark.sql.streaming": streaming,
        "pyspark.sql.types": types_module,
    }


@contextmanager
def _pyspark_stubs():
    """Hold the stubs in sys.modules only while a module body is executing.

    Scoped rather than installed once at import: the loaded module keeps direct
    references to the names it imported, so the entries are dead weight the
    moment exec finishes. Leaving them behind would mean any later test file --
    or a future one that legitimately wants real pyspark -- silently gets these
    hollow objects instead.
    """
    previous = {name: sys.modules.get(name) for name in _STUBBED_MODULES}
    sys.modules.update(_build_pyspark_stubs())
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


with _pyspark_stubs():
    consumer = load_module(CONSUMER_PATH, "spark_consumer_consumer")


def _emitted_lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def _run_progress(listener, progress, capsys) -> dict:
    """Drive the listener with a progress payload and parse the metric line."""
    event = SimpleNamespace(progress=SimpleNamespace(json=json.dumps(progress)))
    listener.onQueryProgress(event)
    lines = _emitted_lines(capsys)
    assert len(lines) == 1
    return json.loads(lines[0])


def _progress_with_source(**source) -> dict:
    return {"batchId": 1, "durationMs": {"triggerExecution": 100, "addBatch": 40}, "sources": [source]}


# -------------------------
# _finite
# -------------------------
@pytest.mark.parametrize("value", [0, 1, -7, 2**40, 0.0, 1.5, -3.25])
def test_finite_returns_ordinary_numbers_unchanged(value):
    assert consumer._finite(value) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_finite_drops_nan_and_infinities(value):
    # json.dumps would happily write NaN/Infinity, which no strict JSON reader
    # (jq included) will parse -- and the aggregation in emit_metric's docstring
    # is a jq pipeline over the whole run.
    assert consumer._finite(value) is None


@pytest.mark.parametrize("value", [None, "1.0", "", [1], {"a": 1}, object()])
def test_finite_drops_non_numbers(value):
    assert consumer._finite(value) is None


@pytest.mark.parametrize("value", [True, False])
def test_finite_drops_bools(value):
    # bool subclasses int, so the isinstance check passes and math.isfinite(True)
    # is True. Dropping the explicit bool guard would turn a flag into the number
    # 1 in a rates field -- valid JSON, wrong measurement, no error anywhere.
    assert consumer._finite(value) is None


# -------------------------
# _offset_total
# -------------------------
def test_offset_total_sums_partitions_of_one_topic():
    total = consumer._offset_total({"bluesky_events": {"0": 123, "1": 456}})
    assert total == 579
    assert isinstance(total, int)


def test_offset_total_accepts_the_json_string_form():
    # Spark renders Kafka offsets as an embedded object in the progress JSON but
    # as an opaque string via attribute access, so both forms reach this helper.
    total = consumer._offset_total('{"bluesky_events": {"0": 123, "1": 456}}')
    assert total == 579
    assert isinstance(total, int)


def test_offset_total_sums_across_topics_and_partitions():
    assert consumer._offset_total({
        "topic_a": {"0": 10, "1": 20, "2": 30},
        "topic_b": {"0": 5, "1": 1},
    }) == 66


@pytest.mark.parametrize("offset", [
    "not json at all",
    '{"topic": {"0": 1}',      # truncated
    "123",                      # parses, but not a map
    "[1, 2, 3]",
    None,
    17,
    ["topic"],
    {"topic": 5},               # partition map is not a map
    {"topic": "0:123"},
])
def test_offset_total_returns_none_for_malformed_input(offset):
    # None rather than 0: a lag of zero and a lag we could not read are very
    # different claims, and only one of them belongs in a benchmark table.
    assert consumer._offset_total(offset) is None


@pytest.mark.parametrize("offset", [{}, "{}", {"topic": {}}])
def test_offset_total_is_zero_for_an_empty_map(offset):
    total = consumer._offset_total(offset)
    assert total == 0
    assert isinstance(total, int)


def test_offset_total_truncates_float_offsets_to_int():
    assert consumer._offset_total({"topic": {"0": 12.9}}) == 12


# -------------------------
# BatchMetricsListener.onQueryProgress
# -------------------------
def test_offset_lag_is_latest_minus_end(capsys):
    # The number the README's recovery table reports: how far behind the head of
    # the topic a capped batch left the consumer.
    line = _run_progress(
        consumer.BatchMetricsListener(),
        _progress_with_source(
            description="KafkaV2[Subscribe[bluesky_events]]",
            startOffset={"bluesky_events": {"0": 1000}},
            endOffset={"bluesky_events": {"0": 21000}},
            latestOffset={"bluesky_events": {"0": 93500}},
            numInputRows=20000,
        ),
        capsys,
    )
    source = line["sources"][0]
    assert source["end_offset"] == 21000
    assert source["latest_offset"] == 93500
    assert source["offset_lag"] == 72500
    assert source["start_offset"] == 1000
    assert source["num_input_rows"] == 20000


@pytest.mark.parametrize("missing", ["endOffset", "latestOffset"])
def test_offset_lag_is_none_when_either_side_is_missing(capsys, missing):
    source = {
        "endOffset": {"bluesky_events": {"0": 21000}},
        "latestOffset": {"bluesky_events": {"0": 93500}},
    }
    source[missing] = None
    line = _run_progress(consumer.BatchMetricsListener(), _progress_with_source(**source), capsys)
    assert line["sources"][0]["offset_lag"] is None


def test_add_batch_pct_and_overhead_split_the_batch_wall_clock(capsys):
    line = _run_progress(
        consumer.BatchMetricsListener(),
        {"batchId": 4, "durationMs": {"triggerExecution": 3000, "addBatch": 2607}, "sources": []},
        capsys,
    )
    assert line["batch_duration_ms"] == 3000
    assert line["add_batch_ms"] == 2607
    assert line["overhead_ms"] == 393
    assert line["add_batch_pct"] == 86.9


def test_add_batch_pct_is_rounded_to_two_decimal_places(capsys):
    line = _run_progress(
        consumer.BatchMetricsListener(),
        {"batchId": 5, "durationMs": {"triggerExecution": 7, "addBatch": 1}, "sources": []},
        capsys,
    )
    assert line["add_batch_pct"] == 14.29


def test_zero_duration_batch_yields_none_pct_rather_than_dividing_by_zero(capsys):
    # An empty trigger can report triggerExecution=0. An exception raised inside
    # a StreamingQueryListener is not contained to the listener -- it takes the
    # streaming query down, so ingestion stops because a metric could not be
    # computed. The pct is simply undefined here and must be reported as such.
    line = _run_progress(
        consumer.BatchMetricsListener(),
        {"batchId": 0, "durationMs": {"triggerExecution": 0, "addBatch": 0}, "sources": []},
        capsys,
    )
    assert line["add_batch_pct"] is None
    assert line["overhead_ms"] == 0


@pytest.mark.parametrize("durations", [{"addBatch": 40}, {"triggerExecution": 100}, {}])
def test_missing_duration_leaves_derived_fields_none(capsys, durations):
    line = _run_progress(
        consumer.BatchMetricsListener(),
        {"batchId": 2, "durationMs": durations, "sources": []},
        capsys,
    )
    assert line["overhead_ms"] is None
    assert line["add_batch_pct"] is None


def test_non_finite_rates_are_nulled_before_emission(capsys):
    # The first batch of a run reports NaN rates; those must not reach the line.
    listener = consumer.BatchMetricsListener()
    progress = SimpleNamespace(
        batchId=0,
        timestamp="2026-08-02T00:00:00.000Z",
        numInputRows=0,
        inputRowsPerSecond=float("nan"),
        processedRowsPerSecond=float("inf"),
        durationMs={"triggerExecution": 10, "addBatch": 5},
        sources=[],
    )
    listener.onQueryProgress(SimpleNamespace(progress=progress))
    lines = _emitted_lines(capsys)
    assert len(lines) == 1
    assert "NaN" not in lines[0] and "Infinity" not in lines[0]
    parsed = json.loads(lines[0])
    assert parsed["input_rows_per_second"] is None
    assert parsed["processed_rows_per_second"] is None


# -------------------------
# _progress_to_dict
# -------------------------
def test_progress_to_dict_prefers_the_json_rendering():
    # The attribute fallback would report batchId 99; Spark's own JSON wins.
    progress = SimpleNamespace(json='{"batchId": 7, "durationMs": {"addBatch": 3}}', batchId=99)
    assert consumer._progress_to_dict(progress) == {"batchId": 7, "durationMs": {"addBatch": 3}}


@pytest.mark.parametrize("json_attr", [None, "", "{not valid json", "[1, 2"])
def test_progress_to_dict_falls_back_to_attributes(json_attr):
    source = SimpleNamespace(
        description="KafkaV2[Subscribe[bluesky_events]]",
        startOffset='{"bluesky_events":{"0":1}}',
        endOffset='{"bluesky_events":{"0":9}}',
        latestOffset='{"bluesky_events":{"0":11}}',
        numInputRows=8,
    )
    progress = SimpleNamespace(
        batchId=3,
        timestamp="2026-08-02T00:00:00.000Z",
        numInputRows=8,
        inputRowsPerSecond=1.5,
        processedRowsPerSecond=2.5,
        durationMs={"triggerExecution": 100, "addBatch": 40},
        sources=[source],
    )
    if json_attr is not None:
        progress.json = json_attr

    result = consumer._progress_to_dict(progress)
    assert result["batchId"] == 3
    assert result["durationMs"] == {"triggerExecution": 100, "addBatch": 40}
    assert result["inputRowsPerSecond"] == 1.5
    assert result["sources"] == [{
        "description": "KafkaV2[Subscribe[bluesky_events]]",
        "startOffset": '{"bluesky_events":{"0":1}}',
        "endOffset": '{"bluesky_events":{"0":9}}',
        "latestOffset": '{"bluesky_events":{"0":11}}',
        "numInputRows": 8,
    }]


def test_progress_to_dict_fallback_tolerates_absent_sources_and_durations():
    result = consumer._progress_to_dict(SimpleNamespace(batchId=1))
    assert result["sources"] == []
    assert result["durationMs"] == {}


def test_lag_is_computed_from_string_offsets_on_the_fallback_path(capsys):
    # The fallback hands _offset_total strings rather than maps; the lag must
    # come out the same either way, or a PySpark build without .json would
    # publish nulls where the JSON build publishes numbers.
    listener = consumer.BatchMetricsListener()
    progress = SimpleNamespace(
        batchId=1,
        timestamp=None,
        numInputRows=8,
        inputRowsPerSecond=1.0,
        processedRowsPerSecond=1.0,
        durationMs={"triggerExecution": 100, "addBatch": 40},
        sources=[SimpleNamespace(
            description="KafkaV2[Subscribe[bluesky_events]]",
            startOffset='{"bluesky_events":{"0":1}}',
            endOffset='{"bluesky_events":{"0":9}}',
            latestOffset='{"bluesky_events":{"0":11}}',
            numInputRows=8,
        )],
    )
    listener.onQueryProgress(SimpleNamespace(progress=progress))
    line = json.loads(_emitted_lines(capsys)[0])
    assert line["sources"][0]["offset_lag"] == 2


# -------------------------
# emit_metric
# -------------------------
def test_emit_metric_writes_one_parseable_line_keyed_by_metric_first(capsys):
    consumer.emit_metric("batch", {"rows": 12, "ms": 3.5})
    lines = _emitted_lines(capsys)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    # First key, not merely present: `grep '"metric":"spark.streaming.batch"'`
    # in the docstring's aggregation pipeline matches on the leading position.
    assert next(iter(parsed)) == "metric"
    assert parsed["metric"] == f"{consumer.METRIC_NAMESPACE}.batch"
    assert parsed["rows"] == 12
    assert lines[0].startswith('{"metric":')


def test_emit_metric_keeps_one_line_despite_newlines_quotes_and_unicode(capsys):
    # Post text never reaches a metric line today, but the one-object-per-line
    # contract is what the whole aggregation rests on: a raw newline in any value
    # splits one record into two, and half of a JSON object kills the jq pass
    # over the entire run.
    hostile = 'first\nsecond\r\nthird "quoted" — \U0001f98b\ttab'
    consumer.emit_metric("batch", {"text": hostile, "rows": 1})
    lines = _emitted_lines(capsys)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["text"] == hostile
    assert parsed["rows"] == 1


def test_emit_metric_stringifies_unserialisable_values_instead_of_raising(capsys):
    # default=str is load-bearing: metrics are emitted from inside the listener
    # and the foreachBatch sink, so a TypeError here would surface as a dead
    # streaming query rather than a missing log line.
    class Opaque:
        def __str__(self):
            return "opaque-value"

    consumer.emit_metric("jdbc_write", {"obj": Opaque(), "set": {1}, "rows": 5})
    lines = _emitted_lines(capsys)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["obj"] == "opaque-value"
    assert parsed["rows"] == 5


def test_emit_metric_namespaces_every_name(capsys):
    for name in ("query_started", "batch", "jdbc_write", "query_terminated"):
        consumer.emit_metric(name, {})
    lines = _emitted_lines(capsys)
    assert [json.loads(line)["metric"] for line in lines] == [
        f"{consumer.METRIC_NAMESPACE}.{name}"
        for name in ("query_started", "batch", "jdbc_write", "query_terminated")
    ]


# -------------------------
# JDBC URL composition
# -------------------------
def _load_consumer_with_env(**env):
    """Re-execute consumer.py under a patched environment.

    JDBC_URL is composed at import time from os.environ, so there is no function
    to call with a different flag -- re-running the module body is the only way
    to observe the other branch, and asserting on a locally rebuilt f-string
    would test the test rather than the source. Loaded under a throwaway name
    and unregistered afterwards so nothing else can pick up the extra copy.
    """
    name = "_consumer_jdbc_probe"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / CONSUMER_PATH)
    module = importlib.util.module_from_spec(spec)
    with _pyspark_stubs(), mock.patch.dict(os.environ, env):
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
    return module


_PG_ENV = {
    "POSTGRES_HOST": "pg-host",
    "POSTGRES_PORT": "5433",
    "POSTGRES_DB": "bench",
}


def test_jdbc_url_appends_rewrite_batched_inserts_when_enabled():
    module = _load_consumer_with_env(JDBC_REWRITE_BATCHED_INSERTS="true", **_PG_ENV)
    assert module.JDBC_REWRITE_BATCHED_INSERTS is True
    assert module.JDBC_URL == "jdbc:postgresql://pg-host:5433/bench?reWriteBatchedInserts=true"


def test_jdbc_url_omits_the_parameter_when_disabled():
    # The off branch is genuinely reachable: the hardcoded `true` in the f-string
    # is the *parameter value*, and it only ever renders on the enabled branch,
    # so flipping the flag drops the whole query string rather than emitting
    # `reWriteBatchedInserts=false`. Both are equivalent to pgjdbc; what matters
    # for the benchmark is that the A/B knob actually moves.
    module = _load_consumer_with_env(JDBC_REWRITE_BATCHED_INSERTS="false", **_PG_ENV)
    assert module.JDBC_REWRITE_BATCHED_INSERTS is False
    assert module.JDBC_URL == "jdbc:postgresql://pg-host:5433/bench"
    assert "reWriteBatchedInserts" not in module.JDBC_URL


@pytest.mark.parametrize("raw, expected", [("TRUE", True), ("True", True), ("1", False), ("no", False), ("", False)])
def test_rewrite_flag_parsing_is_case_insensitive_and_otherwise_strict(raw, expected):
    # Worth pinning because the emitted `rewrite_batched_inserts` field labels
    # every benchmark row: setting the env to "1" turns the optimisation off
    # while reading, to a human, like it turned it on.
    module = _load_consumer_with_env(JDBC_REWRITE_BATCHED_INSERTS=raw, **_PG_ENV)
    assert module.JDBC_REWRITE_BATCHED_INSERTS is expected
    assert ("reWriteBatchedInserts=true" in module.JDBC_URL) is expected


def test_jdbc_rewrite_defaults_to_on():
    module = _load_consumer_with_env(**_PG_ENV)
    assert module.JDBC_REWRITE_BATCHED_INSERTS is True


# -------------------------
# Test-harness invariant
# -------------------------
def test_pyspark_stubs_do_not_outlive_module_loading():
    # Guards the rest of the suite: these entries are scoped to a module body,
    # so a later test file importing pyspark for real -- or asserting it is
    # absent -- is unaffected by anything this file did.
    assert not any(name in sys.modules for name in _STUBBED_MODULES)


def test_loaded_module_survives_stub_removal():
    # The helpers hold no reference to pyspark, which is the premise of the whole
    # approach: they keep working with the stubs long gone.
    assert consumer._finite(1.0) == 1.0
    assert math.isfinite(consumer._offset_total({"t": {"0": 1}}))
