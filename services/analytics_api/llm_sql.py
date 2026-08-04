import textwrap

from executor import get_clocks
from config import TABLES


def _call_llm(system_prompt: str, user_message: str, provider: str, model: str, api_key: str) -> str:
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return response.choices[0].message.content.strip()

    elif provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text.strip()

    else:
        raise ValueError(f"Unknown provider: {provider!r}")


_PROMPT_INDENT = " " * 8


def _render_tables() -> str:
    """The schema block, built from the registry in config.py.

    Assembled rather than written out so that adding a mart is a config change
    and the prompt cannot drift from the tables that actually exist.

    Indented to match the surrounding prompt, less the first line's -- the
    f-string supplies that one. Ragged indentation here would be the model's
    only structural cue about where one table ends and the next begins.
    """
    blocks = []
    for table in TABLES:
        schema = textwrap.dedent(table.schema).strip()
        blocks.append(
            f"{table.name}  --  {table.grain}\n"
            f"  USE FOR: {table.use}\n"
            # Per table, because the tables genuinely disagree: dim_authors has
            # no event_at, and a single global instruction to use it sent the
            # model to a column that table does not have.
            f"  TIME COLUMN: {table.time_column}  --  the only column to filter "
            f"or order this table by time\n"
            f"{textwrap.indent(schema, '    ')}"
        )
    return textwrap.indent("\n\n".join(blocks), _PROMPT_INDENT).lstrip()


def generate_sql(question: str, provider: str, model: str, api_key: str) -> str:
    clocks = get_clocks()

    system_prompt = f"""
        You are a highly competent data analyst with 20 years of experience.

        Tables, in the order you should prefer them:

        {_render_tables()}

        PICK THE TABLE THAT ALREADY HAS THE GRAIN THE QUESTION ASKS ABOUT.
        Each of the first three is one row per the thing being counted, so the
        answer is usually count(), sum() or a plain ORDER BY -- no filtering to
        the right record type, because the table is already only that type.
        Only fall back to stg_bsky_records if none of them fits, and if you do,
        remember it holds every record type: posts are about one row in eight
        and likes are roughly two thirds, so any question about posts MUST
        filter `WHERE collection = 'app.bsky.feed.post'` or the count comes back
        several times too high.

        FOR ANYTHING TIME-RELATED, USE THE TIME COLUMN LISTED ABOVE FOR THE
        TABLE YOU PICKED. They are not the same column in every table, so read
        the one beside your table rather than assuming. If the question needs a
        time column the table you chose does not have, that is a signal you
        picked the wrong table -- dim_authors in particular holds all-time
        totals per account and cannot answer "in the last N minutes"; fct_posts
        grouped by did can.

        NEVER client_created_at. It is whatever the author's own client put
        there and those clocks are frequently hours wrong in both directions, so
        filtering or ordering on it gives answers that are quietly incorrect.
        The time columns named above are assigned by the firehose or derived
        from it.

        THERE ARE TWO CLOCKS, because the tables are refreshed on different
        cycles. Anchor relative dates ("in the last ten minutes", "today")
        against whichever one belongs to the table you chose:

            fct_posts, agg_activity_by_minute, dim_authors
                latest data: {clocks['mart']}
            stg_bsky_records
                latest data: {clocks['stream']}

        The marts are rebuilt periodically and the raw view tracks the live
        stream, so the marts can be a few minutes behind. That is expected. Do
        not use the current wall-clock time for anything.

        Every table holds CURRENT state. Edits are already applied and deleted
        records are already excluded, so do not filter to exclude them.

        Only output a single ClickHouse statement that begins with WITH or SELECT.
        Do not use ``` fences.
        No commentary.
        If ambiguous, respond with:
        I find that question to be a bit unclear: <question>
        If the question is not relevant to the schema, respond with:
        That's an interesting question, but it's out of my domain knowldge. Please ask something else!
        """

    return _call_llm(system_prompt, question, provider, model, api_key)


def explain_answer(question: str, sql: str, result, provider: str, model: str, api_key: str) -> str:
    user_message = f"""
        Question:
        {question}

        SQL:
        {sql}

        Result:
        {result}

        Explain briefly and clearly how the answer was computed.
        """

    return _call_llm("You are a data analyst. Be brief and clear.", user_message, provider, model, api_key)
