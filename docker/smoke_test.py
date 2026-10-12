"""End-to-end smoke test: auth, backend store (Postgres) and artifact proxy (S3).

Run against the dev stack:  docker compose -f compose.dev.yaml --profile test run --rm smoke-test
Or against any server:      MLFLOW_TRACKING_URI=... MLFLOW_TRACKING_USERNAME=... \
                            MLFLOW_TRACKING_PASSWORD=... python docker/smoke_test.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

import requests

uri = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000").rstrip("/")
secret_file = Path(os.environ.get("ML_SECRETS_DIR", "/run/secrets")) / "mlflow_admin_password"
if "MLFLOW_TRACKING_PASSWORD" not in os.environ and secret_file.exists():
    os.environ["MLFLOW_TRACKING_PASSWORD"] = secret_file.read_text().rstrip("\r\n")
os.environ.setdefault("MLFLOW_TRACKING_USERNAME", "admin")
# Same switch MLflow's own client uses; only for testing against self-signed certs.
VERIFY = os.environ.get("MLFLOW_TRACKING_INSECURE_TLS", "").lower() != "true"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip(), flush=True)
    if not ok:
        sys.exit(1)


# 1. liveness is public, everything else requires credentials
r = requests.get(f"{uri}/health", timeout=10, verify=VERIFY)
check("GET /health is public", r.status_code == 200, f"({r.status_code})")

r = requests.get(
    f"{uri}/api/2.0/mlflow/experiments/search?max_results=1", timeout=10, verify=VERIFY
)
check("anonymous API request is rejected", r.status_code == 401, f"({r.status_code})")

bad = requests.get(
    f"{uri}/api/2.0/mlflow/experiments/search?max_results=1",
    auth=("admin", "definitely-wrong"),
    timeout=10,
    verify=VERIFY,
)
check("wrong password is rejected", bad.status_code == 401, f"({bad.status_code})")

# 2. authenticated run with params, metrics and an artifact (DB + S3 via proxy)
import mlflow  # noqa: E402

mlflow.set_tracking_uri(uri)
experiment = f"smoke-test-{int(time.time())}"
mlflow.set_experiment(experiment)
with mlflow.start_run() as run:
    mlflow.log_param("hello", "world")
    mlflow.log_metric("answer", 42)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "note.txt"
        path.write_text("artifact roundtrip ok")
        mlflow.log_artifact(str(path))
check("run logged (params, metrics, artifact upload)", True, f"run_id={run.info.run_id}")

client = mlflow.MlflowClient()
fetched = client.get_run(run.info.run_id)
check("run read back from Postgres", fetched.data.params.get("hello") == "world")

with tempfile.TemporaryDirectory() as tmp:
    local = client.download_artifacts(run.info.run_id, "note.txt", tmp)
    check("artifact downloaded back from S3", Path(local).read_text() == "artifact roundtrip ok")

print("\nAll smoke tests passed.")
