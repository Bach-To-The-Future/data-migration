"""Rebuild the sandbox SQL Server and the corpus with one command.

    python corpus/build.py                  build everything
    python corpus/build.py --differences    also run the T-SQL versus Spark difference tests
    python corpus/build.py --reset          throw the SQL Server data volume away first

Needs Docker and Python 3.9+ on the host, nothing else. The work happens in
the sandbox tools container (see sandbox/docker-compose.yml).
"""
import argparse
import hashlib
import json
import pathlib
import secrets
import shutil
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
CACHE = ROOT / "corpus" / "public" / ".cache"
BACKUPS = ROOT / "corpus" / "public" / "backups.json"
COMPOSE = ["docker", "compose", "-f", str(SANDBOX / "docker-compose.yml")]


def run(cmd, **kwargs):
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    code = subprocess.run(cmd, **kwargs).returncode
    if code:
        sys.exit(code)


def ensure_env():
    env = SANDBOX / ".env"
    if env.exists():
        return
    text = (SANDBOX / ".env.example").read_text(encoding="utf-8")
    # Upper case, lower case, digit and symbol, as SQL Server's password policy asks.
    password = "Sb1!" + secrets.token_hex(12)
    env.write_text(text.replace("MSSQL_SA_PASSWORD=change-me", f"MSSQL_SA_PASSWORD={password}"),
                   encoding="utf-8", newline="\n")
    print(f"Created {env.relative_to(ROOT)} with a generated sa password")


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_backups():
    """Download the public sample backups once and check them against backups.json."""
    spec = json.loads(BACKUPS.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    changed = False
    for backup in spec["backups"]:
        target = CACHE / backup["file"]
        if not target.exists():
            print(f"Downloading {backup['file']}", flush=True)
            partial = target.with_suffix(".part")
            with urllib.request.urlopen(backup["url"]) as response, partial.open("wb") as out:
                shutil.copyfileobj(response, out, length=1 << 20)
            partial.replace(target)
        actual = sha256_of(target)
        if backup["sha256"] is None:
            backup["sha256"] = actual
            changed = True
        elif backup["sha256"] != actual:
            sys.exit(f"{target} does not match the sha256 in {BACKUPS.name}; delete it and rebuild.")
    if changed:
        BACKUPS.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="remove the SQL Server data volume first")
    parser.add_argument("--skip-public", action="store_true", help="skip AdventureWorks and WideWorldImporters")
    parser.add_argument("--force-restore", action="store_true", help="restore the public databases again")
    parser.add_argument("--skip-smoke", action="store_true", help="do not run the sample artefacts")
    parser.add_argument("--differences", action="store_true", help="also run the difference tests")
    args = parser.parse_args()

    ensure_env()
    if not args.skip_public:
        fetch_backups()
    CACHE.mkdir(parents=True, exist_ok=True)

    if args.reset:
        run(COMPOSE + ["--profile", "tools", "down", "--volumes"])
    run(COMPOSE + ["up", "--detach", "sqlserver"])
    run(COMPOSE + ["--profile", "tools", "build", "--quiet", "tools"])

    tool = COMPOSE + ["--profile", "tools", "run", "--rm", "tools", "python"]
    build_args = [flag for flag, on in (("--skip-public", args.skip_public),
                                        ("--force-restore", args.force_restore),
                                        ("--skip-smoke", args.skip_smoke)) if on]
    run(tool + ["corpus/tools/build_db.py"] + build_args)
    if args.differences:
        run(tool + ["corpus/tools/differences.py"])


if __name__ == "__main__":
    main()
