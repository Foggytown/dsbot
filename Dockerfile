# dsbot container image
#
# Two-stage build: dependencies are installed into a virtual environment in the builder,
# then copied into a slim runtime image. That keeps compilers out of the final image.
#
# The bot writes SQLite to /app/var, so mount a volume there to keep history across
# redeploys:
#     docker run -v dsbot-data:/app/var ...

FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Only the requirements first, so the dependency layer is cached when source changes.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
# Install the project itself (console script `dsbot`), without dependencies again.
RUN /opt/venv/bin/pip install --no-deps .


FROM python:3.13-slim AS runtime

# procps provides `ps`, which `dsbot stop` uses to find a lingering process on Linux.
# tini reaps zombies and forwards signals, so the bot shuts down cleanly on `docker stop`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DSBOT_DATABASE_URL="sqlite+aiosqlite:///./var/dsbot.db"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# The project files the bot reads at runtime: forms are data, not code.
COPY --from=builder /build/src ./src
COPY pyproject.toml README.md main.py ./
COPY forms ./forms
COPY scripts ./scripts

# Writable state. Declared as a volume so `docker run -v` preserves it.
RUN mkdir -p /app/var
VOLUME ["/app/var"]

# Run unprivileged. The volume directory must be owned by that user beforehand, otherwise
# Docker creates it as root and SQLite cannot write.
RUN useradd --create-home --uid 1000 dsbot \
    && chown -R dsbot:dsbot /app
USER dsbot

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py", "run"]
