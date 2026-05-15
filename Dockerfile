FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    curl \
    ffmpeg \
    mkvtoolnix \
    aria2 \
    wget \
    ca-certificates \
    tar \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------
# Install CCExtractor (.deb)
# -----------------------------
RUN wget -O /tmp/ccextractor.deb \
    https://github.com/CCExtractor/ccextractor/releases/download/v0.96.6/ccextractor_0.96.6_debian13_amd64.deb \
    && apt-get update \
    && apt-get install -y /tmp/ccextractor.deb \
    && rm /tmp/ccextractor.deb \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------
# Install Shaka Packager
# -----------------------------
RUN wget -O /usr/local/bin/packager \
    https://github.com/shaka-project/shaka-packager/releases/download/v3.7.2/packager-linux-x64 \
    && chmod +x /usr/local/bin/packager

# -----------------------------
# Install N_m3u8DL-RE
# -----------------------------
RUN wget -O /tmp/n_m3u8dl.tar.gz \
    https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz \
    && mkdir -p /tmp/n_m3u8dl \
    && tar -xzf /tmp/n_m3u8dl.tar.gz -C /tmp/n_m3u8dl \
    && mv /tmp/n_m3u8dl/N_m3u8DL-RE /usr/local/bin/N_m3u8DL-RE \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf /tmp/n_m3u8dl /tmp/n_m3u8dl.tar.gz

# -----------------------------
# Install uv
# -----------------------------
RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Clone project
RUN git clone https://github.com/SpaceBallz2k8/unshackle.git .

# Install Python deps
RUN uv sync

# Volume mount points
RUN mkdir -p /config /downloads /services /data

EXPOSE 8080

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY strip_vaults.py /app/strip_vaults.py

RUN chmod +x /app/docker-entrypoint.sh

CMD ["/app/docker-entrypoint.sh"]