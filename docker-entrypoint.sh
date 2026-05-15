#!/bin/bash
set -e

UNSHACKLE_CFG_DIR="/root/.config/unshackle"
UNSHACKLE_CFG="$UNSHACKLE_CFG_DIR/unshackle.yaml"
MAPPED_CFG="/config/unshackle.yaml"

echo "[entrypoint] whoami=$(whoami)"
mkdir -p "$UNSHACKLE_CFG_DIR"

# Create all config subdirs
for subdir in WVDs Cookies Cache Logs Temp vaults DCSL PRDs; do
    mkdir -p "/config/$subdir"
    rm -rf "$UNSHACKLE_CFG_DIR/$subdir"
    ln -s "/config/$subdir" "$UNSHACKLE_CFG_DIR/$subdir"
done

# Services symlink
rm -rf "$UNSHACKLE_CFG_DIR/services"
ln -s "/services" "$UNSHACKLE_CFG_DIR/services"

# Copy and sanitise config
if [ -f "$MAPPED_CFG" ]; then
    echo "[entrypoint] Copying config..."
    cp -f "$MAPPED_CFG" "$UNSHACKLE_CFG"
    python3 /app/strip_vaults.py "$UNSHACKLE_CFG"
    echo "[entrypoint] Config ready. First line: $(head -1 $UNSHACKLE_CFG)"
else
    echo "[entrypoint] WARNING: No config at $MAPPED_CFG"
fi

# Config watcher
(
    LAST_HASH=""
    while true; do
        sleep 2
        if [ -f "$MAPPED_CFG" ]; then
            HASH=$(md5sum "$MAPPED_CFG" 2>/dev/null | cut -d' ' -f1)
            if [ "$HASH" != "$LAST_HASH" ]; then
                cp -f "$MAPPED_CFG" "$UNSHACKLE_CFG"
                python3 /app/strip_vaults.py "$UNSHACKLE_CFG" 2>/dev/null
                LAST_HASH="$HASH"
            fi
        fi
    done
) &

echo "[entrypoint] Starting Unshackle WebUI..."
exec uv run unshackle webui --host 0.0.0.0 --port 8080
