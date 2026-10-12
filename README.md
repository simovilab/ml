# ML Server

MLflow tracking server and model registry for `https://ml.simovi.org`.

- **Image**: Docker Hardened Image `dhi.io/mlflow` (non-root, no shell), plus `boto3` and the `mlflow[auth]` extra (see `Dockerfile`).
- **Metadata**: PostgreSQL at `db.simovi.org` (tracking data and auth users live in the same database; separate tables).
- **Artifacts**: MinIO at `s3.simovi.org`. Clients only need MLflow credentials, never S3 keys: MLflow authorizes every artifact request and either streams the data itself or hands the client a short-lived (5 min), single-object presigned URL (see *Artifact transfers* below).
- **Auth**: MLflow built-in basic-auth with per-experiment/model permissions. Default permission for new resources is `NO_PERMISSIONS`.
- **TLS / routing**: Traefik (Let's Encrypt) via Docker labels in `compose.prod.yaml`; no port is published on the host.

```
client ──https──> Traefik ──(traefik_proxy network)──> MLflow ──TLS──> db.simovi.org (PostgreSQL)
   │                                                          └──TLS──> s3.simovi.org (MinIO)
   └── large artifacts: presigned URL ──https──> s3.simovi.org
```

## Files

| File | Purpose |
| --- | --- |
| `compose.dev.yaml` | Local stack: MLflow + PostgreSQL + MinIO (all on 127.0.0.1) |
| `compose.prod.yaml` | Production: MLflow only, using external db/s3 |
| `Dockerfile`, `requirements.txt` | Derived hardened image |
| `docker/entrypoint.py` | Builds config from secret *files*, then `exec`s `mlflow server` |
| `scripts/init-secrets.sh` | Generates random secret files (`secrets/dev`, `secrets/prod`) |
| `deploy/minio-mlflow-policy.json` | Least-privilege MinIO policy for the bucket |

## Local development

Requires Docker with Compose. `dhi.io` images need a Docker Hub login (`docker login dhi.io`).

```bash
scripts/init-secrets.sh dev
docker compose -f compose.dev.yaml up -d --build --wait
docker compose -f compose.dev.yaml --profile test run --rm smoke-test   # end-to-end check
```

- UI: <http://localhost:5000> — user `admin`, password in `secrets/dev/mlflow_admin_password`
- MinIO console: <http://localhost:9001> — credentials in `secrets/dev/mlflow_s3_*`
- Reset everything: `docker compose -f compose.dev.yaml down -v`

The smoke test checks: `/health` is public, anonymous and wrong-password requests get 401, a run with params/metrics/artifact is written to Postgres + MinIO and read back.

Using it from your training code on the host:

```bash
export MLFLOW_TRACKING_URI=http://localhost:5000
export MLFLOW_TRACKING_USERNAME=admin
export MLFLOW_TRACKING_PASSWORD="$(cat secrets/dev/mlflow_admin_password)"
# Dev only: MinIO's address inside the compose network (http://minio:9000) is not
# resolvable from the host, so keep artifact transfers going through MLflow:
export MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=false
export MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD=false
```

Without these two variables, downloads (and uploads of large files) from the host stall while retrying `minio:9000` — see *Artifact transfers*.

## Production deployment (server)

### 1. PostgreSQL (`db.simovi.org`)

Create a dedicated role and database (run as a superuser; pick the password from the generated file in step 3 or use your own):

```sql
CREATE ROLE mlflow LOGIN PASSWORD '<mlflow_db_password>';
CREATE DATABASE mlflow OWNER mlflow;
```

Allow the MLflow host in `pg_hba.conf` with TLS only (`hostssl mlflow mlflow <server-ip>/32 scram-sha-256`). The default `ML_DB_SSLMODE=verify-full` validates the server certificate against the system CA store; for a private CA mount it and set `ML_DB_SSLROOTCERT`, or relax to `ML_DB_SSLMODE=require`.

### 2. MinIO (`s3.simovi.org`)

Create the bucket, a restricted policy and a service account (not the root keys):

```bash
mc mb s3/mlflow
mc admin policy create s3 mlflow-rw deploy/minio-mlflow-policy.json
mc admin user add s3 mlflow-svc '<mlflow_s3_secret_key>'     # access key = mlflow-svc
mc admin policy attach s3 mlflow-rw --user mlflow-svc
```

Put `mlflow-svc` into `secrets/prod/mlflow_s3_access_key` (step 3). Optionally enable versioning on the bucket.

### 3. Secrets

```bash
scripts/init-secrets.sh prod        # creates secrets/prod/* (git-ignored, dir 0700)
```

It generates five files: `mlflow_db_password`, `mlflow_s3_access_key`, `mlflow_s3_secret_key`, `mlflow_admin_password`, `mlflow_flask_secret_key`. Overwrite the first three with the real credentials from steps 1–2 if you chose different values (files must contain the bare value; a trailing newline is stripped). Back up `mlflow_admin_password` and `mlflow_flask_secret_key` in your password manager.

Secrets are mounted as files at `/run/secrets/*`; they never appear in the compose files, `docker inspect`, image layers, or the process command line.

### 4. Start

```bash
docker login dhi.io                                   # once, to pull the hardened base images
docker compose -f compose.prod.yaml up -d --build --wait
curl -fsS http://127.0.0.1:5000/health
```

Traefik picks the container up from its labels and requests the `ml.simovi.org` certificate from Let's Encrypt. Prerequisites on the Traefik side:

- the external Docker network `traefik_proxy` exists and Traefik's Docker provider watches it (override the name with `TRAEFIK_NETWORK`);
- an HTTPS entrypoint named `websecure` (`TRAEFIK_ENTRYPOINT`) and an ACME certificate resolver named `letsencrypt` (`TRAEFIK_CERTRESOLVER`) — change the env var if yours is called differently;
- HTTP→HTTPS redirect on the `web` entrypoint (not part of the labels);
- DNS `ml.simovi.org` points to the Traefik host.

Traefik forwards the original `Host` header, which MLflow checks against its allow-list. The labels also add HSTS and `nosniff` response headers.

### 5. Users

On first start the admin account is created from `mlflow_admin_password`. Don't use it for training jobs: sign in as admin and open `https://ml.simovi.org/signup` (not reachable anonymously) to create one user per person/pipeline, then grant permissions per experiment/model. Clients use:

```bash
export MLFLOW_TRACKING_URI=https://ml.simovi.org
export MLFLOW_TRACKING_USERNAME=...
export MLFLOW_TRACKING_PASSWORD=...
```

To rotate the admin password: change the secret file and `docker compose -f compose.prod.yaml up -d --force-recreate`.

## Operations

- **Upgrade**: bump `MLFLOW_TAG` in the `Dockerfile` (or `--build-arg`), back up the database, run `docker compose -f compose.prod.yaml run --rm mlflow db-upgrade`, then `up -d --build`. For reproducible builds pin the base images by digest.
- **Settings** (optional, via env / `.env`): `ML_DB_HOST`, `ML_DB_SSLMODE`, `ML_BUCKET`, `ML_WORKERS`, `ML_MEMORY_LIMIT`, `ML_CPU_LIMIT`, `ML_SECRETS_DIR`, `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER`.
- **Backups**: PostgreSQL (`mlflow` database) and the MinIO bucket together.
- **Host header errors (403)**: MLflow only accepts the hosts in `ML_ALLOWED_HOSTS` (`ml.simovi.org`); a proxy that rewrites `Host` will be rejected.
- **Artifact transfers**: with an S3 artifact store, MLflow 3.17 servers advertise presigned transfers and clients then download (and upload large files) directly from `MLFLOW_S3_ENDPOINT_URL` — here `https://s3.simovi.org` — instead of through Traefik and the Python server. That is efficient, and no S3 credentials are exposed (URLs are signed per object, expire after 5 minutes and are issued only after MLflow's permission check), but **every client must be able to reach `s3.simovi.org`**. Clients that cannot should set `MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=false` and `MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD=false`.
- **Container hardening**: non-root uid 65532, read-only root filesystem, all capabilities dropped, `no-new-privileges`, memory/CPU/PID limits, log rotation.
