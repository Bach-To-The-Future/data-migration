"""Loads .NET and the SQL Server source's assemblies (ScriptDom, Microsoft.Data.SqlClient)
into this process through pythonnet, once."""
import os
import pathlib
import threading

_lock = threading.Lock()
_loaded = False

ASSEMBLIES = ("Microsoft.SqlServer.TransactSql.ScriptDom", "Microsoft.Data.SqlClient")


def libs_dir() -> pathlib.Path:
    """Where the published assemblies are: MIGRT_DOTNET_LIBS, set in the toolbox image."""
    value = os.environ.get("MIGRT_DOTNET_LIBS")
    if not value:
        raise RuntimeError(
            "MIGRT_DOTNET_LIBS is not set: the engine runs in the sandbox toolbox image, "
            "which publishes ScriptDom and Microsoft.Data.SqlClient there (sandbox/tools.Dockerfile)"
        )
    return pathlib.Path(value)


def load() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        libs = libs_dir()
        import pythonnet

        pythonnet.load("coreclr", runtime_config=str(libs / "toolbox-libs.runtimeconfig.json"))
        import clr

        for name in ASSEMBLIES:
            clr.AddReference(str(libs / f"{name}.dll"))
        _loaded = True
