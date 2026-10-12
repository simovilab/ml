"""Container entrypoint for the MLflow tracking server.

Why this exists: the hardened image has no shell, and MLflow does not support
``*_FILE`` variables. To keep secrets out of ``docker inspect``, compose files
and the process command line, they are mounted as files under /run/secrets and
assembled here, then MLflow is started with ``exec`` (so it becomes PID 1's
payload and receives signals directly).

Non-secret configuration (environment variables)
    ML_DB_HOST                required  PostgreSQL host
    ML_DB_PORT                          default 5432
    ML_DB_NAME                          default mlflow
    ML_DB_USER                          default mlflow
    ML_DB_SSLMODE                       default verify-full
    ML_DB_SSLROOTCERT                   default "system" (libpq system CA store)
    ML_AUTH_DB_NAME                     default = ML_DB_NAME (own tables, own
                                        alembic_version_auth table)
    ML_ARTIFACTS_DESTINATION  required  e.g. s3://mlflow
    ML_ALLOWED_HOSTS          required  Host headers MLflow accepts
    ML_CORS_ALLOWED_ORIGINS   required  e.g. https://ml.simovi.org
    ML_WORKERS                          default 4
    ML_AUTH_DEFAULT_PERMISSION          default NO_PERMISSIONS
    ML_SECRETS_DIR                      default /run/secrets
    MLFLOW_S3_ENDPOINT_URL              passed through to MLflow/boto3
    MLFLOW_AUTH_ADMIN_USERNAME          passed through (bootstrap admin name)

Secrets (files in ML_SECRETS_DIR)
    mlflow_db_password, mlflow_s3_access_key, mlflow_s3_secret_key,
    mlflow_admin_password, mlflow_flask_secret_key

Usage
    (no args)      start the tracking server
    db-upgrade     run `mlflow db upgrade` against the backend store, then exit
    <anything>     passed to the `mlflow` CLI as-is (secrets still injected)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

MLFLOW_BIN = "/opt/mlflow/bin/mlflow"
SECRETS_DIR = Path(os.environ.get("ML_SECRETS_DIR", "/run/secrets"))


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"entrypoint: error: {message}", file=sys.stderr, flush=True)
    sys.exit(78)  # EX_CONFIG


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"required environment variable {name} is not set")
    return value


def read_secret(name: str) -> str:
    path = SECRETS_DIR / name
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        die(f"cannot read secret {path}: {exc.strerror or exc}")
    if not value:
        die(f"secret {path} is empty")
    return value


def db_uri(database: str, password: str) -> str:
    from sqlalchemy.engine import URL

    query = {"sslmode": os.environ.get("ML_DB_SSLMODE", "verify-full")}
    sslrootcert = os.environ.get("ML_DB_SSLROOTCERT", "system")
    if sslrootcert and query["sslmode"] in ("verify-ca", "verify-full"):
        query["sslrootcert"] = sslrootcert
    url = URL.create(
        "postgresql+psycopg2",
        username=os.environ.get("ML_DB_USER", "mlflow"),
        password=password,  # percent-encoded by SQLAlchemy
        host=require_env("ML_DB_HOST"),
        port=int(os.environ.get("ML_DB_PORT", "5432")),
        database=database,
        query=query,
    )
    return url.render_as_string(hide_password=False)


def write_auth_config(auth_db_uri: str) -> str:
    """Write MLflow's basic-auth ini to a private tmpfs location.

    MLflow only accepts the auth database URI through this file, so it has to
    contain the DB password; it lives on tmpfs, mode 0600, never in the image.
    """
    directory = Path(tempfile.mkdtemp(prefix="mlflow-auth-"))
    os.chmod(directory, 0o700)
    path = directory / "basic_auth.ini"
    permission = os.environ.get("ML_AUTH_DEFAULT_PERMISSION", "NO_PERMISSIONS")
    # configparser treats "%" as interpolation syntax -> escape it.
    escaped_uri = auth_db_uri.replace("%", "%%")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            "[mlflow]\n"
            f"default_permission = {permission}\n"
            f"database_uri = {escaped_uri}\n"
        )
    return str(path)


def main() -> None:
    args = sys.argv[1:]

    db_password = read_secret("mlflow_db_password")
    backend_uri = db_uri(os.environ.get("ML_DB_NAME", "mlflow"), db_password)

    env = os.environ.copy()
    env["MLFLOW_BACKEND_STORE_URI"] = backend_uri  # not on the command line

    if args and args[0] == "db-upgrade":
        os.execve(MLFLOW_BIN, [MLFLOW_BIN, "db", "upgrade", backend_uri], env)
    if args:
        os.execve(MLFLOW_BIN, [MLFLOW_BIN, *args], env)

    # --- tracking server -------------------------------------------------
    env["AWS_ACCESS_KEY_ID"] = read_secret("mlflow_s3_access_key")
    env["AWS_SECRET_ACCESS_KEY"] = read_secret("mlflow_s3_secret_key")
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")  # MinIO default region
    env["MLFLOW_FLASK_SERVER_SECRET_KEY"] = read_secret("mlflow_flask_secret_key")
    env["MLFLOW_AUTH_ADMIN_PASSWORD"] = read_secret("mlflow_admin_password")
    env.setdefault("MLFLOW_AUTH_ADMIN_USERNAME", "admin")
    auth_db = os.environ.get("ML_AUTH_DB_NAME") or os.environ.get("ML_DB_NAME", "mlflow")
    env["MLFLOW_AUTH_CONFIG_PATH"] = write_auth_config(db_uri(auth_db, db_password))

    command = [
        MLFLOW_BIN,
        "server",
        "--app-name", "basic-auth",
        "--host", "0.0.0.0",
        "--port", "5000",
        "--workers", os.environ.get("ML_WORKERS", "4"),
        # Artifact access goes through MLflow (auth + permissions): clients never get
        # S3 credentials, only MLflow-authorized, short-lived presigned URLs.
        "--serve-artifacts",
        "--artifacts-destination", require_env("ML_ARTIFACTS_DESTINATION"),
        "--default-artifact-root", "mlflow-artifacts:/",
        "--allowed-hosts", require_env("ML_ALLOWED_HOSTS"),
        "--cors-allowed-origins", require_env("ML_CORS_ALLOWED_ORIGINS"),
        "--x-frame-options", "DENY",
    ]
    os.execve(MLFLOW_BIN, command, env)


if __name__ == "__main__":
    main()
