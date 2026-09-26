FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY inferenceinquire ./inferenceinquire
COPY static ./static
COPY demo ./demo
COPY tests ./tests

# Unprivileged. /data (the history) and /token (the agent's token, shared with
# a containerised agent) belong to it, so a named volume starts out writable;
# a bind mount has to be made so by whoever creates it.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && install -d -o 10001 -g 10001 /data /token
USER 10001

# PORT and LISTEN_ADDRESS move it; see inferenceinquire/__main__.py.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-m", "inferenceinquire", "--healthcheck"]

CMD ["python", "-m", "inferenceinquire"]
