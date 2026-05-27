import os

DUCKDB_PATH = os.getenv("DUCKDB_PATH", "/duckdb_io_manager/my_data.duckdb")
CRYPTO_TABLE = os.getenv("CRYPTO_TABLE", "public.crypto_prices_snapshot")

PROVIDER_MODELS = {
    "openai": ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4", "gpt-5.5"],
    "anthropic": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-6", "claude-opus-4-7"],
}

DEFAULT_PROVIDER = "openai"
