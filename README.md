# ML Server

MLflow tracking server and model registry for `https://ml.simovi.org`.

- **Image**: Docker Hardened Image `dhi.io/mlflow` (non-root, no shell), plus `boto3` and the `mlflow[auth]` extra (see `Dockerfile`).
- **Metadata**: PostgreSQL — self-hosted (`db.simovi.org`) or a managed cluster from a cloud provider. Tracking data and auth users live in the same database (separate tables).
- **Artifacts**: any S3-compatible object store — self-hosted MinIO (`s3.simovi.org`) or a hosted service such as DigitalOcean Spaces. Clients only need MLflow credentials, never S3 keys: MLflow authorizes every artifact request and either streams the data itself or hands the client a short-lived (5 min), single-object presigned URL (see *Artifact transfers* below).
- **Auth**: MLflow built-in basic-auth with per-experiment/model permissions. Default permission for new resources is `NO_PERMISSIONS`.
- **TLS / routing**: Traefik (Let's Encrypt) via Docker labels in `compose.prod.yaml`; no port is published on the host.

```
client ──https──> Traefik ──(traefik_proxy network)──> MLflow ──TLS──> PostgreSQL  (ML_DB_HOST)
   │                                                          └──TLS──> object store (MLFLOW_S3_ENDPOINT_URL)
   └── large artifacts: presigned URL ──https──> object store
```

The server itself is backend-agnostic: everything that differs between a self-hosted and a managed setup is expressed as environment variables in `.env` plus the secret files. Nothing in the image or in `compose.prod.yaml` is tied to a particular provider.

## Files

| File | Purpose |
| --- | --- |
| `compose.dev.yaml` | Local stack: MLflow + PostgreSQL + MinIO (all on 127.0.0.1) |
| `compose.prod.yaml` | Production: MLflow only, using external db/object storage |
| `.env.example` | Template for `.env`; non-secret settings only |
| `Dockerfile`, `requirements.txt` | Derived hardened image |
| `docker/entrypoint.py` | Builds config from secret *files*, then `exec`s `mlflow server` |
| `scripts/init-secrets.sh` | Generates random secret files (`secrets/dev`, `secrets/prod`) |
| `deploy/minio-mlflow-policy.json` | Least-privilege MinIO policy for the bucket (self-hosted MinIO only) |

## Local development

Requires Docker with Compose. `dhi.io` images need a Docker Hub login (`docker login dhi.io`).

The dev stack always runs its own PostgreSQL and MinIO containers, regardless of what production uses.

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

Steps 1 and 2 come in two variants; pick the one that matches your backing services. Steps 3–6 are the same for both.

### 1. PostgreSQL

MLflow needs one database and one role that owns it. The tracking tables and the auth tables share that database.

#### 1a. Self-hosted PostgreSQL (`db.simovi.org`)

Create a dedicated role and database (run as a superuser; pick the password from the generated file in step 3 or use your own):

```sql
CREATE ROLE mlflow LOGIN PASSWORD '<mlflow_db_password>';
CREATE DATABASE mlflow OWNER mlflow;
```

Allow the MLflow host in `pg_hba.conf` with TLS only (`hostssl mlflow mlflow <server-ip>/32 scram-sha-256`). With `ML_DB_SSLMODE=verify-full` the server certificate is validated against the system CA store (`ML_DB_SSLROOTCERT=system`); for a private CA, put the CA file in the secrets directory and point `ML_DB_SSLROOTCERT` at its mount path.

#### 1b. Managed PostgreSQL (DigitalOcean Managed Databases, RDS, Cloud SQL, …)

Create the database and role with the provider's console or CLI — you normally cannot edit `pg_hba.conf`, so access is controlled by the provider's trusted-sources/firewall rules instead. Add the MLflow host's public IP there.

For DigitalOcean specifically:

```bash
doctl databases db create <cluster-id> mlflow
doctl databases user create <cluster-id> mlflow
doctl databases firewalls append <cluster-id> --rule ip_addr:<server-ip>
```

Managed clusters differ from a self-hosted server in three ways that matter here:

- **Non-standard port** — DigitalOcean uses `25060`, not `5432`. Set `ML_DB_PORT`.
- **Private CA** — a DigitalOcean PostgreSQL *Standard Edition* cluster presents a certificate issued by a per-project CA that is *not* in the system trust store, so `verify-full` fails against it. Download the cluster's CA from the control panel (*Download CA certificate*) or with `doctl databases get-ca <cluster-id>`, save it as `secrets/prod/ca-certificate.crt`, and set `ML_DB_SSLROOTCERT=/run/secrets/mlflow_db_ca`. *Advanced Edition* clusters use publicly trusted certificates instead — keep `ML_DB_SSLROOTCERT=system` for those.
- **Low connection limits** — see *Connection limits* below before raising `ML_WORKERS`.

Verify connectivity before deploying (the password is read from the file, so it never reaches your shell history):

```bash
docker run --rm -v "$PWD/secrets/prod:/s:ro" postgres:18-alpine sh -c \
  'PGPASSWORD=$(cat /s/mlflow_db_password) psql "host=<db-host> port=<db-port> dbname=mlflow \
     user=mlflow sslmode=verify-full sslrootcert=/s/ca-certificate.crt" -c "select version()"'
```

### 2. Object storage

MLflow needs one bucket and a credential pair that can read, write and delete inside it.

#### 2a. Self-hosted MinIO (`s3.simovi.org`)

Create the bucket, a restricted policy and a service account (not the root keys):

```bash
mc mb s3/mlflow
mc admin policy create s3 mlflow-rw deploy/minio-mlflow-policy.json
mc admin user add s3 mlflow-svc '<mlflow_s3_secret_key>'     # access key = mlflow-svc
mc admin policy attach s3 mlflow-rw --user mlflow-svc
```

Put `mlflow-svc` into `secrets/prod/mlflow_s3_access_key` (step 3). Optionally enable versioning on the bucket. `deploy/minio-mlflow-policy.json` grants only `ListBucket`/`GetBucketLocation` on the bucket and object-level read/write/delete plus multipart operations; adjust the ARNs if you rename the bucket.

MinIO ignores the SigV4 signing region, so `ML_S3_REGION` can stay at its `us-east-1` default.

#### 2b. Hosted object storage (DigitalOcean Spaces, AWS S3, …)

Create the bucket and an access key in the provider's console, then mind two differences:

- **The signing region must match the endpoint.** MinIO accepts any region, hosted services do not: a request signed for `us-east-1` against `https://sfo3.digitaloceanspaces.com` is rejected. Set `ML_S3_REGION` to the endpoint's region (`sfo3`, `nyc3`, `eu-west-1`, …).
- **The MinIO policy file does not apply.** `deploy/minio-mlflow-policy.json` is a MinIO admin policy. On AWS you would attach an equivalent IAM policy to the key; on DigitalOcean Spaces a classic Spaces key grants access to every Space in the account, so scope it with per-bucket access keys if your account offers them, or accept that the key is broader than the MLflow bucket.

Endpoint and bucket go into `MLFLOW_S3_ENDPOINT_URL` and `ML_BUCKET`; the key pair goes into the `mlflow_s3_*` secret files. Verify before deploying:

```bash
docker run --rm -v "$PWD/secrets/prod:/s:ro" --entrypoint sh amazon/aws-cli -c \
  'export AWS_ACCESS_KEY_ID=$(cat /s/mlflow_s3_access_key) \
          AWS_SECRET_ACCESS_KEY=$(cat /s/mlflow_s3_secret_key) AWS_DEFAULT_REGION=<region>; \
   aws s3 ls s3://<bucket>/ --endpoint-url <endpoint>'
```

### 3. Secrets

```bash
scripts/init-secrets.sh prod        # creates secrets/prod/* (git-ignored, dir 0700)
```

It generates five files: `mlflow_db_password`, `mlflow_s3_access_key`, `mlflow_s3_secret_key`, `mlflow_admin_password`, `mlflow_flask_secret_key`. Overwrite the first three with the real credentials from steps 1–2 if you chose different values (files must contain the bare value; a trailing newline is stripped). They are created read-only, so overwriting one means `chmod 600` first and `chmod 444` afterwards. Back up `mlflow_admin_password` and `mlflow_flask_secret_key` in your password manager — they are random and not recoverable.

A sixth file, `secrets/prod/ca-certificate.crt`, is **always required**: `compose.prod.yaml` mounts it unconditionally and Docker refuses to create the container if the path does not exist (`invalid mount config for type "bind": bind source path does not exist`). Put the database CA there when you need one (step 1b, or a self-hosted private CA); when you rely on the system CA store (`ML_DB_SSLROOTCERT=system`) create an empty placeholder — it is mounted but never read:

```bash
: > secrets/prod/ca-certificate.crt && chmod 444 secrets/prod/ca-certificate.crt
```

Secrets are mounted as files at `/run/secrets/*`; they never appear in the compose files, `docker inspect`, image layers, or the process command line. The directory is `0700` and the files `0444`, so the container's uid 65532 can read each bind-mounted file while no other host user can list the directory.

### 4. Configuration (`.env`)

Copy `.env.example` to `.env` and adjust. Only non-secret settings belong here.

Self-hosted backing services:

```bash
ML_DB_HOST=db.simovi.org
ML_DB_PORT=5432
ML_DB_SSLMODE=verify-full
ML_DB_SSLROOTCERT=system

MLFLOW_S3_ENDPOINT_URL=https://s3.simovi.org
ML_BUCKET=mlflow
ML_S3_REGION=us-east-1
```

Managed services (DigitalOcean example):

```bash
ML_DB_HOST=<cluster>.k.db.ondigitalocean.com
ML_DB_PORT=25060
ML_DB_SSLMODE=verify-full
ML_DB_SSLROOTCERT=/run/secrets/mlflow_db_ca
ML_DB_POOL_SIZE=1
ML_DB_MAX_OVERFLOW=2
ML_WORKERS=2

MLFLOW_S3_ENDPOINT_URL=https://sfo3.digitaloceanspaces.com
ML_BUCKET=<space-name>
ML_S3_REGION=sfo3
```

Check that the substitutions resolve as expected with `docker compose -f compose.prod.yaml config`.

### 5. Start

```bash
docker login dhi.io                                       # once, to pull the hardened base images
docker compose -f compose.prod.yaml build
docker compose -f compose.prod.yaml run --rm mlflow db-upgrade    # create/migrate the schema
docker compose -f compose.prod.yaml up -d --wait
```

`db-upgrade` is optional on a fresh database (the server creates the tables itself) but it surfaces connection, TLS and permission problems on their own, before the server and its healthcheck are involved.

Traefik picks the container up from its labels and requests the `ml.simovi.org` certificate from Let's Encrypt. Prerequisites on the Traefik side:

- the external Docker network `traefik_proxy` exists and Traefik's Docker provider watches it (override the name with `TRAEFIK_NETWORK`);
- an HTTPS entrypoint named `websecure` (`TRAEFIK_ENTRYPOINT`) and an ACME certificate resolver named `letsencrypt` (`TRAEFIK_CERTRESOLVER`) — change the env var if yours is called differently;
- HTTP→HTTPS redirect on the `web` entrypoint (not part of the labels);
- DNS `ml.simovi.org` points to the Traefik host.

Traefik forwards the original `Host` header, which MLflow checks against its allow-list. The labels also add HSTS and `nosniff` response headers.

Verify the deployment end-to-end — this exercises auth, the database and an artifact round-trip through the object store:

```bash
docker run --rm \
  -v "$PWD/docker/smoke_test.py:/opt/smoke_test.py:ro" \
  -v "$PWD/secrets/prod/mlflow_admin_password:/run/secrets/mlflow_admin_password:ro" \
  -e MLFLOW_TRACKING_URI=https://ml.simovi.org \
  --entrypoint /opt/mlflow/bin/python ml-mlflow:prod /opt/smoke_test.py
```

Mount the password file itself, not the `secrets/prod` directory: the directory is `0700` and owned by you, so uid 65532 cannot traverse it. It leaves a `smoke-test-*` experiment and one small artifact behind; delete them afterwards if you care.

### 6. Users

On first start the admin account is created from `mlflow_admin_password`. Don't use it for training jobs: sign in as admin and open `https://ml.simovi.org/signup` (not reachable anonymously) to create one user per person/pipeline, then grant permissions per experiment/model. Clients use:

```bash
export MLFLOW_TRACKING_URI=https://ml.simovi.org
export MLFLOW_TRACKING_USERNAME=...
export MLFLOW_TRACKING_PASSWORD=...
```

To rotate the admin password: change the secret file and `docker compose -f compose.prod.yaml up -d --force-recreate`.

## Operations

- **Upgrade**: bump `MLFLOW_TAG` in the `Dockerfile` (or `--build-arg`), back up the database, run `docker compose -f compose.prod.yaml run --rm mlflow db-upgrade`, then `up -d --build`. For reproducible builds pin the base images by digest.
- **Settings** (optional, via env / `.env`): `ML_DB_HOST`, `ML_DB_PORT`, `ML_DB_NAME`, `ML_DB_USER`, `ML_DB_SSLMODE`, `ML_DB_SSLROOTCERT`, `ML_DB_POOL_SIZE`, `ML_DB_MAX_OVERFLOW`, `ML_DB_POOL_RECYCLE`, `MLFLOW_S3_ENDPOINT_URL`, `ML_BUCKET`, `ML_S3_REGION`, `ML_WORKERS`, `ML_MEMORY_LIMIT`, `ML_CPU_LIMIT`, `ML_SECRETS_DIR`, `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER`.
- **Connection limits**: each gunicorn worker can open its own SQLAlchemy engine for the tracking store, the model registry and the auth store, so the upper bound is roughly `ML_WORKERS x 3 x (ML_DB_POOL_SIZE + ML_DB_MAX_OVERFLOW)` connections. The defaults (4 workers, pool 5, overflow 10) are fine for a self-hosted server but far exceed the limits of small managed plans — a DigitalOcean 1 GB cluster allows 25 connections in total, a few of which the provider reserves. Keep the product below the server's `max_connections` (`show max_connections;`), or put a provider-side connection pooler (PgBouncer; DigitalOcean exposes it on port 25061) between MLflow and the cluster instead of raising the workers. `ML_DB_POOL_RECYCLE` (default 1800 s) keeps idle connections from being dropped silently by the provider.
- **Backups**: PostgreSQL (`mlflow` database) and the artifact bucket together. Managed providers usually snapshot the database for you; the bucket is still your responsibility.
- **Host header errors (403)**: MLflow only accepts the hosts in `ML_ALLOWED_HOSTS` (`ml.simovi.org`); a proxy that rewrites `Host` will be rejected.
- **Artifact transfers**: with an S3 artifact store, MLflow 3.17 servers advertise presigned transfers and clients then download (and upload large files) directly from `MLFLOW_S3_ENDPOINT_URL` instead of through Traefik and the Python server. That is efficient, and no S3 credentials are exposed (URLs are signed per object, expire after 5 minutes and are issued only after MLflow's permission check), but **every client must be able to reach that endpoint**. With a hosted store it is public, so this is normally fine; with a self-hosted MinIO on a private network, clients that cannot reach it should set `MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=false` and `MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD=false`.
- **S3 `SignatureDoesNotMatch` / `AuthorizationHeaderMalformed`**: the signing region does not match the endpoint — fix `ML_S3_REGION` (step 2b).
- **Container hardening**: non-root uid 65532, read-only root filesystem, all capabilities dropped, `no-new-privileges`, memory/CPU/PID limits, log rotation.
