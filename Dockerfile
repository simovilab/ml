# syntax=docker/dockerfile:1
#
# MLflow tracking server on top of the Docker Hardened Image (DHI).
#
# The DHI runtime image is intentionally minimal (no shell, no pip, no -dev
# variant) and lacks two things we need:
#   * boto3       -> S3/MinIO artifact store
#   * Flask-WTF   -> `mlflow[auth]` extra (built-in basic-auth)
# We install them in a builder stage (DHI python -dev image, which has pip) into
# a copy of the MLflow virtualenv and copy the result into the hardened runtime
# image. The final image keeps the DHI properties: non-root (uid 65532), no
# shell, no package manager.

ARG MLFLOW_TAG=3.17
ARG PYTHON_TAG=3.12-dev

FROM dhi.io/mlflow:${MLFLOW_TAG} AS mlflow-base

FROM dhi.io/python:${PYTHON_TAG} AS builder
# Same path as in the runtime image so console-script shebangs stay valid.
COPY --from=mlflow-base /opt/mlflow /opt/mlflow
COPY requirements.txt /tmp/requirements.txt
# `mlflow[auth]==<installed version>` only adds the extra's dependencies and
# never changes the MLflow version that ships in the base image.
RUN MLFLOW_VERSION="$(/opt/mlflow/bin/python -c 'import importlib.metadata as m; print(m.version("mlflow"))')" \
 && pip --python /opt/mlflow/bin/python install --no-cache-dir \
      "mlflow[auth]==${MLFLOW_VERSION}" \
      -r /tmp/requirements.txt \
 && /opt/mlflow/bin/python -W ignore -c "import boto3, flask_wtf; print('extras OK')"

FROM dhi.io/mlflow:${MLFLOW_TAG}
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MLFLOW_DISABLE_TELEMETRY=true \
    DO_NOT_TRACK=true
COPY --from=builder /opt/mlflow /opt/mlflow
COPY --chmod=0555 docker/entrypoint.py /opt/ml/entrypoint.py

EXPOSE 5000
# Already non-root in the base image; stated explicitly for auditability.
USER 65532:65532
ENTRYPOINT ["/opt/mlflow/bin/python", "/opt/ml/entrypoint.py"]
