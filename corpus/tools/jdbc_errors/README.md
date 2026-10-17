# JDBC error probe

Runs the failing batches from `../odbc_errors.py` through Microsoft's JDBC driver (mssql-jdbc, MIT),
catching each error and reading on, to see whether every result set, row count and message after a
failed statement reaches the client. Run it against the sandbox:

```bash
docker run --rm --network data-migration-sandbox_default -e MSSQL_SA_PASSWORD=... \
  -v "$PWD/corpus/tools/jdbc_errors:/src" -w /src eclipse-temurin:17-jdk sh -c \
  "curl -fsSL -o mssql-jdbc.jar https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.8.1.jre11/mssql-jdbc-12.8.1.jre11.jar && java -cp mssql-jdbc.jar Probe.java"
```

Results are recorded in [Observe the ODBC driver after a failed statement](https://github.com/Bach-To-The-Future/data-migration/issues/34).
