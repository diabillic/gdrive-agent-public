# Build: docker build --pull --build-arg PATCHED_AT=$(date +%s) --build-arg REVISION=$(git rev-parse HEAD) -t gdrive-agent .
# Run:   docker run -d --name gdrive-agent --restart unless-stopped \
#     --env-file agent.env \
#     -v gdrive-agent-data:/var/lib/gdrive-agent \
#     -p 8765:8765 \
#     gdrive-agent
#
# agent.env holds the settings in .env.example (the deploy workflow renders it from repo variables and secrets).

FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV TZ=America/New_York

ARG DEBIAN_FRONTEND=noninteractive
ARG PATCHED_AT=unknown
RUN echo "Patching the base OS ($PATCHED_AT)" \
 && apt-get update \
 && apt-get -y full-upgrade \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && echo "$TZ" > /etc/timezone \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN groupadd --system gdrive-agent \
 && useradd --system --gid gdrive-agent --home-dir /var/lib/gdrive-agent --shell /usr/sbin/nologin gdrive-agent \
 && install -d -o gdrive-agent -g gdrive-agent /var/lib/gdrive-agent

WORKDIR /opt/gdrive-agent/app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY gdrive_agent/ ./gdrive_agent/
ARG REVISION=unknown
RUN echo "$REVISION" > REVISION

USER gdrive-agent
VOLUME ["/var/lib/gdrive-agent"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=15s --start-period=2m --retries=5 \
  CMD ["python", "-m", "gdrive_agent.health"]
CMD ["python", "-m", "gdrive_agent"]
