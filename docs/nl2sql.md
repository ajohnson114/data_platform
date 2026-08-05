# Conversational Analytics Interface

An optional NL-to-SQL service that exposes the ClickHouse warehouse through a natural language interface. The data it queries was ingested through the live streaming pipeline, so the full path from Bluesky's firehose to conversational query is connected end to end.

```bash
make dev-nl2sql
# → starts the full platform plus the analytics service
# → opens the query interface at http://localhost:7860
```

## How it works

A FastAPI backend receives a natural language question along with an LLM provider, model, and API key. The LLM is given the schemas of the dbt marts and how recent each one is. It generates a SQL query, which is validated and executed against ClickHouse. The LLM then explains the result in plain English.

```
Question → LLM (schemas + two clocks) → SQL → Guardrail validation → ClickHouse → LLM explanation → Answer
```

The model reads the **marts**, not the raw snapshot, and that is what makes the answers right rather than merely safe:

| Table | Reached for when |
|---|---|
| `fct_posts` | the question is about posts |
| `agg_activity_by_minute` | volume over time, or comparing record types |
| `dim_authors` | anything about accounts |
| `stg_bsky_records` | nothing above fits |

Routing by grain is what removes the original bug. "How many posts" against `fct_posts` is `count()` with no filter to forget, because there is nothing else in the table. The registry lives in `services/analytics_api/config.py` and the prompt is assembled from it, so adding a mart is a registry entry rather than a prompt edit, and the prompt cannot describe a table that no longer exists.

## Guardrails

**The security boundary is the database, not the string check.** The service connects as `analytics_ro`, a ClickHouse user with `readonly=1` and a single grant: `SELECT ON analytics.*`. The server rejects every write, DDL and mutation regardless of what the LLM generates, and the user can see nothing outside the `analytics` database.

Table functions are part of that boundary and worth calling out, because the pipeline user now has them. `file()` and `s3()` need *global* `FILE` and `S3` grants, which `GRANT ALL ON analytics.*` does not imply. The `dagster` user holds them so the warehouse can read landed Parquet itself, and `analytics_ro` does not. So generated SQL reaching for `SELECT ... FROM file('/etc/passwd')` is refused by the server rather than by a keyword list. An LLM writes this service's SQL, so the enforcement has to sit somewhere the LLM cannot reach.

Generated SQL is still validated before it is sent. Defence in depth, a cheap first filter, not the boundary:

- Only `SELECT` and `WITH` statements are permitted
- Multiple statements (semicolons) are blocked
- DDL and mutation keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `EXCHANGE`, `TRUNCATE`, `OPTIMIZE`, `SYSTEM`, `GRANT`) are blocked using word-boundary regex to prevent bypass attempts
- `INTO` and `OUTFILE` are blocked, since ClickHouse's `SELECT ... INTO OUTFILE` is a real exfiltration path out of an otherwise read-only query
- Trailing semicolons and code fences are stripped before validation

Ambiguous questions return a clarification prompt. Out-of-domain questions return a polite rejection. Neither reaches the database.

## LLM context

The system prompt gives the model two pieces of context beyond the question:

1. **The table schemas, with a grain and a "use for" line each**, so the model picks the table that already answers the question instead of reconstructing it from the record stream.
2. **Two dataset clocks**, so relative date queries ("in the last ten minutes", "posted today") are evaluated against the actual data range rather than the current wall clock. A stopped consumer means the newest row is an hour old, and answering "the last ten minutes" against the wall clock would correctly return nothing while looking like a bug.

There are two clocks because there are genuinely two. `stg_bsky_records` is a view over the snapshot the sensor loads every 60 seconds. The marts are tables rebuilt on a freshness policy and sit a few minutes behind by design. One number would make "the last ten minutes" mean something different depending on which table the model happened to pick, with nothing in the answer to say so.

## Provider support

Select your LLM provider and model directly in the Gradio UI. No environment variables required, and you paste your API key into the interface at runtime.

| Provider | Models |
|---|---|
| OpenAI | gpt-5.4-mini, gpt-5.4-nano, gpt-5.4, gpt-5.5 |
| Anthropic | claude-sonnet-5, claude-haiku-4-5-20251001, claude-opus-5 |

Nothing validates these ids. The API key arrives with the request, so an unknown model just fails the call, which means the list is only as current as the last time someone refreshed `services/analytics_api/config.py`.

## Example questions

Once the streaming pipeline has been running for a few minutes:

- *How many posts were made in the last ten minutes?*
- *What are the most common languages people are posting in?*
- *Show me the ten longest posts*
- *Which authors have posted most often?*
- *How many posts mention "coffee"?*
- *How does the volume of likes compare to posts?*

Deleted posts never appear in any of these answers. The `analytics_ro` profile applies `FINAL`, so the warehouse returns current state only.

---

## The table is not only posts

Worth stating because it is the one thing an LLM will get wrong here. `bsky_records_snapshot` holds one row per AT Protocol record across every subscribed collection, not one row per post. On a representative sample:

| Collection | Rows | Carries `text` |
|---|---:|---|
| `app.bsky.feed.like` | 429,395 | no |
| `app.bsky.feed.post` | 83,670 | yes |
| `app.bsky.feed.repost` | 62,095 | no |
| `app.bsky.graph.follow` | 50,146 | no |

Posts are about one row in eight. A model that reads "posts table" and writes `SELECT count()` returns a number roughly seven times too large, and nothing about the answer looks wrong. It is a plausible integer with a confident explanation attached.

The first fix was to name the collections in the system prompt and require `WHERE collection = 'app.bsky.feed.post'`. That works in the sense that a warning the model has to remember ever works. `fct_posts` is the fix that does not depend on remembering: one row is one post, so the filter is not forgotten because there is no filter. The warning still exists, attached only to `stg_bsky_records`, where it is still true.

This is the failure mode worth understanding about NL-to-SQL generally. The guardrails stop the model doing damage, and the *grain of the table you point it at* is what stops it being wrong. Only one of those is a security problem, and the other is the one you ship by accident. A semantic layer is usually sold as tidiness. Here it is the correctness fix, and prompt engineering was the workaround.

*(`bsky_records_snapshot` is still a misnomer. It was accurate when the subscription was posts only. Renaming it is a config change plus a warehouse rebuild.)*

## A full round trip

On the last of the example questions above:

![Conversational Analytics Interface](conversational.png)

**Read the generated SQL rather than the answer.** The question, *"how does the volume of likes compare to posts?"*, is about volume across record types, and the model went to `agg_activity_by_minute`, where `posts` and `likes` are already columns:

```sql
SELECT sum(posts) AS total_posts, sum(likes) AS total_likes,
       sum(likes) / sum(posts) AS likes_per_post
FROM analytics.agg_activity_by_minute
```

There is no `WHERE collection = ...` anywhere in it, and none is needed. The mart's grain already is the answer, so there is no filter to forget. That is this whole page happening in a single query: 96,022 posts against 585,535 likes, about 6.1 likes per post, the same shape as the collection table above and reached without the model reconstructing it from the record stream.

It is also the counterfactual worth noticing. Pointed at `stg_bsky_records`, the same question needs two conditional aggregates over a table where posts are one row in eight, and every one of those filters is a chance to be quietly wrong.
