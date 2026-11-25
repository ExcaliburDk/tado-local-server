#!/bin/sh
set -eu

TARGET_DIR=${1:-/share/Container/Tado-Local-Server}
ZIP_FILE=${2:-"$TARGET_DIR/TadoLocal-setup-web.zip"}
SOURCE_DIR="$TARGET_DIR/source-web"
IMAGE_NAME=${IMAGE_NAME:-tado-local-server:latest}
CONTAINER_NAME=${CONTAINER_NAME:-Tado-Local-Server}
VOLUME_NAME=${VOLUME_NAME:-tado-local-server-data}
HOST_PORT=${HOST_PORT:-4408}

echo "TadoLocal QNAP Server setup"
echo "Project directory: $TARGET_DIR"
echo "ZIP file:          $ZIP_FILE"
echo "Container name:    $CONTAINER_NAME"
echo "Web address:       http://$(hostname -I 2>/dev/null | awk '{print $1}'):$HOST_PORT"
echo

command -v docker >/dev/null 2>&1 || { echo "Docker was not found."; exit 1; }
command -v unzip >/dev/null 2>&1 || { echo "unzip was not found."; exit 1; }
[ -f "$ZIP_FILE" ] || { echo "ZIP file not found: $ZIP_FILE"; exit 1; }

mkdir -p "$TARGET_DIR"
rm -rf "$SOURCE_DIR"
mkdir -p "$SOURCE_DIR"
unzip -q "$ZIP_FILE" -d "$SOURCE_DIR"

if [ ! -f "$SOURCE_DIR/Dockerfile" ]; then
    echo "Dockerfile was not found after extraction. Check the ZIP contents."
    exit 1
fi

echo "Building Docker image..."
docker build --no-cache -t "$IMAGE_NAME" "$SOURCE_DIR"

echo "Creating data volume..."
# Preserve existing volume if migrating from test setup
if [ "$VOLUME_NAME" = "tado-local-server-data" ] && ! docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
    if docker volume inspect tado-local-data >/dev/null 2>&1; then
        VOLUME_NAME="tado-local-data"
    elif docker volume inspect tado-local-test-data >/dev/null 2>&1; then
        VOLUME_NAME="tado-local-test-data"
    fi
fi
docker volume create "$VOLUME_NAME" >/dev/null

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Removing existing container $CONTAINER_NAME..."
    docker rm -f "$CONTAINER_NAME" >/dev/null
fi

# Clean up legacy test container if migrating so port is not occupied
if [ "$CONTAINER_NAME" = "Tado-Local-Server" ] && docker container inspect tado-local-test >/dev/null 2>&1; then
    echo "Removing previous test container: tado-local-test..."
    docker rm -f tado-local-test >/dev/null
fi

echo "Starting container $CONTAINER_NAME..."
docker run -d \
    --name "$CONTAINER_NAME" \
    --user 0:0 \
    -p "$HOST_PORT:4407" \
    -v "$VOLUME_NAME:/data" \
    --restart unless-stopped \
    "$IMAGE_NAME" >/dev/null

echo
echo "Setup complete. Open http://$(hostname -I 2>/dev/null | awk '{print $1}'):$HOST_PORT"
echo "Live logs: docker logs -f $CONTAINER_NAME"
