# -------------------------
# PySpark + Kafka consumer (Bluesky Jetstream -> Postgres)
# Uses the official Spark image (includes PySpark + JVM)
# -------------------------
FROM apache/spark:3.5.4-python3

USER root

COPY spark_consumer/consumer.py /app/consumer.py

# Download the Kafka connector JARs and Postgres JDBC driver
RUN mkdir -p /opt/spark/jars \
    && curl -sL https://repo1.maven.org/maven2/org/apache/spark/spark-sql-kafka-0-10_2.12/3.5.4/spark-sql-kafka-0-10_2.12-3.5.4.jar \
       -o /opt/spark/jars/spark-sql-kafka-0-10_2.12-3.5.4.jar \
    && curl -sL https://repo1.maven.org/maven2/org/apache/kafka/kafka-clients/3.5.2/kafka-clients-3.5.2.jar \
       -o /opt/spark/jars/kafka-clients-3.5.2.jar \
    && curl -sL https://repo1.maven.org/maven2/org/apache/spark/spark-token-provider-kafka-0-10_2.12/3.5.4/spark-token-provider-kafka-0-10_2.12-3.5.4.jar \
       -o /opt/spark/jars/spark-token-provider-kafka-0-10_2.12-3.5.4.jar \
    && curl -sL https://repo1.maven.org/maven2/org/apache/commons/commons-pool2/2.12.0/commons-pool2-2.12.0.jar \
       -o /opt/spark/jars/commons-pool2-2.12.0.jar \
    && curl -sL https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar \
       -o /opt/spark/jars/postgresql-42.7.4.jar

RUN chown -R spark:spark /app /opt/spark/jars

# The checkpoint directory is a mounted volume, and this container runs as
# `spark`. Docker creates a named volume's mountpoint root-owned when the path
# doesn't already exist in the image, so without this the query dies at startup
# with "mkdir of file:/var/lib/spark/checkpoints/bsky_records failed". Creating it
# here, owned by spark, makes Docker seed the volume from it and inherit the
# ownership. Same trick, same reason, as /app/landing in etl_pipeline.dockerfile.
RUN mkdir -p /var/lib/spark/checkpoints && chown -R spark:spark /var/lib/spark

USER spark
WORKDIR /app

# Driver heap has to be set here, not in SparkSession.builder: spark-submit has
# already launched the JVM with a fixed -Xmx by the time Python runs, so a
# programmatic spark.driver.memory is silently ignored. local[*] means the driver
# JVM *is* the executor, so this is the only heap that matters. Left under the
# container's 2g mem_limit to leave room for JVM overhead and the Python worker.
CMD ["/bin/sh", "-c", "exec /opt/spark/bin/spark-submit --master local[*] --driver-memory ${SPARK_DRIVER_MEMORY:-1g} /app/consumer.py"]
