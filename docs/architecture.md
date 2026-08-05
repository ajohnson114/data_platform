# Architecture

Three planes that never mix responsibilities.

## Control plane

Dagster webserver and daemon. Scheduling, dependency resolution, run tracking, observability. It never executes business logic.

Runs go through a `QueuedRunCoordinator` rather than launching on submission. A sensor fires every 60 seconds against a firehose, and the default run launcher executes runs *inside* the code location container, so the queue is a memory budget as much as a scheduling policy. A tag limit holds `streaming_ingest_job` to one run at a time, so two ingests cannot read the same extract watermark and re-land the same id range.

## Execution plane

Code locations. Each team owns its assets, checks and compute, and deploys independently via its own gRPC code server. A failure or dependency conflict in one team's container cannot affect another team's pipelines.

## Data plane

Postgres is the operational landing store. Spark writes the stream into it, and the ETL pipeline persists its output there.

ClickHouse is the analytical warehouse. Assets write to it explicitly through a shared `ClickHouseResource` (`write_table`, `read_table`, `query_df`), one table per asset in the `analytics` database, so every team reads the same warehouse without coupling to another team's code. It is the same service in both worlds: a `clickhouse` container under Docker Compose, a `clickhouse` StatefulSet on EKS. Assets address it identically either way.

**The second hop.** The warehouse reads the landed Parquet itself, `file()` under Compose and `s3()` on EKS, so the bytes never cross the code location. Same code path in both environments, which is the point: a bug in the load is reproducible under `make` rather than only in the cluster. The landing volume mounts read-only into the ClickHouse container to make the local half of that true.

A `FrameLoader` fallback still pulls the file through pandas, for a landing zone the warehouse cannot reach. The choice between the two derives from the zone rather than sitting in config beside it, so a direct load out of storage the warehouse cannot see is unrepresentable rather than validated.

**The IO manager is a separate concern.** It only carries intermediate values between steps, so it stays stock and is chosen from config: `FilesystemIOManager` locally (`type: fs`), `S3PickleIOManager` on AWS (`type: s3`, reusing the compute-logs bucket under a `dagster-io/` prefix). There is no custom IO manager code.

```text
Jetstream  → Kafka Producer → Kafka → Spark Structured Streaming → Postgres.bsky_records
                                                                        │
                          Dagster sensor → bsky_records_snapshot ────────┤
                                                                        ├→ ClickHouse ─┬→ NL-to-SQL
generated → clean → save_data_to_postgres_db → Postgres.etl_table       │  (analytics)  └→ ml_pipeline
                                     └→ etl_table_snapshot ─────────────┘
```

![System Design](sys_design_with_streaming.png)

## Key guarantees

**Team isolation.** Each code location is a separate container with separate dependencies. A crash or import error in one team's code cannot stop another team's pipelines from running.

**Validation-gated execution.** Blocking asset checks stop Dagster executing any downstream asset when an upstream check fails. Bad data stops at the boundary, not silently downstream. See [Validation gates](#validation-gates) below.

**Clear ownership.** Every asset and every check has exactly one owning team. No shared mutable state, no hidden coupling across boundaries.

---

## Asset lineage

The Dagster UI renders the full dependency graph across every asset group and both code locations (**Lineage**, with all groups expanded):

![Global Asset Lineage](asset_lineage_global.png)

> The same graph is committed as [`asset_lineage_global.svg`](asset_lineage_global.svg), exported from the UI as vector. Open it if you want to read the individual cards rather than squint at them.

**Cross-team dependencies are first-class.** `save_data_to_postgres_db` (ETL team) depends on `clean_data` from its own group and on `prepare_postgres_tables` from `db_setup`. The UI draws that contract across the group boundary rather than hiding it inside a job. The same holds one hop out, where `streaming_ingest` hands `bsky_records_snapshot` to the dbt marts in `analytics_dbt`: two different owners, one edge, drawn.

Every asset also carries the technology it touches: Postgres, Parquet, ClickHouse, dbt, Scikit Learn. That is what makes the graph readable as a system rather than as a list of Python functions.

> **Tip:** the graph above uses the UI's default horizontal layout. Graphs with many cross-group edges often read better vertically. In the lineage view, click the gear icon at the bottom right of the graph pane and select **Change graph to vertical orientation** (`⌥O`). The setting is remembered per browser.

---

## Execution results

That graph is the live state after `etl_job`, `ml_pipeline_job` and `failing_job` have run with the streaming sensor going. Every asset carries its own status and check counts, so pipeline health reads straight off the lineage view.

**Green means materialized.** The ETL chain (`pull_data_from_source` → `clean_data` → `save_data_to_postgres_db` → `etl_table_landed` → `etl_table_snapshot`), the `db_setup` table preparation, the ML chain and the four dbt models all completed.

> `prepare_postgres_tables` is the one card reading `Loading…` rather than a status. The UI virtualises off-screen cards, and `db_setup` sits at the far right edge, so it had not re-rendered when the SVG was exported. It materialized with everything else.

**The check counts are the interesting part**, because each one is a different kind of contract:

| Asset | Count | What it asserts |
|---|---|---|
| `pull_data_from_warehouse` | 2 / 2 | the ML team's schema and null checks against the ETL team's output. A cross-team contract, blocking, evaluated before any modelling |
| `fit_model` | 2 / 2 | the model's own held-out RMSE gate, plus an advisory train/holdout gap warning |
| `bsky_records_landed` | 2 / 2 | `warehouse_keys_loadable` (blocking, the columns ClickHouse cannot accept a null in) and `delete_ratio_in_bounds` (advisory) |
| `stg_bsky_records` | 6 / 6 | dbt tests: grain, nullability, and the pinned set of collections |
| `fct_posts` · `dim_authors` · `agg_activity_by_minute` | 5 / 5 · 4 / 4 · 4 / 4 | dbt tests on each mart |

**Red means failed, deliberately.** In `failing_pipeline`, `do_not_clean_data` materialized but its blocking null check failed (**0 / 1 Passed**), so Dagster halted at the boundary. `do_other_operation` is marked failed without ever executing its business logic, and `show_stack_trace_for_returning_wrong_type` fails outright by returning a type that violates its output contract. That group exists to demonstrate the gate, not to work.

**"Never materialized" is not a failure either.** `purge_deleted_records` and `purge_landing_archive` run on a daily schedule (`17 3 * * *`), so on a stack started this morning they have not had a reason to run yet. That is what a scheduled housekeeping asset should look like before its first tick.

---

## Validation gates

Asset checks act as hard execution gates. A check declared `blocking=True` stops Dagster executing downstream assets when it fails, so bad data stops at the boundary.

### Within a team: null checks

The ETL pipeline validates its own data before passing it downstream:

```python
@asset_check(asset="do_not_clean_data", blocking=True)
def check_no_nulls_in_required_columns(do_not_clean_data: pd.DataFrame) -> AssetCheckResult:
    columns_to_check = get_config().get_cols_required_to_not_have_nulls()
    null_counts = df[columns_to_check].isna().sum()
    ...
    return AssetCheckResult(passed=total_nulls == 0, metadata={...})
```

### Across teams: schema drift detection

The ML pipeline does not trust the ETL pipeline's output blindly. Before using any data it runs its own checks against `pull_data_from_warehouse`, the asset that reads the ETL team's `etl_table_snapshot` out of the warehouse:

```python
@asset_check(asset="pull_data_from_warehouse", name="schema_matches_etl_table", blocking=True)
def check_schema_matches_etl_table(pull_data_from_warehouse: pd.DataFrame) -> AssetCheckResult:
    if len(pull_data_from_warehouse) == 0:
        return AssetCheckResult(passed=False,
                                metadata={"reason": "DataFrame is empty, run etl_job first"})

    df = pull_data_from_warehouse
    expected_schema = {
        col: TYPE_MAPPING[spec["type"]]
        for col, spec in get_config().get_expected_schema_from_etl_pipeline().items()
    }
    # Checks for missing columns AND wrong dtypes
    ...
    return AssetCheckResult(passed=passed, metadata={
        "missing_columns": missing_columns,
        "columns_with_wrong_dtype": wrong_type_columns,
        "observed_dtypes": {col: str(df[col].dtype) for col in df.columns},
    })
```

The ML team defines what schema it expects from the ETL team's output, and the pipeline will not proceed if the contract is violated. Each team owns its own validation. No hidden coupling, no silent failures across boundaries.

### On the streaming path, where blocking is the wrong answer

The two checks above guard synthetic data. These run against the live firehose, and they are deliberately different in kind, because treating every check as a gate is how `blocking` stops meaning anything.

**`warehouse_keys_loadable` is blocking, and the gate is real.** It asserts that every column ClickHouse makes non-nullable (the sorting key, the version, the delete flag) is present and non-null. That list derives from the warehouse target rather than from config, so changing the sorting key moves the check with it. A violation is not a quality preference being missed, it is the `INSERT` failing. Catching it here turns a type error thrown from inside the ClickHouse driver into a named check with counts attached.

**`delete_ratio_in_bounds` is advisory, and fails `WARN`.** Retractions are a normal, constant share of the firehose, 3.36% measured over 128k events, so a spike is worth looking at rather than worth halting ingest over. A mass retraction is real data a reader should still see, and refusing to load it because it was surprising would be the pipeline deciding what the truth is allowed to be. The bound sits well clear of steady state, aimed at the producer mis-tagging operations rather than at normal variance.

Both read counts the extract already computed while the rows were in memory. A check that reopened the landed Parquet to count nulls would hand back exactly what pushing the read into the warehouse was for.

The distinction is visible in the UI rather than only in the source. A check declares its own severity, and Dagster plots the metadata each evaluation returns:

![Streaming asset checks](asset_checks_streaming.png)

That is `delete_ratio_in_bounds` on a live run: labelled **non-blocking**, passing, and carrying the numbers it judged. `delete_ratio` 0.0249 against a `max_delete_ratio` of 0.25, over 27,226 rows. The series underneath is every previous evaluation, which turns "deletes are about 3 to 4% of the firehose" from a claim in a README into something a reader can watch hold.

The other two live under the assets they guard: the ML team's cross-team contract on [`pull_data_from_warehouse`](asset_checks_detail.png), and the model's own held-out RMSE gate on [`fit_model`](asset_checks_model.png).

---

## Transformation layer (dbt)

Everything above is EL: extract from Postgres, land Parquet, load ClickHouse. `code_locations/etl_pipeline/dbt/` is the T. Four models turn the raw record stream into tables that mean what they say.

```
bsky_records_snapshot ──▶ stg_bsky_records ──┬──▶ fct_posts
   (loaded by Dagster)     (view, FINAL)     ├──▶ dim_authors
                                             └──▶ agg_activity_by_minute
                                                  (incremental)
```

It exists because of a specific bug rather than for completeness. The NL-to-SQL service queried the raw table, whose name says records but which an LLM reads as posts, and posts are one row in eight. *"How many posts in the last ten minutes"* came back as **109,436 instead of 14,942**. Warning the model in its prompt was the first fix. `fct_posts`, where one row is one post, is the better one.

The models are Dagster assets, not a separate system. `@dbt_assets` expands the manifest into one asset per node, and the dbt *source* maps onto the existing `bsky_records_snapshot` asset key. It is one lineage graph, so the marts visibly go stale when the load fails rather than sitting in an island of their own. They carry the dbt and ClickHouse badges in the UI for the same reason every other asset carries its technology.

They live inside the ETL code location rather than a third one. A separate deployment unit isolates a team's dependencies and blast radius, and four models owned by the people who own the loader need neither.

### Keeping the marts in sync with the stream

The sensor lands a batch every 60 seconds, and the marts are tables, so they are only as current as the last dbt run. Rebuilding them eagerly on every load would re-aggregate the whole record stream once a minute to fold in a minute of it, and a cron rebuild fires at the same rate whether the marts are behind or not.

Two separate questions get two separate answers, which is the part worth copying:

| Question | Answer | Kind of decision |
|---|---|---|
| How often should the marts rebuild? | every 5 minutes (`AutomationCondition.cron_tick_passed`) | cost |
| When should someone be told they have not? | warn at 8 min, fail at 15 (`FreshnessPolicy`) | SLA |

A `FreshnessPolicy` runs nothing. The `FreshnessDaemon` writes a `PASS`/`WARN`/`FAIL` state every 30 seconds and that is all it does. Wiring the trigger to read that state is tempting, and the first version here did, which made the warn window double as the cadence. It conflates two numbers that move for different reasons: tuning the rebuild to be cheaper should not silently move the threshold at which someone gets paged. The windows now sit clear of the cadence on purpose, since a mart on a 5-minute cron is routinely 5 minutes stale a moment before its next rebuild.

The cron lives inside an `AutomationCondition` rather than a `ScheduleDefinition` so that `~in_progress()` comes with it. A plain schedule fires whether or not the last run finished, and stacks once the rebuild outgrows the interval.

`bsky_records_snapshot` carries a policy too, with no automation, purely so the two alarms disambiguate each other. Snapshot green with marts red is a dbt problem. Snapshot red with marts green is an ingest outage the marts are faithfully reflecting.

Where dbt tests stop and Dagster asset checks start is written down in [`dbt/README.md`](../code_locations/etl_pipeline/dbt/README.md), along with the full automation design, why the staging view gets no freshness policy, and why the incremental model uses `delete+insert` with a lookback window instead of appending past `max(minute)`.
