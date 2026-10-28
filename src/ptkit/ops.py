"""High-level edit operations shared by the CLI and the GUI.

Every function here mutates an in-memory :class:`~ptkit.model.PTFile`;
callers decide when to save. :func:`validate_plan` checks a batch plan's
shape up front so a typo fails before any device is touched.
"""

from __future__ import annotations

import dataclasses
import json
import re

from .iosconfig import is_virtual_interface
from .model import Device, Network, PTFile, interface_family
from .xmlio import XmlDoc

# op name -> required keys (besides "device" and "op")
PLAN_OPS: dict[str, tuple[str, ...]] = {
    "merge": ("config",),
    "replace": ("config",),
    "hostname": ("name",),
    "set_ip": ("port", "ip", "mask"),
    "write_memory": (),
    "rename": ("name",),
}


def clone(pt: PTFile) -> PTFile:
    """Independent copy of a document (same container format/params), so a
    failed edit never leaves a half-modified working copy behind."""
    xml = pt.to_xml()
    return PTFile(dataclasses.replace(pt.decoded, xml=xml), XmlDoc.parse(xml), pt.path)


def apply_snippet(device: Device, snippet: str, *, write_memory: bool = False) -> list[str]:
    """Merge config-mode commands into a device's running-config."""
    if not device.is_ios:
        raise ValueError(f"{device.name} ({device.type}) has no IOS CLI configuration")
    if not snippet.strip():
        raise ValueError("no commands to apply")
    snippet = expand_interface_names(device, snippet)
    ed = device.edit_config()
    with ed as cfg:
        before = set(cfg.interface_names())
        cfg.merge(snippet)
        _reject_unknown_interfaces(device, set(cfg.interface_names()) - before)
    changes = list(ed.changes)
    if write_memory:
        device.write_memory()
        changes.append("running-config copied to startup-config")
    return changes


_IFACE_LINE = re.compile(
    r"^(\s*(?:no\s+)?)int(?:e(?:r(?:f(?:a(?:c(?:e)?)?)?)?)?)?(\s+)(\S+)(.*)$", re.I)
_SHUT_LINE = re.compile(r"^(\s+(?:no\s+)?)shut(?:d(?:o(?:w(?:n)?)?)?)?\s*$", re.I)
# Other abbreviations whose full form ptkit's sync logic keys on.
_ABBREVIATIONS = [
    (re.compile(r"^(\s+(?:no\s+)?)ip\s+add(?:r(?:e(?:s(?:s)?)?)?)?(?=\s|$)", re.I),
     r"\1ip address"),
    (re.compile(r"^(\s+)desc(?:r(?:i(?:p(?:t(?:i(?:o(?:n)?)?)?)?)?)?)?(?=\s)", re.I),
     r"\1description"),
    (re.compile(r"^host(?:n(?:a(?:m(?:e)?)?)?)?(?=\s)", re.I), "hostname"),
]
_VIRTUAL_FULL = {"vl": "Vlan", "lo": "Loopback", "tu": "Tunnel", "po": "Port-channel"}


def expand_interface_names(device: Device, snippet: str) -> str:
    """Rewrite abbreviations the IOS parser accepts (``int g0/1``,
    ``fa0/0``, ``lo0``, ``no shut``) to the full forms the running-config
    uses, so they merge with the existing blocks."""
    ports = [p.name for p in device.ports() if p.name]

    def full(name: str) -> str:
        m = re.match(r"([A-Za-z-]+)\s*([\d/.:]+)$", name)
        if not m:
            return name
        abbr, num = m.group(1).lower(), m.group(2)
        num, dot, sub = num.partition(".")  # sub-interface: expand the parent
        hits = {p for p in ports
                if p.lower() == f"{m.group(1)}{num}".lower()
                or (interface_family(p).lower().startswith(abbr)
                    and p[len(interface_family(p)):] == num)}
        if len(hits) == 1:
            return hits.pop() + dot + sub
        for key, fam in _VIRTUAL_FULL.items():
            if fam.lower().startswith(abbr) and abbr.startswith(key):
                return fam + num + dot + sub
        return name

    out = []
    for line in snippet.splitlines():
        m = _IFACE_LINE.match(line)
        if m and m.group(3).lower() == "range":
            raise ValueError("'interface range' is not supported; list interfaces individually")
        if m:
            line = f"{m.group(1)}interface{m.group(2)}{full(m.group(3))}{m.group(4)}"
        elif (sm := _SHUT_LINE.match(line)):
            line = sm.group(1) + "shutdown"
        else:
            for pat, repl in _ABBREVIATIONS:
                line = pat.sub(repl, line)
        out.append(line)
    return "\n".join(out)


def _reject_unknown_interfaces(device: Device, new: set[str]) -> None:
    """IOS refuses ``interface X`` for hardware the device lacks; do the same
    for physical interfaces when every port's name is known. Raising inside
    the edit_config() block discards the edit."""
    ports = device.ports()
    if not ports or not all(p.name for p in ports):
        return
    known = {p.name.lower() for p in ports}
    bad = sorted(n for n in new if not is_virtual_interface(n) and n.lower() not in known)
    if bad:
        raise ValueError(f"{device.name} ({device.model}) has no interface "
                         f"{', '.join(bad)}; available: {', '.join(p.name for p in ports)}")


def set_ip(device: Device, port: str | int, ip: str, mask: str, *,
           gateway: str | None = None, no_shutdown: bool = False) -> list[str]:
    if device.is_ios:
        name = device.port(port).name
        if not name:
            raise ValueError(f"{device.name}: port {port!r} has no resolvable interface name")
        ed = device.edit_config()
        with ed as cfg:
            cfg.set_interface_ip(name, ip, mask)
            if no_shutdown:
                cfg.set_interface_shutdown(name, False)
        return list(ed.changes)
    device.set_endpoint_ip(port, ip, mask, gateway)
    return [f"port {port} IP -> {ip} {mask}" + (f", gateway {gateway}" if gateway else "")]


def parse_plan(text: str) -> dict:
    """Parse plan JSON. A bare list is accepted as ``{"operations": [...]}``."""
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from e
    if isinstance(plan, list):
        plan = {"operations": plan}
    validate_plan(plan)
    return plan


def validate_plan(plan) -> None:
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object or a list of operations")
    ops = plan.get("operations")
    if not isinstance(ops, list) or not ops:
        raise ValueError('plan needs a non-empty "operations" list')
    if "network" in plan and not isinstance(plan["network"], str):
        raise ValueError('"network" must be a string')
    for n, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"operation {n}: must be an object")
        kind = op.get("op")
        if kind not in PLAN_OPS:
            raise ValueError(f"operation {n}: unknown op {kind!r} "
                             f"(expected one of {', '.join(PLAN_OPS)})")
        missing = [k for k in ("device",) + PLAN_OPS[kind] if k not in op]
        if missing:
            raise ValueError(f"operation {n} ({kind}): missing {', '.join(missing)}")


def apply_plan(pt: PTFile, plan: dict, network: str | None = None
               ) -> tuple[Network, list[str]]:
    """Apply a validated batch plan. ``plan["network"]`` overrides ``network``.
    Returns the network edited and a list of change descriptions."""
    validate_plan(plan)
    net = pt.network(plan.get("network", network))
    changes: list[str] = []
    for op in plan["operations"]:
        dev = net.device(op["device"])
        kind = op["op"]
        if kind == "merge":
            got = apply_snippet(dev, op["config"])
        elif kind == "replace":
            dev.set_running_config(op["config"].splitlines(), sync=False)
            got = dev.sync_from_config() or ["running-config replaced"]
        elif kind == "hostname":
            ed = dev.edit_config()
            with ed as cfg:
                cfg.set_hostname(op["name"])
            got = ed.changes
        elif kind == "set_ip":
            got = set_ip(dev, op["port"], op["ip"], op["mask"],
                         gateway=op.get("gateway"), no_shutdown=bool(op.get("no_shutdown")))
        elif kind == "write_memory":
            dev.write_memory()
            got = ["running-config copied to startup-config"]
        else:  # rename
            dev.name = op["name"]
            got = [f"display name -> {op['name']}"]
        changes += [f"{dev.name}: {c}" for c in got]
    return net, changes


def example_plan(network: Network | None) -> str:
    """A starter plan built from the devices actually in ``network``; every
    operation in it targets a real device and a real, named port."""
    devices = network.devices() if network else []

    def first_port(d: Device) -> str | None:
        return next((p.name for p in d.ports() if p.name), None)

    # IOS devices whose interfaces resolve (home gateways have a running-config
    # but no IOS interface names); routers first.
    ios = sorted((d for d in devices if d.is_ios and first_port(d)),
                 key=lambda d: d.type != "Router")
    ends = [d for d in devices if not d.is_ios and first_port(d)]
    ops: list[dict] = []
    if ios:
        r = ios[0]
        port = first_port(r)
        ops += [
            {"device": r.name, "op": "hostname", "name": "R1"},
            {"device": r.name, "op": "set_ip", "port": port, "ip": "10.0.0.1",
             "mask": "255.255.255.0", "no_shutdown": True},
            {"device": r.name, "op": "merge",
             "config": "router ospf 1\n network 10.0.0.0 0.0.0.255 area 0"},
        ]
        if len(ios) > 1:
            ops.append({"device": ios[1].name, "op": "merge",
                        "config": "ip domain-name lab.local"})
    if ends:
        ops.append({"device": ends[0].name, "op": "set_ip", "port": first_port(ends[0]),
                    "ip": "10.0.0.10", "mask": "255.255.255.0", "gateway": "10.0.0.1"})
    for r in ios[:2]:
        ops.append({"device": r.name, "op": "write_memory"})
    if not ops:
        name = devices[0].name if devices else "Router0"
        ops = [{"device": name, "op": "rename", "name": name}]
    return json.dumps({"operations": ops}, indent=2)
