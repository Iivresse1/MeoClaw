ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN sed -i "s|http://deb.debian.org|http://${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash git patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/meoclaw
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --index-url "${PIP_INDEX_URL}" ".[dev]"

RUN useradd --create-home --uid 10001 agent
USER agent
WORKDIR /workspace
CMD ["bash"]
