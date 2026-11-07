# The .NET libraries the engine loads through pythonnet (ScriptDom, Microsoft.Data.SqlClient),
# published in a build stage so the toolbox carries only the runtime and the assemblies.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS dotnet-libs
COPY dotnet/toolbox-libs.csproj /src/
RUN dotnet publish /src/toolbox-libs.csproj -c Release -o /out

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

# Microsoft's ODBC driver and pyodbc, kept only as the test record of issue 34 (spec section 18);
# the product's SQL Server client is Microsoft.Data.SqlClient, below. pymssql stays for the corpus tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && rm -rf /var/lib/apt/lists/*
RUN pip install pyodbc==5.2.0

# The .NET runtime (MIT) for pythonnet, pinned, with ICU for SqlClient's globalisation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libicu72 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && bash /tmp/dotnet-install.sh --runtime dotnet --version 10.0.12 --install-dir /usr/share/dotnet \
    && rm /tmp/dotnet-install.sh
COPY --from=dotnet-libs /out /opt/migrt/dotnet
ENV DOTNET_ROOT=/usr/share/dotnet \
    MIGRT_DOTNET_LIBS=/opt/migrt/dotnet

# The engine's own dependencies: pythonnet for ScriptDom and SqlClient, psycopg for the
# Postgres control database, pyflakes for layer-1 lint, pytest for the engine's tests.
RUN pip install pythonnet==3.2.1 "psycopg[binary]==3.3.8" pyflakes==4.1.0 pytest==9.0.3

# Type checking for the engine (MIT); the nodejs extra bundles the Node runtime it needs,
# fetched here so checks run offline.
RUN pip install "pyright[nodejs]==1.1.414" && pyright --version
