#!/usr/bin/env python3
"""
Seed Nautobot from a YAML topology description.

Idempotent: re-running with the same input is a no-op (uses get_or_create
semantics on every object). Edit the YAML, re-run, diff in Nautobot.

Usage:
    NAUTOBOT_URL=http://192.168.0.252:8080 NAUTOBOT_TOKEN=... \
        python seed.py topologies/frr-3node.yml
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pynautobot
import yaml
from dotenv import load_dotenv

load_dotenv()


def get_or_create(endpoint, search: dict, payload: dict, label: str):
    """Look up by `search` filters; if missing, create with `payload`."""
    existing = endpoint.get(**search)
    if existing:
        print(f"  = {label}: exists ({existing.id})")
        return existing
    created = endpoint.create(payload)
    print(f"  + {label}: created ({created.id})")
    return created


def seed(nb: pynautobot.api, topo: dict) -> None:
    print("\n--- Statuses ---")
    status_active = nb.extras.statuses.get(name="Active")
    if not status_active:
        sys.exit("ERROR: 'Active' status not found in Nautobot. Bootstrap data missing.")
    print(f"  = Active: {status_active.id}")

    print("\n--- Location ---")
    loc_type = get_or_create(
        nb.dcim.location_types,
        {"name": topo["location"]["type"]},
        {"name": topo["location"]["type"], "content_types": ["dcim.device", "ipam.prefix"]},
        f"LocationType {topo['location']['type']}",
    )
    location = get_or_create(
        nb.dcim.locations,
        {"name": topo["location"]["name"]},
        {
            "name": topo["location"]["name"],
            "location_type": loc_type.id,
            "status": status_active.id,
        },
        f"Location {topo['location']['name']}",
    )

    print("\n--- Manufacturer / Platform / Device Type / Role ---")
    mfr = get_or_create(
        nb.dcim.manufacturers,
        {"name": topo["manufacturer"]},
        {"name": topo["manufacturer"]},
        f"Manufacturer {topo['manufacturer']}",
    )
    platform = get_or_create(
        nb.dcim.platforms,
        {"name": topo["platform"]["name"]},
        {
            "name": topo["platform"]["name"],
            "manufacturer": mfr.id,
            "network_driver": topo["platform"]["network_driver"],
        },
        f"Platform {topo['platform']['name']}",
    )
    dev_type = get_or_create(
        nb.dcim.device_types,
        {"model": topo["device_type"]["model"]},
        {
            "manufacturer": mfr.id,
            "model": topo["device_type"]["model"],
            "u_height": topo["device_type"]["u_height"],
        },
        f"DeviceType {topo['device_type']['model']}",
    )
    role = get_or_create(
        nb.extras.roles,
        {"name": topo["role"]},
        {"name": topo["role"], "content_types": ["dcim.device"], "color": "2196f3"},
        f"Role {topo['role']}",
    )

    print("\n--- Namespace / Prefixes ---")
    namespace = get_or_create(
        nb.ipam.namespaces,
        {"name": topo["namespace"]},
        {"name": topo["namespace"]},
        f"Namespace {topo['namespace']}",
    )
    for p in topo["prefixes"]:
        get_or_create(
            nb.ipam.prefixes,
            {"prefix": p["prefix"], "namespace": namespace.id},
            {
                "prefix": p["prefix"],
                "namespace": namespace.id,
                "status": status_active.id,
                "type": p["type"],
            },
            f"Prefix {p['prefix']}",
        )

    print("\n--- Devices, Interfaces, IPs ---")
    devices_by_name: dict[str, object] = {}
    interfaces_by_key: dict[tuple[str, str], object] = {}

    for d in topo["devices"]:
        device = get_or_create(
            nb.dcim.devices,
            {"name": d["name"]},
            {
                "name": d["name"],
                "device_type": dev_type.id,
                "role": role.id,
                "platform": platform.id,
                "location": location.id,
                "status": status_active.id,
            },
            f"Device {d['name']}",
        )
        devices_by_name[d["name"]] = device

        for iface in d["interfaces"]:
            intf = get_or_create(
                nb.dcim.interfaces,
                {"device": device.id, "name": iface["name"]},
                {
                    "device": device.id,
                    "name": iface["name"],
                    "type": iface["type"],
                    "status": status_active.id,
                },
                f"Interface {d['name']}:{iface['name']}",
            )
            interfaces_by_key[(d["name"], iface["name"])] = intf

            ip = get_or_create(
                nb.ipam.ip_addresses,
                {"address": iface["ip4"], "namespace": namespace.id},
                {
                    "address": iface["ip4"],
                    "namespace": namespace.id,
                    "status": status_active.id,
                },
                f"IP {iface['ip4']}",
            )

            # Assign IP to interface (idempotent: skip if already assigned)
            existing_assignment = nb.ipam.ip_address_to_interface.get(
                ip_address=ip.id, interface=intf.id
            )
            if not existing_assignment:
                nb.ipam.ip_address_to_interface.create(
                    ip_address=ip.id, interface=intf.id
                )
                print(f"    + assigned {iface['ip4']} -> {d['name']}:{iface['name']}")
            else:
                print(f"    = {iface['ip4']} already on {d['name']}:{iface['name']}")

        # Set primary_ip4 on the device
        primary_ip = nb.ipam.ip_addresses.get(
            address=d["primary_ip4"], namespace=namespace.id
        )
        if primary_ip and (not device.primary_ip4 or device.primary_ip4.id != primary_ip.id):
            device.update({"primary_ip4": primary_ip.id})
            print(f"    + set {d['name']} primary_ip4 = {d['primary_ip4']}")

    print("\n--- Cables ---")
    for da, ia, db, ib in topo["cables"]:
        side_a = interfaces_by_key[(da, ia)]
        side_b = interfaces_by_key[(db, ib)]

        # pynautobot doesn't have a great "find cable by endpoints" filter,
        # so we check whether either side is already cabled.
        side_a_full = nb.dcim.interfaces.get(side_a.id)
        if side_a_full.cable:
            print(f"  = Cable {da}:{ia} <-> {db}:{ib}: already exists")
            continue

        nb.dcim.cables.create(
            termination_a_type="dcim.interface",
            termination_a_id=side_a.id,
            termination_b_type="dcim.interface",
            termination_b_id=side_b.id,
            status=status_active.id,
        )
        print(f"  + Cable {da}:{ia} <-> {db}:{ib}")

    print("\nDone.")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: seed.py <topology.yml>")

    url = os.environ.get("NAUTOBOT_URL")
    token = os.environ.get("NAUTOBOT_TOKEN")
    if not url or not token:
        sys.exit("ERROR: NAUTOBOT_URL and NAUTOBOT_TOKEN must be set.")

    topo_path = Path(sys.argv[1])
    if not topo_path.exists():
        sys.exit(f"ERROR: topology file not found: {topo_path}")

    with topo_path.open() as f:
        topo = yaml.safe_load(f)

    nb = pynautobot.api(url=url, token=token)
    nb.http_session.verify = False  # homelab self-signed; fine here

    seed(nb, topo)


if __name__ == "__main__":
    main()
