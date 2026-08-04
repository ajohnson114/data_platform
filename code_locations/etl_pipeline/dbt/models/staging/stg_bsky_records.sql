{{
  config(
    materialized = 'view',
    tags = ['staging'],
  )
}}

-- The only model that reads the raw snapshot, and the only place FINAL appears.
--
-- FINAL is what makes a ReplacingMergeTree correct to read: without it a record
-- edited or deleted since it was first loaded is still present under its old
-- version, because deduplication happens at merge time rather than on write.
-- Doing it once here means no downstream model can forget, and the cost is paid
-- once per query plan instead of once per model.
--
-- A view rather than a table on purpose. Materialising this would freeze a copy
-- of the whole record stream and put a staleness window in front of every mart,
-- for a transformation that is a cast and a rename.

select
    did,
    collection,
    rkey,

    -- The AT-URI is the record's real identity. Built here so downstream models
    -- and anything querying the marts have one key to join on rather than
    -- reassembling three columns.
    concat('at://', did, '/', collection, '/', rkey)          as record_uri,

    operation,
    text,
    langs,

    -- event_time_us is the firehose's own clock and the only trustworthy one
    -- here: created_at is stamped by the author's client and is routinely hours
    -- out in either direction. Kept below for anyone who needs what the client
    -- claimed, but nothing should filter or order on it.
    toDateTime64(event_time_us / 1000000, 3)                  as event_at,
    event_time_us,
    created_at                                                as client_created_at,

    -- Provenance, carried through from the load so a mart row can still be
    -- traced back to the Parquet file it arrived in.
    _source_file                                              as source_file,
    _landed_at                                                as landed_at,
    id                                                        as ingest_id

from {{ source('warehouse', 'bsky_records_snapshot') }} final
where did != ''
