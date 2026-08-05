# Streaming Pipeline

The platform consumes **Bluesky's Jetstream firehose**, a live public stream of every repository write on the network.

```
Jetstream (WebSocket) → Kafka Producer → Kafka → Spark Structured Streaming → Postgres
                                                                                  │
                                       Dagster sensor → parquet landing → ClickHouse
```

The producer holds a persistent WebSocket subscription and forwards every commit, `create`, `update` and `delete` alike, keyed by author DID. Spark parses and flattens each event and appends it to Postgres as an immutable change log. A Dagster sensor (`bsky_record_sensor`) watches that table every 60 seconds and triggers `streaming_ingest_job`, which lands new events as Parquet and folds them into current state in ClickHouse.

The whole path is event-driven. No schedules, no manual triggers.

```python
@sensor(job_name="streaming_ingest_job", minimum_interval_seconds=60,
        default_status=DefaultSensorStatus.RUNNING)
def bsky_record_sensor(context: SensorEvaluationContext):
    # MAX(id), not COUNT(*): the id is the primary key, so this is an index
    # lookup rather than a sequential scan over a table growing by ~500 rows/sec
    last_max = int(context.cursor) if context.cursor else 0
    if current_max > last_max:
        context.update_cursor(str(current_max))
        yield RunRequest(run_key=f"bsky_ingest_{current_max}")
```

## Components

| Container | Role | Image |
|---|---|---|
| `kafka` | KRaft-mode broker, no Zookeeper | `apache/kafka:3.8.1` |
| `kafka_producer` | Jetstream WebSocket consumer, publishes to Kafka | Custom (Python, websockets, confluent-kafka) |
| `spark_consumer` | Structured Streaming, Kafka to Postgres | Custom (PySpark 3.5.4) |

The producer publishes a **fixed envelope** rather than the raw event. Routing and identity fields (`did`, `time_us`, `operation`, `collection`, `rkey`, `cid`) are hoisted to the top level, and the record body passes through as an opaque JSON string.

Record bodies have no stable shape. A post carries any combination of embeds, facets, langs and reply refs, and the firehose also carries third-party lexicons, so a fixed schema over the nested body would break on contact with real traffic. Typing the envelope and leaving the body as text keeps the contract stable. The consumer parses the handful of post fields it needs in a second pass.

---

## Measured throughput

Numbers observed on this stack, not estimates. Setup: Apple M4, Docker Desktop, `spark_consumer` capped at `mem_limit: 2g` and `cpus: "1.5"`, 10-second `processingTime` trigger, `maxOffsetsPerTrigger=20000`, JDBC `batchsize=1000`.

**Source rates** (Jetstream, live):

| Subscription | Rate |
|---|---|
| `app.bsky.feed.post` only | ~34 events/sec |
| post + repost + like + follow (default) | **~510–560 events/sec** |
| Cursor replay of a backlog | ~4,990 events/sec sustained |

Likes alone are about two-thirds of the firehose. The replay figure is Jetstream's own server-side ceiling, not the producer's. Re-running the same replay with 4× the CPU and memory produced an identical rate at 19.5% CPU.

**Spark steady state** (107 batches on the live stream, batch 0 excluded as JIT-cold):

| Metric | Median |
|---|---|
| Rows per batch | 5,213 |
| Batch duration | 292 ms |
| `addBatch` share of batch | 82.2% |
| Input rate | 521 rows/sec |
| Processing rate | 18,292 rows/sec |
| Offset lag | 0 |

**Where the time actually goes.** `addBatch` dominates the batch, so per-batch overhead is negligible and cost is per-row. Splitting `addBatch` with the sink's own timers:

| Stage | Median per batch |
|---|---|
| Transform (Kafka fetch, JSON parse, projection) | 176 ms |
| JDBC write | 53 ms |

**The bottleneck is the transform, not the JDBC sink.** A 3.3× ratio, with the sink only 23% of `addBatch`. The intuition runs the other way, which is why it is worth stating plainly. An A/B on `reWriteBatchedInserts` confirms it: enabling it moves the JDBC leg 1.57× (25,306 against 16,106 rows/sec), but because JDBC is under a quarter of the batch, total batch time improves by roughly 7%. Tuning the sink harder would have been the wrong instinct.

*(Note `reWriteBatchedInserts` is the Postgres spelling. `rewriteBatchedStatements` is MySQL's and is a silent no-op against pgjdbc.)*

**Recovery from a 180-second outage.** The consumer was stopped while the producer kept writing, accumulating a 90,242-event backlog:

| Batch | Rows | Duration | Processed rows/sec | Offset lag |
|---|---|---|---|---|
| 113 | 20,000 | 4,140 ms | 4,830 | 90,242 |
| 114 | 20,000 | 1,634 ms | 12,240 | 73,976 |
| 116 | 20,000 | 642 ms | 31,153 | 44,952 |
| 119 | 20,000 | 734 ms | 27,248 | 1,091 |
| 120 | 6,527 | 349 ms | 18,702 | 0 |

Seven batches pinned at the `maxOffsetsPerTrigger` ceiling, then back to steady state. The first batch is 6× slower than the rest, from JIT and cache warm-up, which is exactly why batch 0 is excluded from the steady-state figures above.

The ceiling is doing real work here. A 20,000-row batch completes in ~734 ms, so a single container can process about **27,000 rows/sec**. With a 10-second trigger, though, `maxOffsetsPerTrigger=20000` caps *sustained* drain at 2,000 rows/sec. Recovery is therefore trigger-bound rather than compute-bound: against a ~500/sec live rate, a T-second outage takes roughly T/3 to clear. That is the deliberate price of bounding batch memory. Without the cap, the first batch after an outage plans one micro-batch spanning the entire backlog and the 2 GB container dies.

---

## Sizing: does this need Spark at all?

**No, and that is the point.** At ~520 events/sec against a measured single-container capacity of ~27,000 events/sec, Spark sits idle roughly 98% of the time. A single-threaded Python consumer with batched inserts would keep up with this firehose comfortably.

Spark earns its place here for **stateful streaming semantics, not throughput**: checkpointed offset management that survives restarts, bounded-memory backpressure via `maxOffsetsPerTrigger`, and exactly-once delivery into the sink. Those are the properties that make a 180-second outage a non-event rather than a data-loss incident.

The crossover where distributed compute becomes genuinely necessary is roughly **25,000 to 30,000 events/sec sustained**, about fifty times Bluesky's entire public firehose. Below that, a single node with good checkpointing is the correct engineering answer, and reaching for a cluster is cost without benefit.

---

## Change data capture: creates, updates and deletes

Jetstream is already a change stream. Every event carries `commit.operation` (`create`, `update` or `delete`) and the platform handles all three rather than filtering to inserts.

Deletes are not a rounding error here. They are about **4% of observed traffic**, against roughly 96% creates and a handful of updates. People retract posts constantly, so an insert-only pipeline would accumulate content the author has explicitly withdrawn.

All three cases fall out of a single mechanism. The warehouse table is a `ReplacingMergeTree` keyed on `(did, collection, rkey)`, the AT Protocol identity of a record rather than our ingestion id, versioned by the firehose timestamp, with an `is_deleted` flag:

```sql
ENGINE = ReplacingMergeTree(event_time_us, is_deleted)
PARTITION BY toDate(fromUnixTimestamp64Micro(event_time_us))
ORDER BY (did, collection, rkey)
```

The sorting key carries `collection` because `rkey` is only unique *within* a collection for a repo. `(did, rkey)` alone holds for the default subscription and breaks the moment a lexicon using a fixed rkey is added. The `PARTITION BY` is not for reads. It exists so the purge can rewrite one day instead of the whole table.

A create inserts a row. An update is a later version of the same key, and `FINAL` returns the newest. A delete is a tombstone, and `FINAL` drops the key entirely. The `analytics_ro` profile sets `final = 1`, so the NL-to-SQL service gets deduplicated, delete-respecting results without the LLM needing to know any of this.

**Hiding is not deleting.** `FINAL` stops a deleted post being *returned*, but its text is still sitting in the parts on disk. For content a real person chose to retract, "you cannot query it" is the wrong guarantee. So `purge_deleted_records` runs on a schedule (`17 3 * * *`, from config) and does the physical removal, dropping the tombstone and the record it retracts together. On one 149k-event sample, 124 posts had been created and then deleted within the observed window, and their text remained readable on disk until the purge ran.

The seventeen minutes are load-bearing. Any `0 H * * *` schedule coincides with the marts' `*/5 * * * *` rebuild by construction, so while the purge sat on the hour it landed on a dbt rebuild every single day. That was never a decision. Both crons happened to be anchored to the hour.

The purge is a separate scheduled asset rather than part of the load because a ClickHouse mutation rewrites every part it touches. That sentence used to end "cheap once a day, ruinous every 60 seconds", which was half right and the wrong half. On 2026-08-03 it proved ruinous once a day too, and took the warehouse down with it for 80 minutes. The cadence was never what made the purge safe. The shape of the statement was, and the shape was wrong. What that cost and what replaced it is in [When the purge took the warehouse down](incident-2026-08-03.md).

It covers `bsky_records_snapshot` only. `fct_posts` is not listed beside it, which is worth stating since that table carries `text` too. The marts are dbt tables rebuilt every five minutes from a `FINAL` view, which never returns a tombstoned key, so each rebuild reconstructs them without the retracted rows and drops the old table. They are already purged on a five-minute cycle by ordinary operation, and folding them into this daily mutation would make that guarantee twenty-four hours worse. Measured mid-cycle: 29 retracted posts still in `fct_posts` against 36,436 tombstoned in the snapshot, only those retracted since the last rebuild. That depends on the marts being full-refresh, which is one of the reasons they are.

One honest limit: this works because Jetstream *hands us* the change events. Getting the same semantics out of an ordinary database means reading its write-ahead log, which is a different piece of infrastructure.

### Production note: CDC out of a database

The ETL pipeline's Postgres source has no such stream. It is read incrementally with a high-water mark, which captures inserts but not updates or deletes. In production that gap is closed with a log-based CDC pipeline:

```text
Source DB --> Debezium CDC --> Kafka (raw topic) --> staging table
                                                        |
                                                    Dagster (validate, transform, enrich)
                                                        |
                                                    Kafka (clean topic) --> final table
```

Each stage is independently buffered. Debezium captures row-level changes without polling the source database. The staging table absorbs burst writes so that Dagster can process at its own pace. Publishing back to Kafka after transformation gives downstream consumers a clean, validated stream and decouples processing speed from ingestion rate.

This matters because in production the source stream may produce millions of events per minute. Without the staged decoupling, a slow transformation step would backpressure the entire pipeline. With it, each component scales independently and failures at one stage do not cascade to others.

### Latency

The CDC pattern above targets **near-real-time** workloads where processing within a few minutes is acceptable. Dagster sensors poll on an interval of seconds to minutes, and each triggered run has scheduling and startup overhead. That is the right fit for analytics, warehousing, and most data platform use cases.

For **sub-second latency** (live dashboards, fraud detection, real-time pricing), Dagster should not be in the hot path. The transform layer would be a dedicated stream processor instead:

```text
Source DB --> Debezium CDC --> Kafka (raw) --> Faust / Kafka Streams / Spark Streaming (transform)
                                                        |
                                                  Kafka (clean) --> final table / real-time consumers
                                                        |
                                                  Dagster (periodic audit, reconciliation, monitoring)
```

A lightweight stream processor handles validation and transformation continuously with millisecond-level latency. Dagster steps back from the hot path entirely and runs periodic audits instead: reconciling counts between the raw and clean topics, detecting drift, flagging anomalies, and materializing aggregated snapshots to the warehouse on a schedule.

The two patterns are not mutually exclusive. A production platform often runs both, with the stream processor on the real-time path while Dagster manages the batch path and provides observability across the whole system.
