import threading

import clickhouse_connect

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    MART_CLOCK,
    STREAM_CLOCK,
)

# The client is created lazily and cached for the life of the process, rather
# than reconnecting on every query. It is deliberately NOT created at import
# time: the API must still start when ClickHouse is not reachable yet (the
# container frequently comes up before the ClickHouse StatefulSet is ready).
# If the first connection attempt fails, _client stays None and the next call
# retries. The underlying HTTP connection pool handles ClickHouse restarts.
_client = None
_client_lock = threading.Lock()


def get_client():
    global _client

    if _client is None:
        with _client_lock:
            if _client is None:
                _client = clickhouse_connect.get_client(
                    host=CLICKHOUSE_HOST,
                    port=CLICKHOUSE_PORT,
                    username=CLICKHOUSE_USER,
                    password=CLICKHOUSE_PASSWORD,
                    database=CLICKHOUSE_DATABASE,
                )

    return _client


def execute_sql(sql):
    result = get_client().query(sql)
    return list(result.column_names), result.result_rows


def _scalar(sql):
    """First column of the first row, or None if the query fails.

    Swallowing the error is deliberate: these are context probes, not the
    answer. A warehouse that is up but has no marts yet should still let the
    service start and answer questions against the raw view.
    """
    try:
        rows = get_client().query(sql).result_rows
        return rows[0][0] if rows else None
    except Exception:
        return None


def get_clocks():
    """
    How recent the data actually is, for anchoring relative-date questions.

    Two clocks, because there are genuinely two. `stg_bsky_records` is a view
    over the snapshot the sensor loads every 60 seconds, so it tracks the
    firehose. The marts are dbt tables rebuilt on a freshness policy (see
    code_locations/etl_pipeline/dbt/README.md), so they sit up to a few minutes
    behind by design. Reporting one number for both would make "in the last ten
    minutes" mean something different depending on which table the model picked,
    with nothing in the answer to say so.

    Both derive from event_at, NOT created_at, and the difference is not
    academic. created_at is stamped by the author's own client and clocks are
    routinely wrong: on a live sample the newest created_at sat almost nine
    hours ahead of the newest firehose timestamp, which was itself twenty
    seconds behind wall clock. Anchoring on that told the model "now" was nine
    hours in the future, so every "in the last ten minutes" question filtered a
    window containing nothing and came back empty -- an answer that looks like
    an absent-data problem rather than a wrong-clock one.
    """
    return {"mart": _scalar(MART_CLOCK), "stream": _scalar(STREAM_CLOCK)}
