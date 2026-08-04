import json
import math
import os
import time

from pyspark import StorageLevel
from pyspark.sql import SparkSession
from pyspark.sql.functions import coalesce, col, from_json, lit, to_timestamp, transform, translate
from pyspark.sql.streaming import StreamingQueryListener
from pyspark.sql.types import ArrayType, BooleanType, LongType, StringType, StructField, StructType

# -------------------------
# Configuration
# -------------------------
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC = os.environ.get("KAFKA_TOPIC", "bluesky_events")

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "dagster")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "dagster")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "dagster")
POSTGRES_TABLE = os.environ.get("POSTGRES_TABLE", "bsky_records")

CHECKPOINT_DIR = os.environ.get("CHECKPOINT_DIR", "/tmp/spark_checkpoints/bsky_records")
TRIGGER_INTERVAL = os.environ.get("TRIGGER_INTERVAL", "10 seconds")

# -------------------------
# Streaming backpressure
# -------------------------
# The Jetstream firehose runs ~4 orders of magnitude faster than the CoinGecko
# poller this replaced (0.17 events/sec vs. hundreds/sec, and Kafka retains 24h).
# Two settings kept that from being survivable:
#
#   1. No maxOffsetsPerTrigger. Structured Streaming will happily plan a single
#      micro-batch spanning every offset available at that moment, so the first
#      batch after any downtime tried to pull the whole backlog into one batch.
#      In a 2g container (see mem_limit on spark_consumer in compose) that OOMs
#      the driver long before it reaches the sink. Capping offsets per trigger
#      turns "catch up in one impossible batch" into "catch up over N bounded
#      batches", which is the only shape that fits a fixed memory budget.
#
#   2. startingOffsets=earliest. This only applies on a *cold* checkpoint --
#      after that the checkpoint's committed offsets win -- so it is purely a
#      first-boot policy. For a public firehose there is nothing to recover: the
#      value of the data is recency, and replaying 24h of retention on every
#      fresh volume costs hours of catch-up to land events nobody asked for.
#      Default is now `latest`; set KAFKA_STARTING_OFFSETS=earliest deliberately
#      when you actually want a backlog drain (now safe, just slow).
#
# The default cap is deliberately well above steady-state firehose rate, so it
# never throttles normal operation -- it only bounds the recovery batch.
MAX_OFFSETS_PER_TRIGGER = os.environ.get("MAX_OFFSETS_PER_TRIGGER", "20000")
STARTING_OFFSETS = os.environ.get("KAFKA_STARTING_OFFSETS", "latest")

# -------------------------
# JDBC sink tuning
# -------------------------
# Spark's JDBC writer defaults to batchsize=1000 and, without driver-side help,
# turns that into 1000 separate INSERT statements on one round trip. pgjdbc's
# reWriteBatchedInserts (note: this is the *Postgres* spelling -- MySQL's is
# rewriteBatchedStatements, which does nothing here) collapses them into a
# single multi-row INSERT, which is usually worth several times the throughput.
#
# Tradeoff: larger batches amortise round trips but hold more rows in the
# driver's JDBC buffer and lengthen the transaction, so a failure re-does more
# work. 1000 is left as the default on purpose -- it is Spark's own default and
# therefore a *fair* baseline. The point of the instrumentation below is to find
# out whether the sink is the bottleneck, and hand-tuning this to 10000 up front
# would hide the very thing being measured. Both knobs are the intended A/B
# dimensions for the benchmark.
JDBC_BATCH_SIZE = os.environ.get("JDBC_BATCH_SIZE", "1000")
JDBC_REWRITE_BATCHED_INSERTS = os.environ.get("JDBC_REWRITE_BATCHED_INSERTS", "true").lower() == "true"
# Number of concurrent JDBC connections. Unset means "one per Spark partition",
# which for a single-partition Kafka topic is one connection -- the third knob
# alongside batchsize, and the one to reach for if addBatch is sink-bound.
JDBC_NUM_PARTITIONS = os.environ.get("JDBC_NUM_PARTITIONS", "")

_JDBC_URL_BASE = f"jdbc:postgresql://{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
JDBC_URL = (
    f"{_JDBC_URL_BASE}?reWriteBatchedInserts=true"
    if JDBC_REWRITE_BATCHED_INSERTS
    else _JDBC_URL_BASE
)

# Postgres TEXT cannot store 0x00 and rejects the whole statement if any value
# contains one. Named rather than written inline: a literal NUL in source is
# invisible in an editor and survives a copy-paste as a silent corruption.
NUL = "\u0000"

# Marker key on every metric line. Stable and greppable -- see emit_metric().
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "spark.streaming")

# -------------------------
# Schema definition (best practice: always define explicit schemas)
# -------------------------
# The producer does not forward raw Jetstream events. It flattens them into a
# fixed envelope: routing and identity fields hoisted to the top level, and the
# record body passed through as an opaque JSON *string*. That is deliberate on
# its side and this schema has to match it exactly -- a mismatch here does not
# raise, it silently yields all-null structs and drops every row.
#
# The reason for the split is that record bodies have no stable shape. A post
# carries any combination of embeds, facets, langs and reply refs, and the
# firehose also carries third-party lexicons that no fixed schema could
# anticipate. Typing the envelope while leaving the body as text keeps the
# contract stable; anything that wants to go deeper parses the string itself,
# which is exactly what RECORD_SCHEMA below does for the handful of post fields
# this pipeline needs.
ENVELOPE_SCHEMA = StructType([
    StructField("did", StringType(), True),
    StructField("time_us", LongType(), True),
    StructField("kind", StringType(), True),
    StructField("operation", StringType(), True),
    StructField("collection", StringType(), True),
    StructField("rkey", StringType(), True),
    StructField("rev", StringType(), True),
    StructField("cid", StringType(), True),
    StructField("is_deleted", BooleanType(), True),
    StructField("record", StringType(), True),
    StructField("ingested_at", StringType(), True),
])

# Applied to the record string in a second pass. A *delete* is a key-only
# tombstone with no record at all, so the string is null; from_json leaves the
# struct null and text/created_at/langs come out null rather than blowing up the
# parse. Those rows must survive -- they are the delete half of the change log.
RECORD_SCHEMA = StructType([
    StructField("$type", StringType(), True),
    StructField("text", StringType(), True),
    StructField("createdAt", StringType(), True),
    StructField("langs", ArrayType(StringType()), True),
])

# -------------------------
# Postgres setup
# -------------------------
JDBC_PROPERTIES = {
    "user": POSTGRES_USER,
    "password": POSTGRES_PASSWORD,
    "driver": "org.postgresql.Driver",
}

def ensure_postgres_table(spark: SparkSession):
    """Create the target table if it doesn't exist using JDBC."""
    # Append-only change log: one row per commit event, deletes included. It is
    # never updated in place -- folding the log down to current state happens
    # later, in ClickHouse. Two columns are load-bearing for downstream and must
    # not move: `id` (BIGSERIAL, so monotonic) is the incremental high-water
    # mark a Dagster asset pages on, and `inserted_at` records landing time.
    #
    # WHY THE HIGH-WATER MARK ON `id` IS SAFE -- and what would break it.
    # A BIGSERIAL is monotonic in *allocation*, not in *commit*: two concurrent
    # transactions can take ids 100 and 101 and commit them in the other order.
    # A reader doing `WHERE id > :watermark` between those two commits sees 101,
    # advances its watermark past 100, and skips that row permanently. That is
    # the classic silent-gap failure of watermarking on a sequence.
    #
    # It cannot happen here because there is exactly one writer: the Kafka topic
    # has one partition (KAFKA_NUM_PARTITIONS=1 on the broker in
    # deployment/k8s/07-kafka.yaml, and the compose broker auto-creates with the
    # same default), so the streaming
    # DataFrame has one partition, so Spark's JDBC sink opens one connection and
    # commits batches serially. The safety comes from that -- not from the table
    # being append-only, which is a separate property.
    #
    # So it breaks the moment either of those changes: raising the topic's
    # partition count, or setting JDBC_NUM_PARTITIONS > 1. kafka_partition and
    # kafka_offset are recorded per row so that assumption is auditable rather
    # than folklore -- (partition, offset) is the real total order of the stream,
    # and it is what a per-partition watermark would key on if this ever needs to
    # scale past one writer. In production the answer is log-based CDC (Debezium
    # reading the WAL), where the LSN provides the commit order directly and the
    # sequence never has to carry it.
    #
    # No secondary indexes on purpose: every index is per-row write amplification
    # on the hot append path, and nothing reads this table except the id-ordered
    # watermark scan the primary key already serves.
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {POSTGRES_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            did TEXT NOT NULL,
            rkey TEXT,
            operation VARCHAR(16) NOT NULL,
            collection TEXT,
            text TEXT,
            created_at TIMESTAMP,
            langs TEXT[],
            event_time_us BIGINT NOT NULL,
            cid TEXT,
            is_deleted SMALLINT NOT NULL DEFAULT 0,
            kafka_partition INTEGER,
            kafka_offset BIGINT,
            inserted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
    # CREATE TABLE IF NOT EXISTS is a no-op against a table that already exists,
    # so a volume created before these columns were added would keep the old
    # shape and the JDBC append would fail on the first batch with "column
    # kafka_partition does not exist". ADD COLUMN IF NOT EXISTS closes that:
    # both statements are idempotent, so this is safe on a fresh volume and on
    # an upgraded one. Adding a nullable column with no default is a catalog-only
    # change in Postgres -- it does not rewrite the table.
    alters = [
        f"ALTER TABLE {POSTGRES_TABLE} ADD COLUMN IF NOT EXISTS is_deleted SMALLINT NOT NULL DEFAULT 0",
        f"ALTER TABLE {POSTGRES_TABLE} ADD COLUMN IF NOT EXISTS kafka_partition INTEGER",
        f"ALTER TABLE {POSTGRES_TABLE} ADD COLUMN IF NOT EXISTS kafka_offset BIGINT",
    ]
    # Still no secondary indexes, and that is the considered position rather than
    # an omission. Nothing here reads by anything but the primary key: the
    # incremental extract pages on `id > :watermark` and the sensor reads
    # MAX(id). A BRIN index on inserted_at was added and then removed for
    # exactly that reason -- it would be nearly free to maintain and correlate
    # perfectly with an append-only table's physical order, but no query wants
    # it. When retention lands and needs a time predicate, that is the moment to
    # add it back, with a reader to justify it.

    # Use a raw JDBC connection for DDL
    conn = (
        spark._jvm.java.sql.DriverManager
        .getConnection(JDBC_URL, POSTGRES_USER, POSTGRES_PASSWORD)
    )
    try:
        stmt = conn.createStatement()
        stmt.execute(ddl)
        for alter in alters:
            stmt.execute(alter)
        stmt.close()
        print(f"Ensured Postgres table: {POSTGRES_TABLE}")
    finally:
        conn.close()

# -------------------------
# Metrics
# -------------------------
def emit_metric(name: str, payload: dict):
    """Emit exactly one JSON object per line on stdout.

    The line is valid JSON *by itself* and its first key is a stable marker, so
    a whole benchmark run aggregates without any sed/cut in between:

        docker logs spark_consumer 2>&1 \\
          | grep '"metric":"spark.streaming.batch"' \\
          | jq -s 'map(.duration_ms.addBatch) | add / length'

    Kept small and free of user text so a line can never be split or contain a
    stray newline from a Bluesky post.
    """
    line = {"metric": f"{METRIC_NAMESPACE}.{name}"}
    line.update(payload)
    print(json.dumps(line, separators=(",", ":"), default=str), flush=True)

def _finite(value):
    """JSON has no NaN/Infinity. Spark reports both for the first batch's rates."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    return None

def _offset_total(offset):
    """Sum a Kafka offset map -- {"topic": {"0": 123, "1": 456}} -> 579."""
    if isinstance(offset, str):
        try:
            offset = json.loads(offset)
        except ValueError:
            return None
    if not isinstance(offset, dict):
        return None
    total = 0
    for partitions in offset.values():
        if not isinstance(partitions, dict):
            return None
        for value in partitions.values():
            if isinstance(value, (int, float)):
                total += int(value)
    return total

def _progress_to_dict(progress) -> dict:
    """Normalise a StreamingQueryProgress to a plain dict.

    Prefer Spark's own JSON rendering -- it is stable across versions and it
    already drops NaN rates -- and fall back to attribute access if the PySpark
    build doesn't expose it.
    """
    raw = getattr(progress, "json", None)
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass

    sources = []
    for source in getattr(progress, "sources", None) or []:
        sources.append({
            "description": getattr(source, "description", None),
            "startOffset": getattr(source, "startOffset", None),
            "endOffset": getattr(source, "endOffset", None),
            "latestOffset": getattr(source, "latestOffset", None),
            "numInputRows": getattr(source, "numInputRows", None),
        })
    return {
        "batchId": getattr(progress, "batchId", None),
        "timestamp": getattr(progress, "timestamp", None),
        "numInputRows": getattr(progress, "numInputRows", None),
        "inputRowsPerSecond": getattr(progress, "inputRowsPerSecond", None),
        "processedRowsPerSecond": getattr(progress, "processedRowsPerSecond", None),
        "durationMs": dict(getattr(progress, "durationMs", None) or {}),
        "sources": sources,
    }

class BatchMetricsListener(StreamingQueryListener):
    """One structured line per completed micro-batch, for the benchmark write-up.

    The question this exists to answer is "is the bottleneck the JDBC sink or
    the transform?". `durationMs.addBatch` is the discriminator: it covers
    everything foreachBatch does, so addBatch >> the rest means the sink (or the
    work feeding it) dominates, while a fat queryPlanning/latestOffset/walCommit
    tail means the overhead is per-batch rather than per-row. The sink splits
    that further -- see write_batch_to_postgres, which times the JDBC write on
    its own so it can be subtracted from addBatch.
    """

    def onQueryStarted(self, event):
        emit_metric("query_started", {"query_id": str(event.id), "run_id": str(event.runId), "name": event.name})

    def onQueryProgress(self, event):
        progress = _progress_to_dict(event.progress)
        durations = progress.get("durationMs") or {}

        # triggerExecution is the wall clock for the whole batch; addBatch is the
        # foreachBatch call inside it. Everything else is planning and offset
        # bookkeeping, which is per-batch cost and does not scale with rows.
        trigger_ms = _finite(durations.get("triggerExecution"))
        add_batch_ms = _finite(durations.get("addBatch"))
        overhead_ms = None
        add_batch_pct = None
        if trigger_ms is not None and add_batch_ms is not None:
            overhead_ms = trigger_ms - add_batch_ms
            add_batch_pct = round(100.0 * add_batch_ms / trigger_ms, 2) if trigger_ms else None

        sources = []
        for source in progress.get("sources") or []:
            end_total = _offset_total(source.get("endOffset"))
            latest_total = _offset_total(source.get("latestOffset"))
            sources.append({
                "description": source.get("description"),
                "num_input_rows": source.get("numInputRows"),
                "start_offset": _offset_total(source.get("startOffset")),
                "end_offset": end_total,
                "latest_offset": latest_total,
                # How far behind the head of the topic this batch left us. Only
                # meaningful once maxOffsetsPerTrigger is set -- without a cap
                # endOffset *is* latestOffset by construction and lag is always 0.
                "offset_lag": (latest_total - end_total)
                if (latest_total is not None and end_total is not None)
                else None,
            })

        emit_metric("batch", {
            "batch_id": progress.get("batchId"),
            "timestamp": progress.get("timestamp"),
            "num_input_rows": progress.get("numInputRows"),
            "input_rows_per_second": _finite(progress.get("inputRowsPerSecond")),
            "processed_rows_per_second": _finite(progress.get("processedRowsPerSecond")),
            "batch_duration_ms": trigger_ms,
            "add_batch_ms": add_batch_ms,
            "overhead_ms": overhead_ms,
            "add_batch_pct": add_batch_pct,
            "duration_ms": durations,
            "sources": sources,
        })

    def onQueryIdle(self, event):
        pass

    def onQueryTerminated(self, event):
        emit_metric("query_terminated", {"query_id": str(event.id), "run_id": str(event.runId), "exception": event.exception})

# -------------------------
# Sink
# -------------------------
def write_batch_to_postgres(batch_df, batch_id):
    """foreachBatch sink: write each micro-batch to Postgres via JDBC.

    The batch is cached before anything reads it. Without that, every action on
    batch_df (the row count, then the write) re-runs the Kafka read and the JSON
    parse from scratch -- the previous version counted rows *after* writing them
    and paid for the whole pipeline twice. Caching also splits the wall clock in
    two, which is the point: `transform_ms` is Kafka fetch + parse + projection
    materialised into cache, `jdbc_write_ms` is the sink reading from that cache.
    addBatch in the query progress is roughly their sum, so subtracting the two
    is what actually answers "sink or transform?".
    """
    batch_df.persist(StorageLevel.MEMORY_AND_DISK)
    try:
        transform_start = time.monotonic()
        row_count = batch_df.count()  # forces the batch into cache
        transform_ms = round((time.monotonic() - transform_start) * 1000.0, 1)

        jdbc_write_ms = 0.0
        if row_count:
            write_start = time.monotonic()
            writer = (
                batch_df.write
                .mode("append")
                .option("batchsize", JDBC_BATCH_SIZE)
            )
            if JDBC_NUM_PARTITIONS:
                writer = writer.option("numPartitions", JDBC_NUM_PARTITIONS)
            writer.jdbc(JDBC_URL, POSTGRES_TABLE, properties=JDBC_PROPERTIES)
            jdbc_write_ms = round((time.monotonic() - write_start) * 1000.0, 1)

        emit_metric("jdbc_write", {
            "batch_id": batch_id,
            "rows": row_count,
            "transform_ms": transform_ms,
            "jdbc_write_ms": jdbc_write_ms,
            "rows_per_second": round(row_count / (jdbc_write_ms / 1000.0), 1) if jdbc_write_ms else None,
            "batchsize": int(JDBC_BATCH_SIZE),
            "rewrite_batched_inserts": JDBC_REWRITE_BATCHED_INSERTS,
        })
    finally:
        batch_df.unpersist()

# -------------------------
# Spark session
# -------------------------
def create_spark_session() -> SparkSession:
    return (
        SparkSession.builder
        .appName("BlueskyJetstreamConsumer")
        .config("spark.sql.streaming.forceDeleteTempCheckpointLocation", "true")
        .config("spark.ui.enabled", "false")
        # Jetstream timestamps are UTC. Pin the session zone so `created_at`
        # doesn't shift with whatever the container's TZ happens to be.
        .config("spark.sql.session.timeZone", "UTC")
        # NB: driver heap is fixed when spark-submit launches the JVM, so setting
        # spark.driver.memory here would be a no-op. It comes from --driver-memory
        # in the image's CMD instead.
        .config("spark.jars", "/opt/spark/jars/postgresql-42.7.4.jar")
        .getOrCreate()
    )

# -------------------------
# Main
# -------------------------
def main():
    print(f"Starting Spark consumer: {BOOTSTRAP_SERVERS} / {TOPIC} -> {JDBC_URL}/{POSTGRES_TABLE}")
    print(f"startingOffsets={STARTING_OFFSETS} maxOffsetsPerTrigger={MAX_OFFSETS_PER_TRIGGER} trigger={TRIGGER_INTERVAL}")

    # Wait for Kafka to be ready
    time.sleep(10)

    spark = create_spark_session()
    spark.sparkContext.setLogLevel("WARN")
    spark.streams.addListener(BatchMetricsListener())

    ensure_postgres_table(spark)

    # Read from Kafka
    raw_stream = (
        spark.readStream
        .format("kafka")
        .option("kafka.bootstrap.servers", BOOTSTRAP_SERVERS)
        .option("subscribe", TOPIC)
        .option("startingOffsets", STARTING_OFFSETS)
        .option("maxOffsetsPerTrigger", MAX_OFFSETS_PER_TRIGGER)
        .option("failOnDataLoss", "false")
        .load()
    )

    # Parse the producer's envelope, then the record body inside it.
    #
    # `partition` and `offset` are Kafka source metadata, carried through both
    # projections rather than dropped at the first select. They are the stream's
    # true total order, and the downstream watermark is a Postgres BIGSERIAL that
    # only reproduces that order while there is a single writer -- see
    # ensure_postgres_table for why that holds today and what would break it.
    # Recording them makes the assumption checkable after the fact:
    #
    #   SELECT count(*) FROM bsky_records a JOIN bsky_records b
    #     ON a.kafka_partition = b.kafka_partition
    #    WHERE a.kafka_offset < b.kafka_offset AND a.id > b.id;
    #
    # Anything other than 0 means ids and stream order have diverged, and the
    # high-water-mark read has been silently skipping rows.
    parsed_stream = (
        raw_stream
        .select(
            from_json(col("value").cast("string"), ENVELOPE_SCHEMA).alias("evt"),
            col("partition").alias("kafka_partition"),
            col("offset").alias("kafka_offset"),
        )
        # Only commits carry repository writes. Unparseable JSON yields an
        # all-null struct under PERMISSIVE mode and is dropped by the same
        # predicate; the did/operation guards keep a malformed event from
        # violating a NOT NULL and failing the whole batch.
        .filter(
            (col("evt.kind") == "commit")
            & col("evt.did").isNotNull()
            & col("evt.operation").isNotNull()
        )
        .select(
            col("evt.did").alias("did"),
            col("evt.rkey").alias("rkey"),
            col("evt.operation").alias("operation"),
            col("evt.collection").alias("collection"),
            col("evt.time_us").alias("event_time_us"),
            col("evt.cid").alias("cid"),
            # Carried from the producer rather than derived downstream --
            # build_envelope already sets is_deleted = operation == "delete" and
            # the envelope declares it. Keeping it means neither warehouse loader
            # computes anything per row, which is what lets the same load run as
            # pandas locally and as SQL against the file in ClickHouse. It also
            # makes the landed Parquet self-describing about delete semantics, so
            # a replay needs no Python at all.
            #
            # SMALLINT/short rather than boolean so it is 0/1 the whole way to
            # ClickHouse's UInt8 with no cast on either path. coalesce guards the
            # column's NOT NULL: a commit always carries the flag, but a
            # malformed envelope that still passes the kind/did/operation filter
            # would otherwise fail the whole batch on one bad row.
            coalesce(col("evt.is_deleted"), lit(False)).cast("short").alias("is_deleted"),
            "kafka_partition", "kafka_offset",
            # Second pass over the opaque body. Null in, null out, which is the
            # delete case.
            from_json(col("evt.record"), RECORD_SCHEMA).alias("rec"),
        )
        .select(
            "did", "rkey", "operation", "collection",
            # Null for deletes -- the record is absent, not empty.
            #
            # NUL bytes are stripped because Postgres TEXT cannot store 0x00 and
            # rejects the value with:
            #   invalid byte sequence for encoding "UTF8": 0x00
            # That is not a row-level failure. The JDBC sink writes the whole
            # micro-batch as one statement, so a single post containing a NUL
            # fails all 1000 rows, the batch never commits, the streaming query
            # terminates, and ingestion stops until someone restarts it. One
            # event in ~340k did exactly that.
            #
            # Stripping rather than dropping the row: the NUL is almost always
            # incidental (a client emitting a stray terminator), and the rest of
            # the post is legitimate content a reader should still see. Applied
            # to the two fields carrying arbitrary user data -- the envelope's
            # did/rkey/cid/collection are AT Protocol identifiers with
            # constrained character sets.
            translate(col("rec.text"), NUL, "").alias("text"),
            # createdAt is client-supplied, can be skewed hours from the firehose
            # timestamp, and can be anything at all; a bad value parses to null
            # rather than failing the batch. event_time_us is the event time to
            # actually reason with.
            to_timestamp(col("rec.createdAt")).alias("created_at"),
            # Same treatment for the array's elements. Language tags should be
            # BCP-47 codes, but the firehose carries third-party lexicons and
            # this field is whatever the client put there.
            transform(col("rec.langs"), lambda tag: translate(tag, NUL, "")).alias("langs"),
            "event_time_us", "cid", "is_deleted",
            "kafka_partition", "kafka_offset",
        )
    )

    # Write using foreachBatch (best practice for custom sinks)
    query = (
        parsed_stream.writeStream
        .foreachBatch(write_batch_to_postgres)
        .option("checkpointLocation", CHECKPOINT_DIR)
        .trigger(processingTime=TRIGGER_INTERVAL)
        .start()
    )

    print("Streaming query started, waiting for termination...")
    query.awaitTermination()

if __name__ == "__main__":
    main()
