#!/usr/bin/env python3
"""
Audit live netlab state against Nautobot's declared state.

Connects to the netlab VM via SSH, runs `docker exec` against each FRR
container to pull live interface and IP data, then queries Nautobot via
GraphQL for what should be there. Prints a structured drift report.

Read-only. Reports drift; never writes.

Exits 0 if clean, 1 if drift detected.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()


def ssh_run(host, user, cmd):
    full = ["ssh", "-o", "StrictHostKeyChecking=accept-new", f"{user}@{host}", cmd]
    result = subprocess.run(full, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"ssh failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def list_lab_containers(host, user, topology):
    fmt = '{{.Names}}'
    cmd = f'sudo docker ps --filter "name=clab-{topology}-" --format "{fmt}"'
    out = ssh_run(host, user, cmd)
    return [n.strip() for n in out.splitlines() if n.strip()]


def container_to_device(name, topology):
    prefix = f"clab-{topology}-"
    return name[len(prefix):] if name.startswith(prefix) else name


def gather_live_interfaces(host, user, container):
    raw = ssh_run(host, user, f"sudo docker exec {container} ip -j addr show")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"failed to parse ip -j addr show output from {container}")

    out = []
    for iface in data:
        name = iface.get("ifname")
        if not name or name in ("ip6tnl0", "tunl0"):
            continue
        addrs = []
        for a in iface.get("addr_info", []):
            if a.get("family") == "inet":
                addrs.append(f"{a['local']}/{a['prefixlen']}")
        out.append({"name": name, "addresses": sorted(addrs)})
    return sorted(out, key=lambda x: x["name"])


QUERY = """
query ($location: [String]) {
  devices(location: $location) {
    name
    interfaces {
      name
      ip_addresses { address }
    }
  }
}
"""


def gather_declared(url, token, location):
    r = requests.post(
        f"{url}/api/graphql/",
        headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
        json={"query": QUERY, "variables": {"location": [location]}},
        timeout=30,
        verify=False,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        sys.exit(f"GraphQL errors: {body['errors']}")

    out = {}
    for d in body["data"]["devices"]:
        ifaces = []
        for i in d["interfaces"]:
            ifaces.append({
                "name": i["name"],
                "addresses": sorted(a["address"] for a in i["ip_addresses"]),
            })
        out[d["name"]] = sorted(ifaces, key=lambda x: x["name"])
    return out


@dataclass
class Drift:
    device: str
    kind: str
    detail: str

    def __str__(self):
        return f"  [{self.kind}] {self.device}: {self.detail}"


def diff(live, declared):
    drifts = []
    live_names = set(live)
    decl_names = set(declared)

    for name in sorted(live_names - decl_names):
        drifts.append(Drift(name, "device_only_in_live",
                            "device exists on netlab but not in Nautobot"))
    for name in sorted(decl_names - live_names):
        drifts.append(Drift(name, "device_only_in_sot",
                            "device declared in Nautobot but not running on netlab"))

    for name in sorted(live_names & decl_names):
        l_ifaces = {i["name"]: i["addresses"] for i in live[name]}
        d_ifaces = {i["name"]: i["addresses"] for i in declared[name]}

        for iface in sorted(d_ifaces.keys() - l_ifaces.keys()):
            drifts.append(Drift(name, "iface_only_in_sot",
                                f"{iface} declared but missing on device"))

        for iface in sorted(l_ifaces.keys() & d_ifaces.keys()):
            l_addrs = set(l_ifaces[iface])
            if iface == "lo":
                l_addrs = {a for a in l_addrs
                           if not a.startswith("127.") and not a.startswith("::1")}
            d_addrs = set(d_ifaces[iface])
            if l_addrs != d_addrs:
                only_live = sorted(l_addrs - d_addrs)
                only_decl = sorted(d_addrs - l_addrs)
                parts = []
                if only_live:
                    parts.append(f"only on device: {only_live}")
                if only_decl:
                    parts.append(f"only in Nautobot: {only_decl}")
                drifts.append(Drift(name, "addr_mismatch",
                                    f"{iface}: " + "; ".join(parts)))
    return drifts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--location", default="homelab")
    p.add_argument("--netlab-host", default="192.168.0.253")
    p.add_argument("--netlab-user", default="ubuntu")
    p.add_argument("--topology", default="frr-from-nautobot")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    nb_url = os.environ["NAUTOBOT_URL"]
    nb_token = os.environ["NAUTOBOT_TOKEN"]

    print(f"Discovering containers for topology '{args.topology}' on {args.netlab_host}...",
          file=sys.stderr)
    containers = list_lab_containers(args.netlab_host, args.netlab_user, args.topology)
    if not containers:
        sys.exit(f"No containers found for topology '{args.topology}'.")
    print(f"  Found {len(containers)} containers", file=sys.stderr)

    live = {}
    for c in containers:
        device = container_to_device(c, args.topology)
        ifaces = gather_live_interfaces(args.netlab_host, args.netlab_user, c)
        live[device] = ifaces
        print(f"  Live: {device} ({len(ifaces)} interfaces)", file=sys.stderr)

    print(f"Querying Nautobot ({nb_url}) for location '{args.location}'...",
          file=sys.stderr)
    declared = gather_declared(nb_url, nb_token, args.location)
    print(f"  Declared: {len(declared)} devices", file=sys.stderr)

    drifts = diff(live, declared)

    if args.json:
        print(json.dumps([d.__dict__ for d in drifts], indent=2))
    else:
        if drifts:
            print(f"\nDrift detected: {len(drifts)} issue(s)\n")
            for d in drifts:
                print(d)
        else:
            print("\nClean. Live state matches Nautobot.")

    return 1 if drifts else 0


if __name__ == "__main__":
    sys.exit(main())
