#!/bin/sh
set -eu

TARGET_DIR=${1:-/share/Container/TadoLocal-test}
ZIP_FILE=${2:-"$TARGET_DIR/TadoLocal-setup-web.zip"}
SOURCE_DIR="$TARGET_DIR/source-web"
IMAGE_NAME=${IMAGE_NAME:-tado-local-test:setup-web}
CONTAINER_NAME=${CONTAINER_NAME:-tado-local-test}
VOLUME_NAME=${VOLUME_NAME:-tado-local-test-data}
HOST_PORT=${HOST_PORT:-4408}

echo "TadoLocal QNAP test setup"
echo "Project directory: $TARGET_DIR"
echo "ZIP file:          $ZIP_FILE"
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
docker volume create "$VOLUME_NAME" >/dev/null

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Removing existing test container $CONTAINER_NAME..."
    docker rm -f "$CONTAINER_NAME" >/dev/null
fi

echo "Starting isolated test container..."
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
