# netlab-from-nautobot

Python tooling that bridges Nautobot (network source of truth) to containerlab (network simulator) on the homelab.

## Goal

Drive the simulated network from canonical data: Nautobot holds the model, scripts here render it into containerlab topologies and FRR configs.

## Components

### `scripts/seed/` (read-write)

YAML-driven seeder. Given a topology description, populates Nautobot with locations, manufacturers, platforms, device types, roles, devices, interfaces, IP addresses, and cables. Idempotent (get-or-create), so re-running is safe.

Used for bootstrapping a lab Nautobot from a known-good topology spec, or migrating from CMDB exports.

### `scripts/render/` (read-only, future)

Pulls device/interface/cable data from Nautobot via GraphQL and renders containerlab topology files plus FRR config snippets. The deployed netlab matches whatever Nautobot says is true.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e .
    cp .env.example .env
    # edit .env with your Nautobot URL and token

## Seed a topology

    python scripts/seed/seed.py scripts/seed/topologies/frr-3node.yml

## Reset Nautobot (full wipe)

    cd ~/dev/nautobot && ssh ubuntu@192.168.0.252 'cd /opt/nautobot && docker compose down -v'
    cd ansible && ansible-playbook playbook.yml

