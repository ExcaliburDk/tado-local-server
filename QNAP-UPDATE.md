# QNAP test update

`qnap-update.sh` updates only the isolated test instance on port 4408. It downloads the `main` branch from the private GitHub repository, copies the files into the current project folder, rebuilds the image, preserves the `tado-local-test-data` volume, and recreates `tado-local-test`.

The production container on port 4407 is not stopped or changed.

## GitHub token

The repository is private. Create a GitHub fine-grained token with read-only access to this repository and store it in a file on the QNAP, for example:

```sh
mkdir -p /share/Container/secrets
vi /share/Container/secrets/tado-github-token
chmod 600 /share/Container/secrets/tado-github-token
```

Paste the token into the file without quotes or extra text.

## Run the update

Run the script from the folder containing `Dockerfile` and `qnap-update.sh`:

```sh
cd /share/Container/TadoLocal-test/source-web
GITHUB_TOKEN_FILE=/share/Container/secrets/tado-github-token sh ./qnap-update.sh
```

When it finishes, open the printed URL on port 4408. The script prints a verification line for the new web layout and leaves the pairing/database in the Docker volume.

Optional settings can be overridden without editing the script:

```sh
HOST_PORT=4409 CONTAINER_NAME=tado-local-test-2 \
  IMAGE_NAME=tado-local-test:setup-web-2 \
  DATA_VOLUME=tado-local-test-data-2 \
  GITHUB_TOKEN_FILE=/share/Container/secrets/tado-github-token \
  sh ./qnap-update.sh
```
