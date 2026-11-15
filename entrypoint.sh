#!/bin/sh
set -e

STATE_DB="/data/tado-local.db"

ARGS="--state $STATE_DB --port ${TADO_PORT:-4407}"

if [ -n "$TADO_BRIDGE_IP" ]; then
    ARGS="$ARGS --bridge-ip $TADO_BRIDGE_IP"
fi

if [ -n "$TADO_BRIDGE_PIN" ]; then
    ARGS="$ARGS --pin $TADO_BRIDGE_PIN"
fi

# Forward-compatible: PR #40 accessory support (comma-separated IPs and PINs)
if [ -n "$TADO_ACCESSORY_IP" ]; then
    IFS=','
    set -- $TADO_ACCESSORY_IP
    idx=0
    for ip in "$@"; do
        pin=$(echo "$TADO_ACCESSORY_PIN" | cut -d',' -f$((idx + 1)))
        ARGS="$ARGS --accessory-ip $ip"
        [ -n "$pin" ] && ARGS="$ARGS --accessory-pin $pin"
        idx=$((idx + 1))
    done
    unset IFS
fi

exec tado-local $ARGS
