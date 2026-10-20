#!/bin/bash
# Setup SSH client for outbound tunneling
# Prepares SSH keys and known_hosts for outbound connections

set -e

SSH_DIR="$HOME/.ssh"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

# Create known_hosts
touch "$SSH_DIR/known_hosts"
chmod 600 "$SSH_DIR/known_hosts"

# If SSH_PRIVATE_KEY is provided, write it
if [ -n "$SSH_PRIVATE_KEY" ]; then
    echo "$SSH_PRIVATE_KEY" > "$SSH_DIR/id_rsa"
    chmod 600 "$SSH_DIR/id_rsa"
    echo "SSH private key written to $SSH_DIR/id_rsa"
fi

# If SSH_TARGET_HOST is provided, accept the host key
if [ -n "$SSH_TARGET_HOST" ] && [ -n "$SSH_TARGET_PORT" ]; then
    echo "Populating known_hosts for $SSH_TARGET_HOST:$SSH_TARGET_PORT"
    ssh-keyscan -p "$SSH_TARGET_PORT" -H "$SSH_TARGET_HOST" >> "$SSH_DIR/known_hosts" 2>/dev/null || true
fi

echo "SSH client setup complete"
