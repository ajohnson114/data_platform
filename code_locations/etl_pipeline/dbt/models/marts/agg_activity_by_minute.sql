{{
  config(
    materialized = 'incremental',
    incremental_strategy = 'delete+insert',
    unique_key = 'minute',
    engine = 'MergeTree()',
    order_by = '(minute)',
    tags = ['marts'],
  )
}}

-- Activity per minute. The only incremental model here, and the only one where
-- being incremental is worth the complexity: the record stream grows by tens of
-- millions of rows a day, and re-aggregating all of history every run to add
-- sixty seconds of it is the definition of work that should not be repeated.
--
-- WHY delete+insert AND NOT append.
-- The trailing minute is always partial -- the run happens mid-minute and more
-- events for it are still arriving. Appending would write that undercount once
-- and never revisit it, leaving a permanent dent in the series at every run
-- boundary, which is worse than a missing value because it looks like real data.
-- delete+insert removes the minutes in range and rewrites them, so a partial
-- minute is corrected by the next run instead of being frozen.
--
-- WHY A LOOKBACK RATHER THAN max(minute).
-- Filtering on `> max(minute)` would be the obvious incremental predicate and it
-- is wrong twice: it never revisits the partial trailing minute, and it drops
-- late arrivals outright. The pipeline can be minutes behind after an outage
-- (the landing extract is capped at landing_batch_size per run and drains a
-- backlog over several), so rows for an already-summarised minute do arrive.
-- Reprocessing a window absorbs both.
--
-- The window is wider than the observed lag on purpose. Too narrow silently
-- loses late rows; too wide costs a few extra minutes of re-aggregation per run.
-- Only one of those failure modes is detectable after the fact.

{% set lookback_minutes = 15 %}

select
    toStartOfMinute(event_at)                                       as minute,

    count()                                                         as records,
    countIf(collection = 'app.bsky.feed.post')                      as posts,
    countIf(collection = 'app.bsky.feed.like')                      as likes,
    countIf(collection = 'app.bsky.feed.repost')                    as reposts,
    countIf(collection = 'app.bsky.graph.follow')                   as follows,

    uniqExact(did)                                                  as active_authors,

    countIf(collection = 'app.bsky.feed.post' and length(langs) > 1) as multilingual_posts,
    round(avgIf(length(coalesce(text, '')),
                collection = 'app.bsky.feed.post'), 1)              as avg_post_length,

    -- Records here are current state, so a row that was deleted upstream is
    -- already gone rather than counted. This is the count of what still exists
    -- for that minute, not what was originally published in it.
    max(landed_at)                                                  as last_landed_at

from {{ ref('stg_bsky_records') }}

{% if is_incremental() %}
where event_at >= (
    select coalesce(max(minute), toDateTime64(0, 3)) - interval {{ lookback_minutes }} minute
    from {{ this }}
)
{% endif %}

group by minute
