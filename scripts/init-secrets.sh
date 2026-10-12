#!/usr/bin/env bash
# Generate the secret files used by compose.dev.yaml / compose.prod.yaml.
#
#   scripts/init-secrets.sh dev    -> ./secrets/dev   (all random, nothing external)
#   scripts/init-secrets.sh prod   -> ./secrets/prod  (random values to *apply* on db/s3
#                                                      servers, or overwrite with the
#                                                      existing credentials)
#
# Existing files are never overwritten. Secrets are never printed.
#
# Permissions: Compose bind-mounts file secrets as-is (it ignores uid/gid/mode
# outside Swarm) and the container runs as uid 65532, so the files must be
# readable by 'others'. Access is restricted at the directory instead: it is
# 0700 and owned by you, so no other host user can reach the files.
set -euo pipefail

env="${1:-}"
case "$env" in
  dev|prod) ;;
  *) echo "usage: $0 dev|prod [target-dir]" >&2; exit 64 ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="${2:-$root/secrets/$env}"

umask 077
mkdir -p "$dir"
chmod 700 "$dir"

# URL/ini/shell-safe random token (hex) of N bytes.
token() { openssl rand -hex "$1"; }

create() { # name, value
  local file="$dir/$1"
  if [[ -e "$file" ]]; then
    echo "keep    $file"
  else
    printf '%s' "$2" > "$file"
    chmod 444 "$file"
    echo "created $file"
  fi
}

create mlflow_db_password        "$(token 24)"
create mlflow_s3_access_key      "$( [[ $env == dev ]] && echo "mlflow-dev-$(token 4)" || echo "mlflow-$(token 6)" )"
create mlflow_s3_secret_key      "$(token 32)"
create mlflow_admin_password     "$(token 16)"   # 32 chars, MLflow requires >= 12
create mlflow_flask_secret_key   "$(token 32)"   # CSRF key; keep identical across replicas

if [[ $env == prod ]]; then
  cat <<EOF

Next (prod): make the servers match these files. Either set the passwords of the
'mlflow' role on db.simovi.org and the MinIO service account on s3.simovi.org to
the generated values, or overwrite mlflow_db_password / mlflow_s3_* here with the
credentials you already have. See README.md.
EOF
fi
