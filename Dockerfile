FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git curl ffmpeg mkvtoolnix aria2 wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Clone the fork and install with uv (includes all deps from pyproject.toml)
RUN git clone https://github.com/SpaceBallz2k8/unshackle.git .
RUN uv sync

# Volume mount points
RUN mkdir -p /config /downloads /services /data

EXPOSE 8080

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY strip_vaults.py /app/strip_vaults.py
RUN chmod +x /app/docker-entrypoint.sh

CMD ["/app/docker-entrypoint.sh"]
