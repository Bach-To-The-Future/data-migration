# SqlClient error probe

Runs the failing batches from `../odbc_errors.py` through Microsoft.Data.SqlClient with
`FireInfoMessageEventOnUserErrors` on, to see whether every result set, row count and
message after a failed statement reaches the client. Run it against the sandbox:

```bash
docker run --rm --network data-migration-sandbox_default -e MSSQL_SA_PASSWORD=... \
  -v "$PWD/corpus/tools/sqlclient_errors:/src" -w /src mcr.microsoft.com/dotnet/sdk:8.0 dotnet run -c Release
```

Results are recorded in [Observe the ODBC driver after a failed statement](https://github.com/Bach-To-The-Future/data-migration/issues/34).
