from executor import get_max_timestamp
from config import CRYPTO_TABLE


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


def generate_sql(question: str, provider: str, model: str, api_key: str) -> str:
    max_ts = get_max_timestamp()

    system_prompt = f"""
        You are a highly competent data analyst with 20 years of experience.

        Dataset maximum timestamp:
        {max_ts}

        Interpret relative dates relative to this timestamp.

        Tables:

        {CRYPTO_TABLE}(
            coin            VARCHAR,    -- values: 'bitcoin', 'ethereum', 'solana', 'cardano', 'polkadot'
            usd             DOUBLE,     -- price in US dollars
            eur             DOUBLE,     -- price in euros
            event_timestamp TIMESTAMP
        )

        Only output a single DuckDB statement that begins with WITH or SELECT.
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
