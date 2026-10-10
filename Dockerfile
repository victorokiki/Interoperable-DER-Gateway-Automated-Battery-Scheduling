# Runs the battery simulator, the operator dashboard and the gateway together on one server.
# See deploy/start.py. Build and run locally:
#   docker build -t der-gateway .
#   docker run -p 10000:10000 -e DASHBOARD_PASSWORD=<12+ characters> -e DASHBOARD_SECRET_KEY=<random> der-gateway
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PUBLIC_MODE=1 \
    DASHBOARD_HOST=0.0.0.0 \
    RUNTIME_DIR=/app/runtime

WORKDIR /app

# The gateway package first, so the dependency layer is cached between code changes.
COPY pyproject.toml dev-requirements.txt ./
COPY capstone ./capstone
RUN pip install .

# The gateway identifies itself by a device certificate. Make a throwaway set for this image
# (never stored in git): new keys with every build, used only between the parts on this server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
COPY certs/generate_dev_certs.sh ./certs/generate_dev_certs.sh
RUN cd certs && bash generate_dev_certs.sh && chmod -R a+rX /app/certs

COPY simulators ./simulators
COPY deploy ./deploy

RUN useradd --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/runtime \
    && chown -R app:app /app/runtime
USER app

EXPOSE 10000
CMD ["python", "deploy/start.py"]
