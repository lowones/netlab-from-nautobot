#!/usr/bin/env python3
"""
Render a containerlab topology + FRR configs from Nautobot.

Reads device/interface/cable data via GraphQL, transforms it into a clab
topology file and per-node FRR configs.

Usage:
    python render.py --location homelab --out ./output
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from collections import defaultdict
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

load_dotenv()

# Static FRR daemons file (same for every node)
FRR_DAEMONS = """\
bgpd=yes
ospfd=yes
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
"""

DEFAULT_FRR_IMAGE = "quay.io/frrouting/frr:9.1.0"


def graphql_query(url: str, token: str, query: str, variables: dict) -> dict:
    """Run a GraphQL query against Nautobot and return data."""
    r = requests.post(
        f"{url}/api/graphql/",
        headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        verify=False,
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        sys.exit(f"GraphQL errors: {body['errors']}")
    return body["data"]


def build_links(devices: list[dict]) -> list[dict]:
    """
    Walk every interface that has a cable. A cable is a single
    object with two terminations; if we naively iterate we'd
    emit each link twice. De-dupe by sorted (iface_id_a, iface_id_b).
    """
    iface_by_id: dict[str, tuple[str, str]] = {}  # iface_id -> (device_name, iface_name)
    for d in devices:
        for i in d["interfaces"]:
            iface_by_id[i["id"]] = (d["name"], i["name"])

    seen: set[tuple[str, str]] = set()
    links: list[dict] = []

    for d in devices:
        for i in d["interfaces"]:
            cable = i.get("cable")
            if not cable:
                continue
            a_id = cable["termination_a_id"]
            b_id = cable["termination_b_id"]
            if a_id not in iface_by_id or b_id not in iface_by_id:
                continue
            key = tuple(sorted([a_id, b_id]))
            if key in seen:
                continue
            seen.add(key)
            a_dev, a_if = iface_by_id[a_id]
            b_dev, b_if = iface_by_id[b_id]
            links.append({"a_dev": a_dev, "a_iface": a_if, "b_dev": b_dev, "b_iface": b_if})
    return links


def shape_devices(devices: list[dict]) -> list[dict]:
    """Reshape GraphQL response into a flat structure for templates."""
    out = []
    # Stable mgmt-ip allocation: 172.20.20.10 + N where N is sorted index
    sorted_devs = sorted(devices, key=lambda d: d["name"])
    for idx, d in enumerate(sorted_devs, start=11):
        primary = (d.get("primary_ip4") or {}).get("address")
        router_id = primary.split("/")[0] if primary else d["name"]

        ifaces = []
        for i in sorted(d["interfaces"], key=lambda x: x["name"]):
            addresses = [a["address"] for a in (i.get("ip_addresses") or [])]
            # Convert /32 host addresses on loopback to network form for OSPF
            networks = []
            for a in addresses:
                net = ipaddress.ip_interface(a).network
                networks.append(str(net))
            ifaces.append({
                "name": i["name"],
                "type": i["type"],
                "addresses": addresses,
                "networks": networks,
            })

        out.append({
            "name": d["name"],
            "mgmt_ip": f"172.20.20.{idx}",
            "router_id": router_id,
            "interfaces": ifaces,
        })
    return out


def render(args) -> None:
    url = os.environ["NAUTOBOT_URL"]
    token = os.environ["NAUTOBOT_TOKEN"]

    here = Path(__file__).parent
    query = (here.parent / "common" / "topology.gql").read_text()

    print(f"Querying Nautobot for devices at location='{args.location}'...")
    data = graphql_query(url, token, query, {"location": [args.location]})
    raw_devices = data["devices"]
    if not raw_devices:
        sys.exit(f"No devices found at location '{args.location}'")
    print(f"  Got {len(raw_devices)} devices")

    devices = shape_devices(raw_devices)
    links = build_links(raw_devices)

    # Render templates with addresses-only context for FRR (use addresses for `ip address` lines)
    env = Environment(
        loader=FileSystemLoader(here / "templates"),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )

    # For FRR, network statements should reference networks not host addresses
    # (router ospf network X.Y.Z.0/24 area 0). Build a tweaked view per device.
    frr_devices = []
    for d in devices:
        frr_d = {
            "name": d["name"],
            "router_id": d["router_id"],
            "interfaces": [
                {
                    "name": i["name"],
                    "addresses": i["addresses"],
                    "networks": i["networks"],
                }
                for i in d["interfaces"]
            ],
        }
        frr_devices.append(frr_d)

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    configs_dir = out_dir / "configs"
    configs_dir.mkdir(exist_ok=True)

    # Topology
    topo_tmpl = env.get_template("topology.clab.yml.j2")
    topo = topo_tmpl.render(
        topology_name=args.name,
        frr_image=args.image,
        devices=devices,
        links=links,
    )
    (out_dir / f"{args.name}.clab.yml").write_text(topo)
    print(f"  Wrote {out_dir / f'{args.name}.clab.yml'}")

    # Per-device configs
    frr_tmpl = env.get_template("frr.conf.j2")
    for d in frr_devices:
        d_dir = configs_dir / d["name"]
        d_dir.mkdir(exist_ok=True)
        (d_dir / "daemons").write_text(FRR_DAEMONS)
        # Replace the `network` lines with `networks` data so OSPF sees subnets, not /32
        # Templates use iface.addresses but we'd rather use networks for OSPF stmts.
        # The simplest fix: just hand the template a tweaked iface list where
        # addresses key holds the host (X/24) and networks key holds (X.Y.Z.0/24).
        # The current frr.conf.j2 uses iface.addresses for both; fix this template
        # later if BGP/multi-area complicate things.
        rendered = frr_tmpl.render(device=d)
        (d_dir / "frr.conf").write_text(rendered)
        print(f"  Wrote {d_dir / 'frr.conf'}")

    # Summary
    print("\nLinks rendered:")
    for l in links:
        print(f"  {l['a_dev']}:{l['a_iface']} <-> {l['b_dev']}:{l['b_iface']}")

    print(f"\nDone. Output in {out_dir}")
    print(f"\nTo deploy on netlab:")
    print(f"  rsync -avz {out_dir}/ ubuntu@192.168.0.253:/opt/netlab/topologies/{args.name}/")
    print(f"  ssh ubuntu@192.168.0.253 'cd /opt/netlab/topologies/{args.name} && sudo containerlab deploy -t {args.name}.clab.yml'")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--location", default="homelab", help="Nautobot location name to query")
    p.add_argument("--name", default="frr-from-nautobot", help="Name of the rendered topology")
    p.add_argument("--image", default=DEFAULT_FRR_IMAGE, help="Container image for nodes")
    p.add_argument("--out", default="./output", help="Output directory")
    args = p.parse_args()
    render(args)


if __name__ == "__main__":
    main()
