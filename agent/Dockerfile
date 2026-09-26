# The host agent as a container, for `docker compose` (see ../compose.yaml).
# It reads the host, so it runs as root in the host's pid namespace, with the
# GPUs and the Docker socket; the compose file grants exactly that.
FROM python:3.13-slim

# ipmitool reads the BMC when /dev/ipmi0 is passed in; nvidia-smi comes from
# the NVIDIA Container Toolkit when the container is given the GPUs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ipmitool \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NVIDIA_DRIVER_CAPABILITIES=utility

# The token the dashboard creates and this reads, through a shared volume. A
# new volume takes this directory's owner if the agent mounts it first, so it
# belongs to the dashboard's user (uid 10001), which writes the token.
RUN install -d -o 10001 -g 10001 /token

WORKDIR /opt/inferenceinquire
COPY inferenceinquire_agent ./inferenceinquire_agent
RUN python -m inferenceinquire_agent --self-test

EXPOSE 9101

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('AGENT_PORT', '9101') + '/healthz', timeout=4)"

CMD ["python", "-m", "inferenceinquire_agent"]
