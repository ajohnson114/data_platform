import os
import time

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json, current_timestamp, to_timestamp
from pyspark.sql.types import StructType, StructField, StringType, DoubleType

# -------------------------
# Configuration
# -------------------------
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC = os.environ.get("KAFKA_TOPIC", "financial_data")

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "dagster")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "dagster")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "dagster")
POSTGRES_TABLE = os.environ.get("POSTGRES_TABLE", "crypto_prices")

JDBC_URL = f"jdbc:postgresql://{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
CHECKPOINT_DIR = os.environ.get("CHECKPOINT_DIR", "/tmp/spark_checkpoints/crypto_prices")

# -------------------------
# Schema definition (best practice: always define explicit schemas)
# -------------------------
PRICE_SCHEMA = StructType([
    StructField("coin", StringType(), False),
    StructField("usd", DoubleType(), True),
    StructField("eur", DoubleType(), True),
    StructField("timestamp", StringType(), False),
])

# -------------------------
# Postgres setup
# -------------------------
JDBC_PROPERTIES = {
    "user": POSTGRES_USER,
    "password": POSTGRES_PASSWORD,
    "driver": "org.postgresql.Driver",
}

def ensure_postgres_table(spark: SparkSession):
    """Create the target table if it doesn't exist using JDBC."""
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {POSTGRES_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            coin VARCHAR(50) NOT NULL,
            usd DOUBLE PRECISION,
            eur DOUBLE PRECISION,
            event_timestamp TIMESTAMP NOT NULL,
            inserted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
    # Use a raw JDBC connection for DDL
    conn = (
        spark._jvm.java.sql.DriverManager
        .getConnection(JDBC_URL, POSTGRES_USER, POSTGRES_PASSWORD)
    )
    try:
        stmt = conn.createStatement()
        stmt.execute(ddl)
        stmt.close()
        print(f"Ensured Postgres table: {POSTGRES_TABLE}")
    finally:
        conn.close()

def write_batch_to_postgres(batch_df, batch_id):
    """foreachBatch sink: write each micro-batch to Postgres via JDBC."""
    if batch_df.isEmpty():
        return

    # Cast and rename timestamp -> event_timestamp for the Postgres schema
    write_df = batch_df.withColumn("event_timestamp", to_timestamp(col("timestamp"))).drop("timestamp")

    (
        write_df.write
        .mode("append")
        .jdbc(JDBC_URL, POSTGRES_TABLE, properties=JDBC_PROPERTIES)
    )

    print(f"Batch {batch_id}: wrote {batch_df.count()} rows to Postgres")

# -------------------------
# Spark session
# -------------------------
def create_spark_session() -> SparkSession:
    return (
        SparkSession.builder
        .appName("CryptoPriceConsumer")
        .config("spark.sql.streaming.forceDeleteTempCheckpointLocation", "true")
        .config("spark.ui.enabled", "false")
        .config("spark.driver.memory", "512m")
        .config("spark.executor.memory", "512m")
        .config("spark.jars", "/opt/spark/jars/postgresql-42.7.4.jar")
        .getOrCreate()
    )

# -------------------------
# Main
# -------------------------
def main():
    print(f"Starting Spark consumer: {BOOTSTRAP_SERVERS} / {TOPIC} -> {JDBC_URL}/{POSTGRES_TABLE}")

    # Wait for Kafka to be ready
    time.sleep(10)

    spark = create_spark_session()
    spark.sparkContext.setLogLevel("WARN")

    ensure_postgres_table(spark)

    # Read from Kafka
    raw_stream = (
        spark.readStream
        .format("kafka")
        .option("kafka.bootstrap.servers", BOOTSTRAP_SERVERS)
        .option("subscribe", TOPIC)
        .option("startingOffsets", "earliest")
        .option("failOnDataLoss", "false")
        .load()
    )

    # Parse JSON values
    parsed_stream = (
        raw_stream
        .select(
            from_json(col("value").cast("string"), PRICE_SCHEMA).alias("data")
        )
        .select("data.*")
        .filter(col("coin").isNotNull())
    )

    # Write using foreachBatch (best practice for custom sinks)
    query = (
        parsed_stream.writeStream
        .foreachBatch(write_batch_to_postgres)
        .option("checkpointLocation", CHECKPOINT_DIR)
        .trigger(processingTime="10 seconds")
        .start()
    )

    print("Streaming query started, waiting for termination...")
    query.awaitTermination()

if __name__ == "__main__":
    main()
