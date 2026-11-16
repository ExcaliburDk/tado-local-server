#!/bin/sh
set -eu

# Update the isolated QNAP test container from the private GitHub repository.
# Production on port 4407 is never stopped or changed by this script.

REPO_OWNER="${REPO_OWNER:-ExcaliburDk}"
REPO_NAME="${REPO_NAME:-TadoLocalForQnap_private}"
BRANCH="${BRANCH:-main}"
CONTAINER_NAME="${CONTAINER_NAME:-tado-local-test}"
IMAGE_NAME="${IMAGE_NAME:-tado-local-test:setup-web}"
DATA_VOLUME="${DATA_VOLUME:-tado-local-test-data}"
HOST_PORT="${HOST_PORT:-4408}"
CONTAINER_PORT="${CONTAINER_PORT:-4407}"
PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WORK_DIR="${WORK_DIR:-/tmp/tado-local-update.$$}"
ZIP_FILE="$WORK_DIR/source.zip"

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v unzip >/dev/null 2>&1 || fail "unzip is required"

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

TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -n "${GITHUB_TOKEN_FILE:-}" ] && [ -f "$GITHUB_TOKEN_FILE" ]; then
    TOKEN="$(cat "$GITHUB_TOKEN_FILE")"
fi

mkdir -p "$WORK_DIR"
ZIP_URL="https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/zipball/$BRANCH"

echo "Downloading $REPO_OWNER/$REPO_NAME ($BRANCH)..."
if [ -n "$TOKEN" ]; then
    # GitHub API redirects archive downloads to codeload.github.com. Keep the
    # authorization header across that trusted GitHub redirect.
    curl -fsSL --location-trusted \
        -H "Authorization: Bearer $TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "$ZIP_URL" -o "$ZIP_FILE" \
        || fail "Could not download the private GitHub repository"
else
    curl -fsSL "$ZIP_URL" -o "$ZIP_FILE" \
        || fail "Could not download repository. Set GITHUB_TOKEN or GITHUB_TOKEN_FILE for the private repository"
fi

unzip -q -o "$ZIP_FILE" -d "$WORK_DIR/unpacked"
SOURCE_DIR="$(find "$WORK_DIR/unpacked" -mindepth 1 -maxdepth 2 -type f -name Dockerfile -exec dirname {} \; | head -n 1)"
[ -n "$SOURCE_DIR" ] || fail "Downloaded archive does not contain a Dockerfile"

echo "Updating existing project folder: $PROJECT_DIR"
# Copy the archive contents into the current folder, without creating a nested
# project directory. The .git folder and QNAP data volume stay untouched.
cp -a "$SOURCE_DIR"/. "$PROJECT_DIR"/

grep -q "Modern interface refinements" "$PROJECT_DIR/tado_local/static/index.html" \
    || fail "The downloaded source does not contain the expected latest web UI"

echo "Building image: $IMAGE_NAME"
docker_cmd build --no-cache -t "$IMAGE_NAME" "$PROJECT_DIR"

echo "Verifying image contents..."
docker_cmd run --rm --entrypoint /bin/sh "$IMAGE_NAME" \
    -c "grep -q 'Modern interface refinements' /app/tado_local/static/index.html" \
    || fail "The built image does not contain the latest web UI"

if docker_cmd ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "Replacing test container: $CONTAINER_NAME"
    docker_cmd stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker_cmd rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

echo "Starting test container on port $HOST_PORT"
docker_cmd run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --user 0:0 \
    -p "$HOST_PORT:$CONTAINER_PORT" \
    -v "$DATA_VOLUME:/data" \
    "$IMAGE_NAME" >/dev/null

sleep 2
docker_cmd ps --filter "name=^/${CONTAINER_NAME}$" --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker_cmd exec "$CONTAINER_NAME" \
    sh -c "grep -n 'Modern interface refinements' /app/tado_local/static/index.html"

echo "Update complete. Open http://<QNAP-IP>:$HOST_PORT/?v=$(date +%s)"
