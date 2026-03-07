# -------------------------
# PySpark + Kafka consumer (writes to Postgres)
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

USER spark
WORKDIR /app

CMD ["spark-submit", "--master", "local[*]", "/app/consumer.py"]
