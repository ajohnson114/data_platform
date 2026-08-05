# Design Decisions

## Why separate code locations instead of one monolith?

Each code location runs in its own container with its own dependencies. A failure or dependency conflict in one team's code cannot break another team's pipelines.

## Why is the warehouse separate from the IO manager?

They are two different jobs, and one component doing both serves neither well.

ClickHouse is the warehouse. Assets write to it explicitly, one queryable table per asset, and anything that speaks SQL can read it: the ML pipeline, the NL-to-SQL service, a BI tool. Dagster's IO manager is only transport for intermediate values between steps, so it stays stock (`FilesystemIOManager` locally, `S3PickleIOManager` on AWS) and swaps by config rather than code.

Collapsing the two makes the warehouse a pile of pickles. Fine for handing a DataFrame to the next step, useless to an LLM that needs a real SQL surface to query.

## Why incremental loads instead of full snapshots?

A full replace costs O(table) on every run no matter how little changed, and the streaming sensor fires roughly every 60 seconds. Both source tables are append-only with a monotonic `id`, so each asset asks the warehouse for the highest `id` it already holds and reads only what is above it. Cost is proportional to new rows rather than to accumulated history.

**There is exactly one watermark, and it belongs to the extract.** It is the highest id already landed, read off the file listing. The load has none: the extract hands it the file it just wrote, and it loads that file.

An earlier version watermarked both hops, so the load could work out for itself which files were outstanding and catch up unattended. That bought automatic recovery from a long warehouse outage. It cost a bookmark table in ClickHouse, a set of accessors to maintain it, and a subtle interaction with `purge_deleted_records`: because the purge physically removes rows, `max(id)` over the loaded data reports a mark *lower* than the load actually reached, so the purged table needed its position stored somewhere the purge could not reach. That is a lot of machinery, and a failure mode to reason about, in service of not having to re-run a job.

Retries buy the same recovery more directly. The load carries a `RetryPolicy` (3 attempts, exponential backoff) for a step that fails, and the instance sets `run_retries` for a run that dies outright. Re-inserting is free because the sorting key dedupes. And the honest bit: a failure that outlives both now surfaces as a *failed run* rather than as a later run quietly loading ten files at once. The self-healing was partly hiding the incident.

Deleting the load watermark deleted the purge interaction with it. Not managed, gone.

The warehouse tables are still `ReplacingMergeTree`, which is what makes all of the above safe: a retried or re-delivered file collapses instead of double-counting. `ReplacingMergeTree` deduplicates at merge time, so readers that need exactness ask for it. The ML pipeline's query uses `SELECT ... FINAL`, and the `analytics_ro` profile sets `final = 1` so the NL-to-SQL service gets the same guarantee without the LLM having to remember it. Schema drift becomes an explicit `ALTER TABLE ADD COLUMN` rather than a side effect of rewriting the table.

## Why land Parquet before the warehouse?

Each source is extracted once into an immutable Parquet file, and the warehouse is loaded from those files rather than from Postgres:

```
Postgres ──▶ etl_table_landed ──▶ etl_table_snapshot ──▶ ClickHouse
             (parquet archive)     (load + provenance)
```

Only the landing asset touches Postgres. That buys three things a direct load does not:

1. The warehouse can be rebuilt without going back to the operational database, so recovery costs no load on the system serving traffic. It still works if `etl_table` has since been truncated or aged out.
2. Reprocessing with new logic replays files instead of re-extracting.
3. Every warehouse row carries `_source_file` and `_landed_at`, so a value can be traced back to the exact file it arrived in.

It is also what makes `bsky_records` evictable. At firehose rate that change log grows by tens of millions of rows a day, and nothing needs its history once the rows are landed: the Parquet archive is the durable copy, and the sensor only ever reads `MAX(id)`, which is unaffected by removing old rows. The safe bound is the landing zone's own bookmark, never evict above the highest id already landed, which makes it a Dagster asset rather than a cron job since that number is one directory listing away. At this volume the mechanism should be a native Postgres partition drop rather than `DELETE`, which would leave a day's worth of dead tuples for autovacuum to chase on a table that is nothing but a hot append path.

**The file name is the index:**

```
bsky_records__0000050001-0000100000__20260802T024248Z.parquet
```

Id range plus landing timestamp, zero-padded so a lexicographic listing is a chronological one. No catalog and no sidecar state store, because the extract's bookmark is the highest id already landed, read straight off the listing.

That is also what makes the replay work. `rebuild_warehouse_from_landing` reloads the archive into an empty warehouse in id order, optionally from a `from_id` if only a recent window was lost. That job is the *only* thing that reads the listing to decide what to load. The steady-state path never does, because it is handed its file. Keeping the replay machinery out of a job that runs every 60 seconds, and in one that runs when someone has just dropped a table, is the whole point of separating them.

**The archive is bounded, by privacy rather than by disk.** `purge_landing_archive` drops landed files past a 7-day horizon (`landing_retention_days`), which is what stops "immutable" meaning "keeps a retracted post's text forever". A landed file is still never *rewritten*, and that property is what makes a replay reproduce exactly what the live load saw. It is eventually dropped whole. So replay loses reach rather than trust: it goes back seven days and no further, which is a limit worth stating rather than a guarantee quietly broken. The disk argument points the same way, since at firehose rate the zone grows by gigabytes a day and nothing bounded it before.

The retention job never deletes the newest file of a dataset, whatever its age. That guard is load-bearing rather than defensive: the extract's bookmark *is* `max(end_id)` over this listing, so an empty directory would silently reset it to zero and re-extract the source from the beginning. The warehouse would dedupe the result, so nothing would look broken. It would just quietly redo every batch.

**Why not Iceberg or Delta?** Their value is the catalog and snapshot-metadata layer: multi-engine concurrent writers, row-level updates on the lake, table-version time travel. With one writer and one reader whose only job is replay into ClickHouse, plain Parquet buys the same recovery story without a catalog to run in both environments. Iceberg starts paying the moment a second engine writes these tables.

The full-replace path (`write_table`, which stages a table and `EXCHANGE TABLES` swaps it in atomically) is still the right tool for a *mutable* source, where rows can change in place and re-reading is the only way to be correct. Neither current source is mutable.

### Two honest limits, on different sources

**The insert-only limit is about `etl_table`, not the streaming path.** A high-water mark on a `BIGSERIAL` sees new rows, so it cannot see a row updated or hard-deleted in place, and `etl_table` is an ordinary table where that could happen.

`bsky_records` is not. It is a change log: Jetstream hands us `create`, `update` and `delete` as distinct events and Spark appends each one as a new row with a new id, so the watermark *does* capture all three there. Updates and deletes arrive as inserts into the log, and `bsky_records_snapshot` folds them down to current state with `ReplacingMergeTree(event_time_us, is_deleted)`. Closing the gap for a genuinely mutable source means log-based CDC, reading the Postgres WAL with something like Debezium, which is a different piece of infrastructure rather than a change to this asset.

**The ordering limit applies to both**, because it is about whether the watermark skips a row of the log, regardless of what that row means. It assumes ids *commit* in order, which a sequence does not guarantee. `BIGSERIAL` is monotonic in allocation, not in commit: two concurrent transactions can take ids 100 and 101 and commit them the other way round, and a reader doing `WHERE id > :watermark` in between sees 101, advances past 100, and skips that row for good.

What makes it safe here is not that the tables are append-only. It is that there is exactly one writer. The Kafka topic has a single partition, so the streaming DataFrame has one partition, so Spark's JDBC sink opens one connection and commits serially. Raising the topic's partition count or setting `JDBC_NUM_PARTITIONS > 1` would break it silently.

Because that guarantee rests on a deployment detail rather than on the schema, every row now carries `kafka_partition` and `kafka_offset`, the stream's true total order, so the assumption is auditable rather than folklore:

```sql
SELECT count(*) FROM bsky_records a JOIN bsky_records b
  ON a.kafka_partition = b.kafka_partition
 WHERE a.kafka_offset < b.kafka_offset AND a.id > b.id;
```

Anything but `0` means ids and stream order have diverged. They are provenance rather than a fix, since the watermark still reads `id`. The fix at more than one writer is a per-partition offset watermark, or Debezium, where the WAL's LSN carries commit order directly and the sequence never has to.

## Why Kafka and Spark for streaming?

It demonstrates that the platform handles both batch orchestration (Dagster) and stream processing (Spark), with Dagster observing and materializing the streaming outputs rather than managing the stream itself.

## Why a singleton config pattern?

It keeps the demo readable: one object, loaded once, with named accessors instead of dictionary spelunking at every call site.

The obvious next step is not a different *delivery* mechanism, since ConfigMaps and Secrets already do that job in the `aws` environment. It is giving the config a typed shape in Python. Today `_Config` is a bag of `self.config['data_pipeline']['postgres']['read_from_etl_table']` lookups, so a typo or a missing key surfaces as a `KeyError` deep inside an asset at materialization time, in the cluster, long after the pod started. Parsing into a typed structure once at import turns that into a startup failure that names the missing key.

That matters more here than it would elsewhere, because `config.aws.yaml` is produced by `aws_up.sh` doing string substitution on `${...}` placeholders. That adds a whole failure class, the placeholder that never got replaced, which currently reaches the database layer as a connection attempt to a host literally named `${RDS_HOST}`.

Dataclasses are enough for this and cost nothing in dependencies: validate on construction, fail loudly on a missing key, no silent defaults for anything credential-shaped. Pydantic buys coercion, nested validation and better error messages, at the price of a non-trivial dependency in every code location image. Either works. The win is *when* you find out the config is wrong, not which library reports it.

One constraint either choice has to respect: the two files are merged (`config.{env}.yaml` plus `secrets.{env}.yaml`), and in the repo the secrets are still `${...}` placeholders. So validate presence, never format. A password-shape check would break local development against the literal placeholder string.

## Why NL-to-SQL as a platform service?

LLM-powered query interfaces are increasingly a first-class concern for data platforms. Building it as a separate optional service (a Docker Compose profile) shows how platform capabilities can be layered on top of the orchestration stack without modifying it.
