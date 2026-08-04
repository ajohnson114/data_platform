from typing import Optional

import pandas as pd
from dagster import asset, MetadataValue
from sqlalchemy import text
from config.config import get_config
from ..warehouse_targets import LOAD_RETRY, etl_target


@asset(group_name="etl_pipeline", required_resource_keys={"etl_postgres", "landing_zone"}, deps=["save_data_to_postgres_db"], kinds={"postgres", "parquet"})
def etl_table_landed(context) -> Optional[dict]:
    """Extract new etl_table rows from Postgres into an immutable Parquet file.

    This is the archive, and the only asset that reads Postgres. Its bookmark is
    the highest id already landed, read straight off the file names, so the
    extract advances even when the warehouse is down.
    """
    etl_postgres = context.resources.etl_postgres
    landing = context.resources.landing_zone
    table_name = get_config().get_etl_table_name()
    dataset = get_config().get_etl_landing_dataset()

    watermark = landing.max_landed_id(dataset)

    with etl_postgres.get_engine().begin() as conn:
        df = pd.read_sql(
            text(get_config().get_read_etl_table()),
            conn,
            params={"watermark": int(watermark),
                    "batch_size": int(get_config().get_landing_batch_size())},
        )

    if df.empty:
        context.add_output_metadata({
            "table_source": table_name,
            "rows_landed": 0,
            "watermark": int(watermark),
            "file": MetadataValue.text("nothing new to land"),
        })
        return None

    landed = landing.write(dataset, df)

    context.add_output_metadata({
        "table_source": table_name,
        "rows_landed": len(df),
        "watermark_from": int(watermark),
        "watermark_to": landed.end_id,
        "file": MetadataValue.path(landed.uri),
        "landed_at": landed.landed_at.isoformat(),
    })

    return landed._asdict()


@asset(
    group_name="etl_pipeline",
    required_resource_keys={"landing_zone", "warehouse_loader"},
    retry_policy=LOAD_RETRY,
    kinds={"parquet", "clickhouse"},
)
def etl_table_snapshot(context, etl_table_landed: Optional[dict]) -> None:
    """Load the newly landed Parquet file into the ClickHouse warehouse.

    Reads the landing zone, never Postgres. Every row carries the file it came
    from, so a warehouse row can always be traced back to its exact archive file
    -- and a wiped warehouse rebuilds by replaying those files, with no load on
    the operational database.
    """
    if etl_table_landed is None:
        context.add_output_metadata({"rows_loaded": 0, "file": MetadataValue.text("nothing landed")})
        return

    landed = context.resources.landing_zone.landed_file(etl_table_landed)
    loader = context.resources.warehouse_loader
    target = etl_target()

    result = loader.load(
        target.table,
        landed,
        order_by=target.order_by,
        version_column=target.version_column,
        is_deleted_column=target.is_deleted_column,
        partition_by=target.partition_by,
    )

    context.add_output_metadata({
        "table": target.table,
        "rows_loaded": result.rows_loaded,
        "source_file": MetadataValue.path(landed.uri),
        "loader": type(loader).__name__,
        "warehouse_source": MetadataValue.text(str(result.source)),
        "columns_added": MetadataValue.json(result.columns_added),
    })
