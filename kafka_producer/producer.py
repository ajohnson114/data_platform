"""Bluesky Jetstream -> Kafka producer.

Jetstream is a WebSocket firehose of AT Protocol repository commits, emitted as
plain JSON (no CBOR/CAR decoding, no auth). It replaces the previous CoinGecko
polling producer, which sourced ~0.17 events/sec; a Jetstream subscription
across the default collections sustains ~300 events/sec, and a cursor replay
bursts to several thousand/sec. See the notes on JETSTREAM_CURSOR_START below --
replay is the load-test lever for this platform.
"""

import asyncio
import json
import os
import random
import signal
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import websockets
from confluent_kafka import Producer
from websockets.asyncio.client import connect

# -------------------------
# Configuration
# -------------------------
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC = os.environ.get("KAFKA_TOPIC", "bluesky_events")

# Public Jetstream instances, any of which will serve the full firehose:
#   jetstream1.us-east / jetstream2.us-east / jetstream1.us-west / jetstream2.us-west
JETSTREAM_HOST = os.environ.get("JETSTREAM_HOST", "jetstream2.us-east.bsky.network")

# Server-side collection filter. Repeated wantedCollections params (not a comma
# separated value) are what Jetstream actually parses, so this is split here and
# re-encoded one param per entry. Set to an empty string to subscribe to every
# collection. Measured rates against jetstream2.us-east:
#   app.bsky.feed.post alone ......  ~34 events/sec
#   the four defaults below ....... ~290 events/sec
#   unfiltered ................... ~300 events/sec (likes are ~63% of the firehose)
JETSTREAM_COLLECTIONS = os.environ.get(
    "JETSTREAM_COLLECTIONS",
    "app.bsky.feed.post,app.bsky.feed.repost,app.bsky.feed.like,app.bsky.graph.follow",
)

# Explicit start cursor as a microsecond timestamp (Jetstream's time_us).
#
# This is the load-test lever. Jetstream replays from a past cursor as fast as
# the client can consume, so starting from a timestamp N minutes in the past
# makes the firehose deliver that backlog at maximum rate -- a real load test of
# the Kafka -> Spark -> warehouse path with real data, instead of a synthetic
# generator. Measured: a 30-minute backlog of app.bsky.feed.post drains in ~13s,
# peaking near 5,500 events/sec, i.e. ~31x realtime.
#
# JETSTREAM_REPLAY_MINUTES is the same lever expressed relatively, which is
# easier to use ad hoc: JETSTREAM_REPLAY_MINUTES=30 means "start 30 minutes ago".
# Jetstream retains roughly 36 hours; older cursors are silently clamped to the
# oldest retained event rather than rejected, and cursor=0 means "oldest
# retained", NOT "beginning of time".
JETSTREAM_CURSOR_START = os.environ.get("JETSTREAM_CURSOR_START", "")
JETSTREAM_REPLAY_MINUTES = os.environ.get("JETSTREAM_REPLAY_MINUTES", "")

# Cursor is persisted here so a container restart resumes instead of skipping
# ahead to live. Mount a volume at this path to survive container re-creation.
CURSOR_FILE = os.environ.get("JETSTREAM_CURSOR_FILE", "/app/state/cursor.json")
CURSOR_PERSIST_INTERVAL = float(os.environ.get("CURSOR_PERSIST_INTERVAL_SECONDS", "5"))

# On reconnect, rewind the cursor slightly. Duplicates are cheap -- the warehouse
# dedupes on key + version (did/rkey + time_us) -- but a gap is unrecoverable, so
# the rewind is deliberately biased toward replaying events we already sent.
CURSOR_REWIND_SECONDS = float(os.environ.get("CURSOR_REWIND_SECONDS", "5"))

# Reconnect backoff. The firehose drops connections routinely; the cap keeps a
# sustained outage from spinning hot.
BACKOFF_INITIAL = float(os.environ.get("RECONNECT_BACKOFF_INITIAL_SECONDS", "1"))
BACKOFF_MAX = float(os.environ.get("RECONNECT_BACKOFF_MAX_SECONDS", "60"))

# A read that stalls this long is treated as a dead connection. Reconnecting is
# free here: the cursor makes resumption exact, so a false positive costs only a
# handshake and a few duplicate events.
RECV_TIMEOUT = float(os.environ.get("JETSTREAM_RECV_TIMEOUT_SECONDS", "30"))

STATS_INTERVAL = float(os.environ.get("STATS_INTERVAL_SECONDS", "10"))
FLUSH_TIMEOUT = float(os.environ.get("KAFKA_FLUSH_TIMEOUT_SECONDS", "30"))

# identity and account events describe an account's state, not a repository
# record: they carry no collection and no rkey, so they don't map onto the
# record-shaped warehouse table. They arrive even when wantedCollections is set
# (the filter only applies to commits) and are ~1.7% of the stream. Off by
# default; set to "true" to forward them with null collection/rkey.
INCLUDE_ACCOUNT_EVENTS = os.environ.get("JETSTREAM_INCLUDE_ACCOUNT_EVENTS", "false").lower() == "true"

running = True

# Latest time_us seen, i.e. our position in the firehose. This is module-level
# rather than a return value from the read loop on purpose: the read loop
# normally exits by raising (a dropped connection is the expected case, not the
# exceptional one), and a returned cursor would be discarded on exactly that
# path -- leaving the reconnect to resume from live and silently skip the
# outage window.
cursor = 0

# Set once the event loop is up. The signal handler uses these to close the live
# socket from the loop thread; without that, a shutdown arriving during a quiet
# moment would sit parked in recv() until the next event or the read timeout.
_loop = None
_websocket = None

def signal_handler(sig, frame):
    global running
    print(f"Received signal {sig}, shutting down gracefully...", flush=True)
    running = False
    if _loop is not None and _websocket is not None:
        _loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_websocket.close()))

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# -------------------------
# Kafka producer setup
# -------------------------
def create_producer() -> Producer:
    config = {
        "bootstrap.servers": BOOTSTRAP_SERVERS,
        "acks": "all",
        "enable.idempotence": True,
        "retries": 5,
        "retry.backoff.ms": 500,
        # At 300+ events/sec linger actually earns its keep: it turns a stream of
        # single-record requests into batches of ~30, which is what makes acks=all
        # affordable at this volume.
        "linger.ms": 100,
        "compression.type": "snappy",
        # Sized to fit the producer's container memory limit. Filling this queue
        # is the backpressure signal -- see publish() -- and the first thing to
        # watch for when hunting the platform's degradation point.
        "queue.buffering.max.messages": 100000,
        "queue.buffering.max.kbytes": 65536,
    }
    return Producer(config)

def delivery_callback(err, msg):
    """Delivery report handler.

    The previous version printed one line per delivered message. That is fine at
    0.17 events/sec and catastrophic at 300/sec -- the log becomes the
    bottleneck and buries everything useful. Successes are counted and reported
    by the periodic throughput log instead; only failures print, and only the
    first few, since a broker outage fails every in-flight message at once.
    """
    if err is not None:
        stats.delivery_errors += 1
        if stats.delivery_errors <= 5:
            print(f"Message delivery failed: {err}", flush=True)
        elif stats.delivery_errors == 6:
            print("Further delivery failures suppressed; see periodic stats.", flush=True)
    else:
        stats.delivered += 1

# -------------------------
# Throughput accounting
# -------------------------
class Stats:
    """Counters for the periodic throughput log.

    This log is the data source for the benchmark write-up, so it reports rate
    and stream lag rather than raw totals only: lag is how you tell "keeping up
    with live" from "still draining a replay backlog".
    """

    def __init__(self):
        self.start = time.monotonic()
        self.last_report = self.start
        self.total = 0
        self.window = 0
        self.delivered = 0
        self.delivery_errors = 0
        self.buffer_full = 0
        self.dropped = 0
        self.reconnects = 0
        self.by_operation = {}

    def record(self, operation: str):
        self.total += 1
        self.window += 1
        self.by_operation[operation] = self.by_operation.get(operation, 0) + 1

    def maybe_report(self, last_time_us: int):
        now = time.monotonic()
        elapsed = now - self.last_report
        if elapsed < STATS_INTERVAL:
            return
        # How far behind the live edge of the firehose we are. ~0 means keeping
        # up; a large positive value means we're replaying a backlog.
        lag = (time.time() * 1_000_000 - last_time_us) / 1_000_000 if last_time_us else 0.0
        ops = " ".join(f"{k}={v}" for k, v in sorted(self.by_operation.items()))
        print(
            f"[stats] {self.window / elapsed:.1f} evt/s over {elapsed:.1f}s | "
            f"total={self.total} delivered={self.delivered} "
            f"lag={lag:.1f}s errors={self.delivery_errors} "
            f"buffer_full={self.buffer_full} dropped={self.dropped} "
            f"reconnects={self.reconnects} | {ops}",
            flush=True,
        )
        self.window = 0
        self.last_report = now

stats = Stats()

# -------------------------
# Cursor persistence
# -------------------------
def load_cursor() -> int:
    """Resolve the start cursor: explicit override, then replay window, then disk.

    Returning 0 means "no cursor", which Jetstream treats as "start from live".
    """
    if JETSTREAM_CURSOR_START:
        cursor = int(JETSTREAM_CURSOR_START)
        print(f"Start cursor from JETSTREAM_CURSOR_START: {cursor}", flush=True)
        return cursor

    if JETSTREAM_REPLAY_MINUTES:
        minutes = float(JETSTREAM_REPLAY_MINUTES)
        cursor = int((time.time() - minutes * 60) * 1_000_000)
        print(f"Replay mode: starting {minutes} minutes back at cursor {cursor}", flush=True)
        return cursor

    try:
        with open(CURSOR_FILE) as fh:
            cursor = int(json.load(fh)["cursor"])
        age = (time.time() * 1_000_000 - cursor) / 1_000_000
        print(f"Resuming from persisted cursor {cursor} ({age:.1f}s old)", flush=True)
        return cursor
    except (OSError, ValueError, KeyError, TypeError):
        print("No usable persisted cursor; starting from the live edge.", flush=True)
        return 0

def save_cursor(cursor: int):
    """Persist atomically so a crash mid-write can't leave a truncated cursor."""
    if not cursor:
        return
    try:
        os.makedirs(os.path.dirname(CURSOR_FILE) or ".", exist_ok=True)
        tmp = f"{CURSOR_FILE}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"cursor": cursor}, fh)
        os.replace(tmp, CURSOR_FILE)
    except OSError as e:
        # A read-only or missing volume shouldn't kill the producer; it only
        # costs us resumption across a restart.
        print(f"Could not persist cursor: {e}", flush=True)

# -------------------------
# Jetstream event handling
# -------------------------
def build_subscribe_url(cursor: int) -> str:
    params = [("wantedCollections", c.strip()) for c in JETSTREAM_COLLECTIONS.split(",") if c.strip()]
    if cursor:
        params.append(("cursor", str(cursor)))
    query = urlencode(params)
    return f"wss://{JETSTREAM_HOST}/subscribe" + (f"?{query}" if query else "")

def build_envelope(event: dict) -> dict:
    """Flatten a Jetstream event into a fixed-shape envelope for Spark.

    The routing and identity fields are hoisted to the top level so the consumer
    can declare an explicit schema over them, and the record body is passed
    through as an opaque JSON string. That is deliberate: record bodies have no
    stable shape -- a post carries any combination of embeds, facets, langs and
    reply refs, and the unfiltered firehose also carries third-party lexicons
    (place.stream.livestream, bridgy's fields) that no fixed schema could
    anticipate. Typing the envelope and stringifying the body keeps the contract
    stable while leaving the full record available to anyone who wants to parse
    deeper.

    Returns None for events that are filtered out.
    """
    kind = event.get("kind")
    did = event.get("did")
    time_us = event.get("time_us")
    if not did or not time_us:
        return None

    if kind == "commit":
        commit = event.get("commit") or {}
        operation = commit.get("operation")
        # Deletes carry the key only -- no record body and no cid. That's the
        # whole reason the warehouse has an is_deleted column, so they're
        # forwarded alongside creates and updates rather than filtered out.
        record = commit.get("record")
        return {
            "did": did,
            "time_us": time_us,
            "kind": kind,
            "operation": operation,
            "collection": commit.get("collection"),
            "rkey": commit.get("rkey"),
            "rev": commit.get("rev"),
            "cid": commit.get("cid"),
            "is_deleted": operation == "delete",
            "record": json.dumps(record, separators=(",", ":")) if record is not None else None,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

    if not INCLUDE_ACCOUNT_EVENTS:
        return None

    return {
        "did": did,
        "time_us": time_us,
        "kind": kind,
        "operation": None,
        "collection": None,
        "rkey": None,
        "rev": None,
        "cid": None,
        "is_deleted": False,
        "record": json.dumps(event.get(kind), separators=(",", ":")),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

def publish(producer: Producer, envelope: dict):
    """Publish keyed by did.

    Keying on the repository DID puts every event from one account on the same
    partition, so a create and its later delete stay ordered relative to each
    other no matter how many partitions the topic grows to.
    """
    payload = json.dumps(envelope, separators=(",", ":"))
    # Deliberately not `while running:` -- a signal landing between entry and the
    # loop check would drop the message without ever attempting a produce. Only
    # the retry path yields to shutdown.
    while True:
        try:
            producer.produce(
                topic=TOPIC,
                key=envelope["did"],
                value=payload,
                callback=delivery_callback,
            )
            return
        except BufferError:
            # The local queue is full: Kafka is not draining as fast as Jetstream
            # is delivering. Blocking here is the correct response -- it stops
            # reading the socket, which pushes back through TCP to Jetstream --
            # and it's precisely the degradation point the replay load test
            # exists to find, so it's counted rather than silently absorbed.
            stats.buffer_full += 1
            producer.poll(0.1)
            if not running:
                # Shutting down against a full queue: give up on enqueueing more
                # so flush() can drain what's already buffered inside its timeout.
                stats.dropped += 1
                return

# -------------------------
# Stream loop
# -------------------------
async def stream_once(producer: Producer, start_cursor: int):
    """Consume one connection's worth of events, advancing the module-level cursor."""
    global _websocket, cursor

    url = build_subscribe_url(start_cursor)
    print(f"Connecting to {url}", flush=True)

    last_persist = time.monotonic()
    since_poll = 0
    connected_at = None

    # max_size guards against a pathological frame; Jetstream's are a few KB.
    async with connect(url, max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=20) as ws:
        _websocket = ws
        connected_at = time.monotonic()
        print("Connected to Jetstream.", flush=True)

        while running:
            # The timeout doubles as stall detection. Cancelling recv() here is
            # safe because the only thing we do afterwards is drop the
            # connection and resume from the cursor.
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)

            event = json.loads(raw)
            time_us = event.get("time_us")
            if time_us:
                cursor = time_us

            envelope = build_envelope(event)
            if envelope is not None:
                publish(producer, envelope)
                stats.record(envelope["operation"] or envelope["kind"])

            # produce() only queues; poll() is what serves delivery callbacks.
            # Batching the call keeps it off the per-message hot path.
            since_poll += 1
            if since_poll >= 100:
                producer.poll(0)
                since_poll = 0

            now = time.monotonic()
            if now - last_persist >= CURSOR_PERSIST_INTERVAL:
                save_cursor(cursor)
                last_persist = now

            stats.maybe_report(cursor)

    if connected_at is not None:
        print(f"Connection closed after {time.monotonic() - connected_at:.1f}s.", flush=True)

async def run(producer: Producer):
    global _loop, _websocket, cursor

    _loop = asyncio.get_running_loop()
    cursor = load_cursor()
    backoff = BACKOFF_INITIAL

    while running:
        # Rewind before reconnecting so the replayed window overlaps what we
        # already sent. Duplicates are absorbed downstream; a gap would not be.
        resume_from = cursor - int(CURSOR_REWIND_SECONDS * 1_000_000) if cursor else 0
        attempt_started = time.monotonic()

        try:
            await stream_once(producer, resume_from)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            print(f"Stream stalled: no event for {RECV_TIMEOUT:.0f}s, treating as dead.", flush=True)
        except (websockets.exceptions.WebSocketException, OSError) as e:
            print(f"Stream error ({type(e).__name__}): {e}", flush=True)
        except Exception as e:
            print(f"Unexpected stream error ({type(e).__name__}): {e}", flush=True)
        finally:
            _websocket = None
            save_cursor(cursor)

        if not running:
            break

        stats.reconnects += 1

        # Reset the backoff only if the last attempt actually stayed up. A server
        # that accepts the handshake and immediately closes would otherwise reset
        # the backoff every time and turn this into a hot loop.
        if time.monotonic() - attempt_started >= 30:
            backoff = BACKOFF_INITIAL

        # Jitter so a fleet of producers doesn't reconnect in lockstep.
        delay = backoff * (0.5 + random.random() * 0.5)
        print(f"Reconnecting in {delay:.1f}s (attempt backoff {backoff:.0f}s)", flush=True)

        waited = 0.0
        while running and waited < delay:
            await asyncio.sleep(min(0.5, delay - waited))
            waited += 0.5

        backoff = min(backoff * 2, BACKOFF_MAX)

# -------------------------
# Main
# -------------------------
def main():
    print(f"Starting Jetstream producer: {JETSTREAM_HOST} -> {BOOTSTRAP_SERVERS}/{TOPIC}", flush=True)
    print(f"Collections: {JETSTREAM_COLLECTIONS or '(all)'}", flush=True)
    print(f"Cursor file: {CURSOR_FILE} (rewind {CURSOR_REWIND_SECONDS}s on reconnect)", flush=True)

    producer = create_producer()

    try:
        asyncio.run(run(producer))
    finally:
        # Flush before exiting so an in-flight batch isn't lost. linger.ms means
        # there is almost always something queued at these rates.
        print("Flushing producer...", flush=True)
        remaining = producer.flush(FLUSH_TIMEOUT)
        if remaining:
            print(f"WARNING: {remaining} messages still queued after {FLUSH_TIMEOUT}s flush timeout", flush=True)
        save_cursor(cursor)

    elapsed = time.monotonic() - stats.start
    print(
        f"Producer shut down. {stats.total} events in {elapsed:.1f}s "
        f"({stats.total / elapsed if elapsed else 0:.1f} evt/s), "
        f"delivered={stats.delivered} errors={stats.delivery_errors} "
        f"dropped={stats.dropped} reconnects={stats.reconnects} final_cursor={cursor}",
        flush=True,
    )

if __name__ == "__main__":
    main()
