# Pinned to Databricks Runtime 17.3 LTS: Spark 4.0.0, Delta 4.0.0, JDK 17, Python 3.12.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless procps \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

# PySpark is a 430 MB download; it gets its own layer so a later change does not repeat it.
RUN pip install pyspark==4.0.0
RUN pip install delta-spark==4.0.0 pymssql==2.4.4

# Fetch the Delta jars once at build time so sessions start offline.
RUN python -c "from delta import configure_spark_with_delta_pip; from pyspark.sql import SparkSession; \
b = SparkSession.builder.master('local[1]') \
  .config('spark.sql.extensions', 'io.delta.sql.DeltaSparkSessionExtension') \
  .config('spark.sql.catalog.spark_catalog', 'org.apache.spark.sql.delta.catalog.DeltaCatalog'); \
s = configure_spark_with_delta_pip(b).getOrCreate(); s.sql('select 1').collect(); s.stop()"

# Microsoft's ODBC driver, the product's SQL Server driver (ADR 0017: free, not open source,
# a recorded exception), and pyodbc. pymssql above stays for the corpus tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && rm -rf /var/lib/apt/lists/*
RUN pip install pyodbc==5.2.0
