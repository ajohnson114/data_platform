{{
  config(
    materialized = 'table',
    engine = 'MergeTree()',
    order_by = '(event_at, did)',
    tags = ['marts'],
  )
}}

-- Posts, and only posts.
--
-- This model exists because of a real incident rather than for tidiness. The
-- NL-to-SQL service pointed an LLM at the raw snapshot, whose name says "posts"
-- but which holds likes, reposts and follows too -- posts are about one row in
-- eight. "How many posts in the last ten minutes" came back 109,436 instead of
-- 14,942: a plausible integer, confidently explained, wrong by 7x. The fix at
-- the time was to warn the model in its prompt. This is the better fix: a table
-- where one row is one post, so being right is the default rather than
-- something a prompt has to remember to ask for.
--
-- Ordered by (event_at, did) because every real query here is a time window --
-- the ordering key is what makes those read a few granules instead of the table.

select
    record_uri,
    did,
    rkey,
    event_at,
    event_time_us,
    client_created_at,
    text,
    langs,

    length(coalesce(text, ''))                as text_length,
    length(langs)                             as language_count,
    arrayElement(langs, 1)                    as primary_language,

    -- A post whose declared languages disagree with its content is a common
    -- spam signal, and multi-language declarations are the cheap first cut.
    length(langs) > 1                         as is_multilingual,
    coalesce(text, '') = ''                   as is_empty,

    source_file,
    landed_at,
    ingest_id

from {{ ref('stg_bsky_records') }}
where collection = 'app.bsky.feed.post'
