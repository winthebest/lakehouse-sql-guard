# Runner for the integration tests and the demo: Python 3.12 + uv + the project's dependencies
# (extras `spark`, `mcp`, `ui` and `dbt`, dev group) + the Trino client, which the project does not
# depend on. `ui` and `dbt` are for docker/demo-e2e.sh (the approval UI, `dbt build`).
# The environment lives in /opt/venv so the read-only bind mount of the repository stays
# untouched. dbt-duckdb gets its own environment (/opt/dbt-venv) so it cannot move the pins.
# Everything outside uv.lock is pinned to an exact version so the image is reproducible.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

RUN pip install --no-cache-dir uv==0.11.29

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --extra spark --extra mcp --extra ui --extra dbt --group dev \
    && uv pip install --python /opt/venv/bin/python "trino==0.340.0"
RUN uv venv /opt/dbt-venv && uv pip install --python /opt/dbt-venv/bin/python dbt-duckdb==1.11.1

ENV PATH="/opt/venv/bin:${PATH}"
