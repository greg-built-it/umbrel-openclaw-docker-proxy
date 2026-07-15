FROM python:3.13-alpine@sha256:18159b2be11d91b5781fe298b296ea1b760f844d484c3bd604cca5c86e5180b8

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt

COPY src /app/src

ENV PYTHONPATH=/app/src
ENV PROXY_SOCKET=/run/proxy/docker-proxy.sock

USER 0:0

ENTRYPOINT ["python", "-m", "openclaw_docker_proxy"]
