# dbt models over the Bluesky warehouse

Four models turning the raw record stream into tables you can query without
knowing how the pipeline works. Orchestrated by Dagster, materialized in
ClickHouse, living inside the `etl_pipeline` code location.

```
bsky_records_snapshot        (loaded by Dagster, not by dbt)
        │
        ▼
stg_bsky_records             view — applies FINAL once
        │
        ├──▶ fct_posts                one row per post
        ├──▶ dim_authors              one row per account
        └──▶ agg_activity_by_minute   incremental time series
```

---

## Why this exists

The warehouse table is called `bsky_records_snapshot` and holds every AT
Protocol record type: posts, likes, reposts, follows. Posts are about **one row
in eight**, and only they carry text.

That caused a real bug. The NL-to-SQL service pointed an LLM at the raw table,
the model read "records" as "posts," and *"how many posts in the last ten
minutes"* returned **109,436 instead of 14,942** — plausible, confidently
explained, and wrong by 7×. The first fix was to warn the model in its prompt.

`fct_posts` is the better fix. One row is one post, so being right is the
default instead of something a prompt has to remember to ask for. That is the
argument for a transformation layer in one example: it is cheaper to make the
data mean what it says than to make every consumer remember what it actually
means.

### The service reads the marts

For a while it didn't, which made the paragraph above a claim rather than a
description: the marts existed, and `services/analytics_api` still pointed a
single `POSTS_TABLE` at the raw snapshot with the warning paragraph doing the
work. Both fixes were in the repo and only the weaker one was wired up.

The service now takes all four models. `config.py` holds a table registry —
name, grain, when to use it, columns — and `llm_sql.py` assembles the schema
block from it, so adding a mart is a registry entry rather than a prompt edit
and the prompt cannot describe a table that no longer exists.

| Table | The model reaches for it when |
|---|---|
| `fct_posts` | the question is about posts |
| `agg_activity_by_minute` | the question is about volume over time, or compares record types |
| `dim_authors` | the question is about accounts |
| `stg_bsky_records` | nothing above fits |

Routing by grain is what removes the original failure. "How many posts" against
`fct_posts` is `count()` with nothing to forget, because there is nothing else
in the table. The `WHERE collection = 'app.bsky.feed.post'` warning survives in
the prompt, but only attached to `stg_bsky_records`, where it is still true.

Two things fell out of the switch that are worth knowing:

**The prompt carries two clocks now.** The marts are rebuilt on the freshness
policy below and `stg_bsky_records` is a view over a snapshot the sensor loads
every 60 seconds, so they are genuinely not the same instant. Reporting one
number would make *"in the last ten minutes"* mean something different depending
on which table the model happened to pick, with nothing in the answer to say so.
`executor.get_clocks()` reads both and the system prompt labels which tables
each one governs.

**`agg_activity_by_minute` is usually the cheaper answer.** "How does the volume
of likes compare to posts" is a scan of the record stream against
`stg_bsky_records` and a handful of rows against the aggregate. The registry
ordering and its `USE FOR` line are what push the model there — which is the
other half of what a semantic layer buys, and the half that is easy to build and
then never route anything to.

---

## What's in `dbt_project.yml`

Short file, four decisions:

**`profile: analytics`, resolved from env vars.** See *Credentials* below.

**`quoting: {database: false, schema: false, identifier: false}`.** ClickHouse
has no schemas in the Postgres sense — `database` is the namespace and the
warehouse already has one (`analytics`). Quoting on would generate DDL whose
identifiers don't match what the loader writes.

**`staging: +materialized: view` / `marts: +materialized: table`.** Set on the
directory rather than per model, so a new mart is a table by existing rather
than by someone remembering the config block.

**`macro-paths: ["macros"]`** for the one custom generic test.

Deliberately *not* set: `+schema:`, which would suffix models into
`analytics_marts` and split them away from the tables the loader writes into the
same database.

---

## The incremental model

`agg_activity_by_minute` is the only incremental one, and the only one where the
complexity earns itself: the record stream grows by tens of millions of rows a
day, and re-aggregating all of history to add sixty seconds of it is the
definition of work that shouldn't be repeated. The other three are small enough
that a full rebuild is faster than reasoning about correctness.

**Strategy: `delete+insert`, `unique_key = 'minute'`.**

*Why not `append`?* The trailing minute is always partial — the run happens
mid-minute and more events for it are still arriving. Appending writes that
undercount once and never revisits it, leaving a permanent dent in the series at
every run boundary. That's worse than a gap, because it looks like real data.
`delete+insert` removes the minutes in range and rewrites them, so a partial
minute gets corrected by the next run instead of frozen.

*Why a lookback window instead of `> max(minute)`?* The obvious predicate is
wrong twice. It never revisits the partial trailing minute, and it drops late
arrivals outright — and this pipeline does deliver late. The landing extract is
capped at `landing_batch_size` per run, so after an outage it drains a backlog
over several runs and rows for an already-summarised minute genuinely do show
up. A 15-minute lookback absorbs both.

The window is wider than the observed lag on purpose. Too narrow silently loses
late rows; too wide costs a few extra minutes of re-aggregation. Only one of
those is detectable after the fact.

*What guards it?* The `unique` test on `minute`. If `delete+insert` ever
degraded to `append`, a reprocessed minute would appear twice and that test is
what catches it. Verified: a second run took the table from 168 to 169 rows with
zero duplicates.

**Full refresh:** `dbt build --full-refresh --select agg_activity_by_minute`
drops and rebuilds from all of history. Needed after changing the model's
grain or adding a column — the incremental path only ever touches the lookback
window, so a schema change would otherwise leave old rows in the old shape.

---

## Why staging is a view

`stg_bsky_records` is the only model that reads the raw snapshot, and the only
place `FINAL` appears.

`FINAL` is what makes a `ReplacingMergeTree` correct to read: deduplication
happens at merge time, so without it a record edited or deleted since it was
loaded is still present under its old version. Applying it once here means no
downstream model can forget.

A view rather than a table because materializing it would freeze a copy of the
whole record stream and put a staleness window in front of every mart — for a
transformation that is a cast and a rename.

---

## Tests: dbt's, and Dagster's

Both assert things about data, and it would be easy to end up with two of
everything. The line:

| | Scope | Failure means |
|---|---|---|
| **dbt tests** | invariants *inside* a model | this table doesn't mean what its schema says |
| **Dagster asset checks** | contracts *between* owners | two components have stopped agreeing |

A test that would still make sense if this model were the only thing in the repo
is a dbt test. One that exists because two components have to agree is an asset
check — `warehouse_keys_loadable` guards the handoff from extract to loader and
blocks downstream execution; `delete_ratio_in_bounds` watches a source for
anomalies and only warns.

The `accepted_values` test on `collection` is the one worth pointing at. It's
pinned to the producer's subscription, so adding a fifth collection upstream
fails it — which is the point: `fct_posts` and `dim_authors` both switch on that
column, and an unexpected value would be silently dropped by one and silently
lumped into "other" by whoever reads the numbers next.

`between` is a local generic test (`macros/test_between.sql`) rather than
`dbt_utils.accepted_range`, because pulling in dbt_utils means `dbt deps`
fetching from the hub on every image build to get eleven lines of SQL. Worth it
for a second package; not for the first.

---

## Credentials

`profiles.yml` is entirely `env_var()` references, and `src/dbt.py` sets them
from `get_clickhouse_creds()` — the same accessor the `ClickHouseResource` is
built from.

This is the answer to "dbt brings a second config system." It does, and the fix
is to give it no independent source of truth. Credentials live where every other
component reads them (`config.{env}.yaml` + `secrets.{env}.yaml`, delivered as a
ConfigMap and a Secret on EKS); `profiles.yml` only restates them in dbt's shape.
There is no path by which dbt connects somewhere the rest of the platform does
not.

The defaults in `profiles.yml` exist so `dbt parse` can build a manifest at image
build time, when no warehouse is reachable and none is needed.

---

## Orchestration

`@dbt_assets` in `src/dbt.py` turns each dbt node into a Dagster asset. Two
details matter:

**The manifest is built at image build time**, not at import. `dagster-dbt`
reads the node list out of `target/manifest.json`; generating it when the code
server starts would make loading definitions depend on dbt parsing successfully
inside a live container, so a typo in a model would present as a dead code
location taking every unrelated asset down with it, rather than as a build
failure.

**The dbt source maps onto the existing Dagster asset key.** dbt calls it
`warehouse.bsky_records_snapshot`; Dagster already materializes
`bsky_records_snapshot`. `_Translator.get_asset_key` collapses the source to the
table name, which is what joins the two halves into one graph — without it dbt's
lineage is an island and nothing shows that the marts go stale when the load
fails.

`dbt build`, not `dbt run` then `dbt test`: build interleaves tests with models
and stops a failing model's children being built on top of it. Run-then-test
materializes everything first and only then reports that a grain was wrong — by
which point the bad table is what the NL-to-SQL service is querying.

`dbt_job` selects by group (`AssetSelection.groups("analytics_dbt")`), so a new
`.sql` file joins the job by existing rather than by being added to a list. It
still exists and is still the manual entry point, but in steady state nobody
runs it — see *Staying in sync with the stream*.

**Why not its own code location?** A separate deployment unit is how you isolate
a team's dependencies and blast radius. Four models owned by the people who own
the loader need neither — it would buy a container, a Dockerfile, a k8s manifest
and a workspace entry in exchange for a busier diagram.

---

## Staying in sync with the stream

The sensor loads `bsky_records_snapshot` every 60 seconds. The marts are tables,
so they only reflect the stream as of the last time dbt ran. Two separate
questions close that gap, and the design keeps them separate:

| Question | Answer | Where |
|---|---|---|
| How often should the marts rebuild? | every 5 minutes | `schedules.dbt_rebuild` |
| When should someone be told they haven't? | warn at 8 min, fail at 15 | `freshness.marts` |

The first is a **cost** decision, the second is an **SLA**. They were briefly one
number — the condition read `freshness_warned()`, so the warn window doubled as
the cadence — which was tidy and conflated two things that move for different
reasons. Tuning the rebuild to be cheaper should not silently move the threshold
at which someone gets paged.

### The trigger

```python
REBUILD_ON_CRON = (
    (AutomationCondition.missing() | AutomationCondition.cron_tick_passed("*/5 * * * *"))
    & ~AutomationCondition.any_deps_missing()
    & ~AutomationCondition.any_deps_in_progress()
    & ~AutomationCondition.in_progress()
)
```

A cron, because the cost of a rebuild is set by how much history there is, not
by how stale the marts are. `fct_posts` and `dim_authors` re-read a growing
record stream, so the honest question is "how often can we afford this" — a
fixed interval, not a function of lag.

Not `AutomationCondition.eager()`, which fires on any dependency update: the
snapshot lands every 60 seconds, so eager would rebuild every mart every minute
to fold in a minute of a stream.

| Clause | Why |
|---|---|
| `missing()` | Bootstraps a cold warehouse. Without it the marts sit empty until the first cron tick — a poor first five minutes after `make`. It is also what creates the staging view. |
| `cron_tick_passed(...)` | The trigger. |
| `~any_deps_missing()` | Don't build a mart on a snapshot that isn't there yet. |
| `~any_deps_in_progress()` / `~in_progress()` | Skip the tick if a rebuild is still running. |

**Why this and not a `ScheduleDefinition`.** A plain schedule would be simpler
and would do the same thing today. What it would not give is the last row of
that table: a cron schedule fires whether or not the previous run finished, so
as the rebuild grows past the interval it starts stacking runs on top of each
other. `streaming_ingest_job` solves the same problem with a tag concurrency
limit in `dagster.yaml`; here the guard comes with the condition. Putting the
cron inside an `AutomationCondition` keeps the trigger and its interlocks in one
place.

Observable difference from the freshness-driven version it replaced: runs now
land on wall-clock boundaries (`19:45:18`, `19:50:24`) instead of drifting by
the build duration each cycle (`19:24:27`, `19:29:58`, `19:35:33`).

**A `*/5` cron meets every hour-anchored schedule by construction**, which is
worth knowing before adding another heavy job to the warehouse. `*/5 * * * *`
and `0 H * * *` share a tick every day, not occasionally. That is how a mart
rebuild came to start twenty seconds into the nightly warehouse purge on
2026-08-03, with the two together exhausting the ClickHouse container; the purge
has since moved to `17 3 * * *`. Nothing in the condition above changed — but a
full rebuild reads the whole record stream, and that is a real claim on the
warehouse when something else is rewriting it. See
[When the Purge Took the Warehouse Down](../../../README.md#when-the-purge-took-the-warehouse-down).

### The alarm

```python
MART_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=timedelta(minutes=15),
    warn_window=timedelta(minutes=8),
)
```

**A freshness policy runs nothing**, and that is worth stating because the name
suggests otherwise. The `FreshnessDaemon` evaluates it every 30 seconds and
writes a `PASS`/`WARN`/`FAIL` state per asset, which shows in the UI and can be
alerted on. Nothing else happens. An asset with a policy and no automation just
turns red on schedule.

**The windows must sit clear of the cadence.** A mart on a 5-minute cron is
routinely just under 5 minutes stale immediately before its next rebuild, so
warning at 5 would mean warning always, and an alarm that is always on is not an
alarm. 8 minutes gives the cron a missed tick of headroom; 15 is three missed
ticks, by which point it is not a blip. Changing the cron without moving these
is the mistake this note exists to prevent.

### The sensor has to be declared

Dagster synthesizes a `default_automation_condition_sensor` for any code
location whose assets carry automation conditions — with
`default_status=STOPPED`. Left implicit, all of the above ships as a policy, a
condition that reads it, and nothing running to act on either: a feature that
looks implemented on the lineage graph and never fires until someone finds the
toggle in the UI.

`src/sensors/automation_sensor.py` declares it explicitly instead, named,
`RUNNING` by default, and scoped to `AssetSelection.groups("analytics_dbt")`.
The scope is deliberate — every other asset here is driven by the sensor, a
schedule, or a person, so a future asset that grows an automation condition has
to opt in rather than silently joining a sensor that was already running.

### The staging view has no freshness policy

`_Translator.get_asset_spec` attaches `MART_FRESHNESS` only to nodes tagged
`marts` — which the models already carry in their own config blocks, so a new
mart inherits the policy by being a mart rather than by being added to a list
here.

`stg_bsky_records` is deliberately excluded. It is a view: it re-reads the
snapshot on every query and cannot be stale. Putting a clock on it would measure
staleness that does not exist, and it would go red in any quiet period purely
because nothing had re-run something that did not need re-running.

It still gets the automation condition. Rebuilding a view on the cron is close
to free — a `CREATE OR REPLACE VIEW`, not a scan — and the `missing()` branch is
what creates it in the first place on a cold warehouse.

### Two policies, so the red means something

`bsky_records_snapshot` carries a freshness policy of its own (3 min warn /
10 min fail, in `src/assets/bsky_to_warehouse.py`) and **no** automation — the
sensor already drives it, and a second thing requesting it would race the sensor
for the same watermark. It is purely an alarm, and it earns its place by
disambiguating the marts' alarm:

| snapshot | marts | means |
|---|---|---|
| PASS | FAIL | dbt is broken |
| FAIL | PASS | ingest is down; the marts are faithfully reflecting a stream that stopped |
| FAIL | FAIL | ingest has been down long enough that the marts can't be current either |

Without a policy on the snapshot, the middle row reads exactly like the top one:
the marts go red and nothing on the graph says the cause is upstream of dbt.

### What has to be running

Two daemons, both enabled by default and both now stated explicitly in
`deployment/dagster.yaml` so the dependency is readable rather than inferred
from an absence:

- **`auto_materialize`** → `AssetDaemon`, evaluates `REBUILD_ON_CRON` and
  submits the runs. **This is the one the rebuild depends on.** Turn it off and
  the marts stop updating.
- **`freshness`** → `FreshnessDaemon`, writes the `PASS`/`WARN`/`FAIL` states.
  Turn it off and you lose the alarms only — the rebuild carries on, because
  since the split nothing in the trigger reads freshness state.

That second line is the point of separating them. Before the split, disabling
the freshness daemon would have silently stopped the marts rebuilding, which is
a surprising amount of blast radius for something that presents as monitoring.

---

## Running it

```bash
# through Dagster (what the schedule does)
#   Dagster UI → Jobs → dbt_job → Materialize

# directly, inside the code location container
docker exec -e DBT_CLICKHOUSE_PASSWORD=dagster deployment-code_location_etl-1 \
  dbt build --project-dir /app/dbt --profiles-dir /app/dbt

# rebuild the incremental model from scratch
docker exec -e DBT_CLICKHOUSE_PASSWORD=dagster deployment-code_location_etl-1 \
  dbt build --project-dir /app/dbt --profiles-dir /app/dbt \
  --select agg_activity_by_minute --full-refresh
```

## Why fct_posts and dim_authors are not incremental

They look like obvious candidates. Both are full-refresh, both re-read the whole
record stream every five minutes to fold in five minutes of it, and that cost
grows with history while the benefit does not. The fix looks like making them
incremental.

It isn't, and the reason is one property of the source table. Measured on a
4.6M-row snapshot:

| Read | Rows | Time |
|---|---|---|
| Full `FINAL` scan — what a full rebuild does | 4,648,667 | **0.222s** |
| 15-minute window on `event_at` — what incremental would do | 286,014 | **0.315s** |

**The incremental read is slower than the full one.**
`bsky_records_snapshot` is `ORDER BY (did, collection, rkey)` — the AT Protocol
identity of a record, because that is what `ReplacingMergeTree` has to collapse
edits and tombstones onto. Nothing about that ordering correlates with time, so
a predicate on `event_at` prunes no granules: ClickHouse reads all 4.6M rows,
pays `FINAL` exactly as it would anyway, and then throws away 94% of what it
read. The window filter is pure added work.

Reordering the table by time would fix the scan and break the deduplication that
makes updates and deletes work at all. The sort key is load-bearing.

So incremental would reduce the *write* and leave the dominant cost untouched,
in exchange for:

- a tombstone-driven deletion hook (see below — `delete+insert` cannot express
  retraction on its own),
- a lookback window to size and keep sized,
- a new failure mode where a rebuild outage longer than the lookback silently
  loses deletions,
- and a correctness invariant that now needs its own test.

That is a lot of moving parts on the wrong side of a 0.2 second scan. It is the
same trade the platform already refused for the load watermark: machinery whose
only job is avoiding work that turns out to be cheap.

**Two findings from the attempt, kept because they are not obvious:**

*Deletions are of old records.* Every tombstone in a sampled 10-minute window
was for a record created outside that window; the median retracted post was
about ten days old. Any incremental scheme here has to key its deletion window
off the **tombstone's own arrival time**, never the record's. A lookback on
record time would catch almost nothing while looking like it worked.

*`delete+insert` cannot delete what is absent.* In `dbt-clickhouse` 1.9.8 the
strategy emits `delete from <target> where (unique_key) in (select unique_key
from <new_data>)`. `incremental_predicates` are ANDed onto that, so they narrow
the delete and can never widen it. A retracted post is gone from the staging
view, so it is absent from the batch, so nothing deletes it. Retraction needs a
`pre_hook`, or `insert_overwrite` at partition granularity — not a strategy
setting.

**The growth concern was real; this just is not its fix.** The scan is O(history)
whichever way the model is materialized. The lever that actually caps it is
bounding what the marts *cover* — a rolling window rather than all of history —
which is a decision about what the tables mean, not about how they are built.

## Known limits

- **`dbt-clickhouse` is less mature** than the Snowflake/BigQuery adapters.
  Incremental strategies against `ReplacingMergeTree` need care, which is why
  the marts are plain `MergeTree` — dbt owns them outright and nothing else
  writes them, so replacing-merge semantics would buy nothing and complicate
  `delete+insert`.
- **`threads: 1`.** Four models on a single-node warehouse; concurrency would
  buy nothing and makes a failure harder to read.
- **No snapshots.** Nothing here needs SCD2 — the warehouse already holds
  current state and the landing zone already holds the full history.
- **`etl_table_snapshot` is declared as a source but not modelled.** It's
  synthetic data from the batch demo; declaring it documents what else the
  warehouse holds without pretending there's a mart worth building on it.
