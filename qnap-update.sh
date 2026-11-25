#!/bin/sh
set -eu

# ==============================================================================
# QNAP / Docker Auto-Updater for Tado Local Server
# Repository: https://github.com/ExcaliburDk/tado-local-server
# ==============================================================================

REPO_OWNER="${REPO_OWNER:-ExcaliburDk}"
REPO_NAME="${REPO_NAME:-tado-local-server}"
BRANCH="${BRANCH:-main}"

# Default ports
HOST_PORT="${HOST_PORT:-4408}"
CONTAINER_PORT="${CONTAINER_PORT:-4407}"

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WORK_DIR="${WORK_DIR:-/tmp/tado-local-update.$$}"
ARCHIVE_FILE="$WORK_DIR/source.tar.gz"

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v tar >/dev/null 2>&1 || fail "tar is required"

# Check docker privilege
SUDO=""
if docker info >/dev/null 2>&1; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    SUDO="sudo"
else
    fail "Docker is not available for this user"
fi

docker_cmd() {
    if [ -n "$SUDO" ]; then
        sudo docker "$@"
    else
        docker "$@"
    fi
}

# Container and image settings (defaults to Tado-Local-Server)
CONTAINER_NAME="${CONTAINER_NAME:-Tado-Local-Server}"
IMAGE_NAME="${IMAGE_NAME:-tado-local-server:latest}"

# Auto-detect existing data volume (preserves existing pairing data)
if [ -z "${DATA_VOLUME:-}" ]; then
    if docker_cmd volume ls --format '{{.Name}}' | grep -qx "tado-local-server-data"; then
        DATA_VOLUME="tado-local-server-data"
    elif docker_cmd volume ls --format '{{.Name}}' | grep -qx "tado-local-data"; then
        DATA_VOLUME="tado-local-data"
    elif docker_cmd volume ls --format '{{.Name}}' | grep -qx "tado-local-test-data"; then
        DATA_VOLUME="tado-local-test-data"
    else
        DATA_VOLUME="tado-local-server-data"
    fi
fi

echo "=========================================================="
echo " Updating Tado Local Server"
echo " Repository:  $REPO_OWNER/$REPO_NAME ($BRANCH)"
echo " Container:   $CONTAINER_NAME (Image: $IMAGE_NAME)"
echo " Volume:      $DATA_VOLUME"
echo " Host Port:   $HOST_PORT -> Container Port: $CONTAINER_PORT"
echo " Project Dir: $PROJECT_DIR"
echo "=========================================================="

# Optional GitHub token (not required for public repository)
TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -n "${GITHUB_TOKEN_FILE:-}" ] && [ -f "$GITHUB_TOKEN_FILE" ]; then
    TOKEN="$(tr -d '\r\n' < "$GITHUB_TOKEN_FILE")"
fi

mkdir -p "$WORK_DIR/unpacked"
TAR_URL="https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/$BRANCH.tar.gz"

echo "Downloading latest source from GitHub..."
if [ -n "$TOKEN" ]; then
    curl -fsSL -H "Authorization: Bearer $TOKEN" "$TAR_URL" -o "$ARCHIVE_FILE" \
        || curl -fsSL "$TAR_URL" -o "$ARCHIVE_FILE" \
        || fail "Could not download repository archive from GitHub"
else
    curl -fsSL "$TAR_URL" -o "$ARCHIVE_FILE" \
        || fail "Could not download repository archive from GitHub"
fi

tar -xzf "$ARCHIVE_FILE" -C "$WORK_DIR/unpacked" --strip-components=1
[ -f "$WORK_DIR/unpacked/Dockerfile" ] || fail "Downloaded archive does not contain Dockerfile"

echo "Updating project files in $PROJECT_DIR..."
cp -a "$WORK_DIR/unpacked"/. "$PROJECT_DIR"/
chmod +x "$PROJECT_DIR/qnap-update.sh" "$PROJECT_DIR/qnap-setup.sh" 2>/dev/null || true

echo "Building Docker image: $IMAGE_NAME..."
docker_cmd build --no-cache -t "$IMAGE_NAME" "$PROJECT_DIR"

if docker_cmd ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "Stopping and replacing container: $CONTAINER_NAME..."
    docker_cmd stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker_cmd rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

# Also remove legacy test container if transitioning to Tado-Local-Server
if [ "$CONTAINER_NAME" = "Tado-Local-Server" ] && docker_cmd ps -a --format '{{.Names}}' | grep -qx "tado-local-test"; then
    echo "Removing legacy test container: tado-local-test..."
    docker_cmd stop "tado-local-test" >/dev/null 2>&1 || true
    docker_cmd rm "tado-local-test" >/dev/null 2>&1 || true
fi

echo "Ensuring volume exists: $DATA_VOLUME..."
docker_cmd volume create "$DATA_VOLUME" >/dev/null

echo "Starting container $CONTAINER_NAME on port $HOST_PORT..."
docker_cmd run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --user 0:0 \
    -p "$HOST_PORT:$CONTAINER_PORT" \
    -e EXTERNAL_PORT="$HOST_PORT" \
    -e HOME_NAME="${HOME_NAME:-Tado Local Server}" \
    -v "$DATA_VOLUME:/data" \
    "$IMAGE_NAME" >/dev/null

sleep 2
echo "Verifying running container..."
docker_cmd ps --filter "name=^/${CONTAINER_NAME}$" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

echo ""
echo "=========================================================="
echo " Update completed successfully!"
echo " Web UI:     http://<QNAP-IP>:$HOST_PORT/"
echo " API Docs:   http://<QNAP-IP>:$HOST_PORT/docs"
echo " Logs:       docker logs -f $CONTAINER_NAME"
echo "=========================================================="
