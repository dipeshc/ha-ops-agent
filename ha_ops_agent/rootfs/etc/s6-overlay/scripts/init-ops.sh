#!/command/with-contenv bashio
# shellcheck shell=bash
# Prepares /data, the privilege boundary for the `ops` user and, when the house
# profile enables git proposals, the agent's own GitHub deploy key on every start.
set -euo pipefail

umask 077
mkdir -p /data/secrets /data/ssh /data/runs /data/snapshots /run/ops

# Everything the daemon keeps is root-only; `ops` may only traverse /data to
# reach its own Claude config directory.
chmod 0711 /data
find /data -mindepth 1 -maxdepth 1 ! -name claude-ops -exec chmod go-rwx {} +
mkdir -p /data/claude-ops/home
chown -R ops:ops /data/claude-ops
chmod 0700 /data/claude-ops /data/claude-ops/home

# The HA config and backups are mapped below /mnt/ha (see config.yaml);
# `ops` cannot traverse into it at all.
chmod 0700 /mnt/ha
chmod 0700 /run/ops

# s6 keeps the container environment (including SUPERVISOR_TOKEN) as files.
if [[ -d /run/s6/container_environment ]]; then
  chmod 0700 /run/s6/container_environment
fi

# Claude's read-only export (root-owned) and the opsctl socket (root:ops).
install -d -o root -g root -m 0755 /run/ops-claude
install -d -o root -g ops -m 0750 /run/ops-sock
# Proposal drafts: the daemon recreates this per chat/investigation run, owned by ops.
rm -rf /run/ops-drafts

# Git is optional: no deploy key unless ops/profile.yaml (the house profile) enables git and is valid.
# An existing key is kept either way.
if python3 -I -c 'import sys; sys.path.insert(0, "/opt/ops"); from pathlib import Path; from opsd.profile import load; p, bad = load(Path("/mnt/ha/config")); sys.exit(0 if p.git.enabled and not bad else 1)'; then
  if [[ ! -f /data/ssh/github_deploy ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "ha-ops-agent" -f /data/ssh/github_deploy
    bashio::log.notice "Generated a GitHub deploy key for this agent."
  fi
  bashio::log.notice "Deploy key (add to GitHub with write access): $(cat /data/ssh/github_deploy.pub)"
else
  bashio::log.info "Git proposals are off (house profile): no deploy key."
fi
