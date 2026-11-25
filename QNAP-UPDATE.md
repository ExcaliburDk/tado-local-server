# QNAP Update & Deployment Guide

This guide explains how to update or deploy **Tado Local Server** on a QNAP NAS (via Container Station or SSH).

The companion repository: [ExcaliburDk/tado-local-server](https://github.com/ExcaliburDk/tado-local-server) is public, so no GitHub tokens are required!

---

## 🚀 Quick Update via SSH

1. Connect to your QNAP NAS via SSH:
   ```sh
   ssh lars@<QNAP-IP>
   ```

2. Navigate to your project folder (e.g. `/share/Container/Tado-Local-Server/source-web` or `/share/Container/tado-local`):
   ```sh
   cd /share/Container/Tado-Local-Server/source-web
   ```

3. Run the updater (use `sudo` if your user requires root permissions for Docker):
   ```sh
   sudo ./qnap-update.sh
   ```

The script will automatically:
- Download the latest source code from GitHub (`main` branch).
- Rebuild the Docker image with all the latest web UI refinements and identify APIs.
- Preserve your HomeKit pairing and settings volume (`tado-local-server-data`, `tado-local-data`, or `tado-local-test-data`).
- Recreate and restart the container cleanly (defaults to `Tado-Local-Server`).

---

## ⚙️ Customizing the Update

You can override defaults on the command line:

```sh
# Example: Change host port to 4407 and container name to Tado-Local-Server
sudo HOST_PORT=4407 CONTAINER_NAME=Tado-Local-Server ./qnap-update.sh
```

Available variables:
- `CONTAINER_NAME`: Name of the Docker container (default auto-detects `Tado-Local-Server`, `tado-local`, or `tado-local-test`).
- `DATA_VOLUME`: Named Docker volume preserving HomeKit pairing data (default auto-detects existing volume).
- `HOST_PORT`: Port exposed on your QNAP (default `4408` or `4407`).
