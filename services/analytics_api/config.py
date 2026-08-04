import os
from collections import namedtuple

CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "analytics")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "analytics_ro")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "analytics_ro")

WAREHOUSE = CLICKHOUSE_DATABASE

# -------------------------
# The tables the model is allowed to see
# -------------------------
# This used to be a single POSTS_TABLE pointing at the raw snapshot, with the
# system prompt carrying a paragraph explaining that the table called "records"
# is not one row per post. That paragraph was the fix for a real bug -- "how
# many posts in the last ten minutes" came back 109,436 instead of 14,942 -- and
# it worked, in the sense that a warning the model has to remember ever works.
#
# The dbt marts are the better fix, so the service now reads them. `fct_posts`
# is one row per post, which makes the original bug unrepresentable rather than
# warned against: there is no filter to forget because there is nothing else in
# the table.
#
# Ordered by how often they should win. The model picks from this list, so the
# order and the `use` line are doing the routing.
# time_column is part of the registry rather than a sentence in the prompt, and
# that is the whole fix for a specific bug. The prompt used to say "FOR ANYTHING
# TIME-RELATED USE event_at" globally -- but dim_authors has no event_at. Its
# grain is one row per account, so it carries first_seen_at/last_seen_at
# instead. "Which authors posted in the last ten minutes" therefore routed the
# model to a table and then named a column that table does not have.
#
# Same lesson as fct_posts, one level up: the registry describes each table, so
# anything the prompt says about a table belongs in the registry beside it. A
# prompt cannot name a column that is not declared here, and a test asserts
# every time_column actually appears in its own schema block.
Table = namedtuple("Table", "name grain use schema time_column")

TABLES = [
    Table(
        name=f"{WAREHOUSE}.fct_posts",
        grain="one row per post",
        use="anything about posts: counts, text, length, language.",
        time_column="event_at",
        schema="""
            record_uri        String                  -- at://<did>/<collection>/<rkey>
            did               String                  -- author's decentralised identifier
            rkey              String                  -- record key
            event_at          DateTime64(3)           -- firehose clock; use THIS for time
            event_time_us     Int64                   -- same instant, microseconds
            client_created_at Nullable(DateTime64(9)) -- author's own clock; UNRELIABLE
            text              String                  -- post body
            langs             Array(String)           -- declared language codes, e.g. ['en']
            text_length       UInt64                  -- characters in text
            language_count    UInt64                  -- length(langs)
            primary_language  String                  -- first declared language
            is_multilingual   UInt8                   -- langs has more than one entry
            is_empty          UInt8                   -- text is empty
        """,
    ),
    Table(
        name=f"{WAREHOUSE}.agg_activity_by_minute",
        grain="one row per minute",
        use=(
            "volume over time, and any comparison BETWEEN record types "
            "(posts vs likes vs reposts vs follows). Pre-aggregated, so prefer "
            "it over counting rows whenever the question is about volume."
        ),
        time_column="minute",
        schema="""
            minute              DateTime  -- minute bucket, firehose clock
            records             UInt64    -- all record types in that minute
            posts               UInt64
            likes               UInt64
            reposts             UInt64
            follows             UInt64
            active_authors      UInt64    -- distinct accounts seen that minute
            multilingual_posts  UInt64
            avg_post_length     Float64   -- mean characters, posts only
        """,
    ),
    Table(
        name=f"{WAREHOUSE}.dim_authors",
        grain="one row per account",
        use=(
            "an account's totals over all time: most active, most prolific, "
            "bot-shaped. It has NO per-event timestamp, so it cannot answer "
            "'who posted in the last N minutes' -- use fct_posts and group by "
            "did for that."
        ),
        # Not event_at, which is the reason this field exists. These bound when
        # an account was seen; they do not date the individual records, so a
        # window filter on last_seen_at answers "which accounts were active in
        # the window", not "how much did they do in it".
        time_column="last_seen_at",
        schema="""
            did                 String
            posts               UInt64
            likes               UInt64
            reposts             UInt64
            follows             UInt64
            total_records       UInt64
            first_seen_at       DateTime64(3)
            last_seen_at        DateTime64(3)
            post_share          Nullable(Float64) -- posts / total_records; ~0 consumer, ~1 broadcaster
            multilingual_posts  UInt64
        """,
    ),
    Table(
        name=f"{WAREHOUSE}.stg_bsky_records",
        grain="one row per AT Protocol record, ALL types",
        use=(
            "the fallback, for questions the three tables above cannot answer. "
            "Reaching for it means filtering on `collection` yourself -- see the "
            "warning below."
        ),
        time_column="event_at",
        schema="""
            did               String
            collection        String                  -- 'app.bsky.feed.post' | '...like' | '...repost' | 'app.bsky.graph.follow'
            rkey              String
            record_uri        String
            operation         Nullable(String)        -- 'create' or 'update'
            text              Nullable(String)        -- NULL for every collection except post
            langs             Array(String)
            event_at          DateTime64(3)           -- firehose clock; use THIS for time
            event_time_us     Int64
            client_created_at Nullable(DateTime64(9)) -- author's own clock; UNRELIABLE
        """,
    ),
]

# Where the two clocks come from. The marts are rebuilt on a freshness policy
# and the raw stream lands every 60 seconds, so they are not the same instant
# and the model is told both -- see generate_sql.
MART_CLOCK = f"SELECT max(minute) FROM {WAREHOUSE}.agg_activity_by_minute"
STREAM_CLOCK = f"SELECT max(event_at) FROM {WAREHOUSE}.stg_bsky_records"

# Offered in the UI dropdown. Nothing validates these against the provider --
# the key is supplied at request time and an unknown id simply fails the call --
# so the list is only as current as the last time someone looked.
PROVIDER_MODELS = {
    "openai": ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4", "gpt-5.5"],
    # Claude 5 family, plus Haiku 4.5 as the cheap option. Sonnet leads because
    # it is the sensible default for this workload: schema-grounded SQL over a
    # handful of columns is not a reasoning-heavy task, and the round trip is
    # user-facing.
    "anthropic": [
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
    ],
}

DEFAULT_PROVIDER = "openai"
