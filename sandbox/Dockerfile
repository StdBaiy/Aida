FROM node:22.22.0-bookworm-slim AS node

FROM python:3.12.10-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        ripgrep \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        fastapi==0.141.1 \
        fastmcp==4.0.3 \
        langchain==1.4.0 \
        langchain-openai==1.6.2 \
        langgraph==1.2.11 \
        langgraph-checkpoint-sqlite==3.1.1 \
        langsmith==0.12.4 \
        pydantic==2.13.5 \
        pytest==9.1.1 \
        ruff==0.16.6 \
        uv==0.12.12 \
        uvicorn==0.52.4 \
    && groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent agent \
    && mkdir -p /workspace /opt/coding-agent \
    && chown agent:agent /workspace /opt/coding-agent

COPY --chown=agent:agent web/package.json web/package-lock.json /opt/coding-agent/web/

RUN npm ci --prefix /opt/coding-agent/web --ignore-scripts --no-audit --no-fund

ENV HOME=/home/agent \
    TMPDIR=/tmp \
    LANG=C.UTF-8 \
    NODE_PATH=/opt/coding-agent/web/node_modules \
    PATH=/opt/coding-agent/web/node_modules/.bin:$PATH \
    UV_OFFLINE=1 \
    PIP_NO_INDEX=1

USER 1000:1000
WORKDIR /workspace
