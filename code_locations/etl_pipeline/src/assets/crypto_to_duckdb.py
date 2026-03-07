import pandas as pd
from dagster import asset, MetadataValue
from sqlalchemy import text
from config.config import get_config

@asset(group_name="streaming_ingest", required_resource_keys={"etl_postgres"})
def crypto_prices_snapshot(context) -> pd.DataFrame:
    """Read crypto prices from Postgres (written by Spark) and materialize to DuckDB."""
    etl_postgres = context.resources.etl_postgres
    table_name = get_config().get_crypto_table_name()

    with etl_postgres.get_engine().begin() as conn:
        df = pd.read_sql(text(get_config().get_read_crypto_prices()), conn)

    context.add_output_metadata({
        "table_source": table_name,
        "row_count": len(df),
        "unique_coins": MetadataValue.json(df["coin"].unique().tolist()) if len(df) > 0 else MetadataValue.json([]),
        "head": MetadataValue.md(df.head(10).to_markdown(index=False)) if len(df) > 0 else MetadataValue.text("empty"),
    })

    return df
