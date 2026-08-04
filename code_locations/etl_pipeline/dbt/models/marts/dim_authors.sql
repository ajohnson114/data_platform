{{
  config(
    materialized = 'table',
    engine = 'MergeTree()',
    order_by = '(did)',
    tags = ['marts'],
  )
}}

-- One row per account seen in the window, with what it did.
--
-- Deliberately built from the staging model rather than from fct_posts: an
-- account that only likes and follows never appears in fct_posts, and leaving it
-- out would make "how many accounts are active" answer a narrower question than
-- it looks like it answers.

select
    did,

    countIf(collection = 'app.bsky.feed.post')      as posts,
    countIf(collection = 'app.bsky.feed.like')      as likes,
    countIf(collection = 'app.bsky.feed.repost')    as reposts,
    countIf(collection = 'app.bsky.graph.follow')   as follows,
    count()                                         as total_records,

    min(event_at)                                   as first_seen_at,
    max(event_at)                                   as last_seen_at,

    -- Posting rather than reacting. Accounts near 0 are consumers; near 1 are
    -- broadcasters, and at exactly 1 with high volume, usually bots.
    round(
        countIf(collection = 'app.bsky.feed.post') / nullIf(count(), 0),
        4
    )                                               as post_share,

    countIf(collection = 'app.bsky.feed.post' and length(langs) > 1) as multilingual_posts

from {{ ref('stg_bsky_records') }}
group by did
