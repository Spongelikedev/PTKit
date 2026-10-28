"""Schema-independent, read-only topology inspection of unpacked XML."""
from __future__ import annotations

from .iosconfig import IosConfig


def _tag(el):
    return el.tag.rsplit("}", 1)[-1].upper() if isinstance(el.tag, str) else ""


def _add(mapping, key, value):
    if key not in mapping:
        mapping[key] = value
    elif isinstance(mapping[key], list):
        mapping[key].append(value)
    else:
        mapping[key] = [mapping[key], value]


def extract_topologies(doc) -> dict:
    """Return ``{state: {device: {parameters, interfaces, configurations}}}``.

    Inspect every DEVICE, including devices in unknown wrappers. XML parameters
    retain their hierarchy; repeated siblings become lists, attributes use @keys,
    and empty leaves become None. State/device collisions get numbered suffixes
    rather than overwriting values. No source elements are modified.
    """
    def value(el):
        children = [c for c in el if _tag(c)]
        text = doc.from_tree(el.text)
        text = text.strip() if text and text.strip() else None
        if not children and not el.attrib:
            return text
        out = {"@" + k: doc.from_tree(v) for k, v in el.attrib.items()}
        if text is not None:
            out["#text"] = text
        for child in children:
            _add(out, _tag(child).lower(), value(child))
        return out

    def name(el, tags=("NAME", "HOSTNAME", "SYS_NAME")):
        attrs = {k.upper(): v for k, v in el.attrib.items()}
        for tag in tags:
            for child in el:
                if _tag(child) == tag and child.text and child.text.strip():
                    return doc.from_tree(child.text).strip()
            if attrs.get(tag):
                return doc.from_tree(attrs[tag])
        return None

    def unique(mapping, key):
        candidate, i = key, 2
        while candidate in mapping:
            candidate = f"{key}#{i}"
            i += 1
        return candidate

    result, states = {}, {}
    pt_roots = [c for c in doc.root if _tag(c) in ("PACKETTRACER", "PACKETTRACER5")]
    for device in doc.root.iter():
        if _tag(device) != "DEVICE":
            continue
        ancestors = list(device.iterancestors())
        state = next((a for a in ancestors if _tag(a).endswith(
            ("_NETWORK", "_STATE", "_TOPOLOGY"))), None)
        if state is None:
            state = next((a for a in ancestors if _tag(a) in
                          ("PACKETTRACER", "PACKETTRACER5")), doc.root)
        if state not in states:
            role = _tag(state).lower()
            if state in pt_roots and len(pt_roots) == 3:
                role = ("user", "initial", "answer")[pt_roots.index(state)]
            elif state is doc.root or _tag(state) in ("PACKETTRACER", "PACKETTRACER5"):
                role = "network"
            else:
                role = role.removesuffix("_network")
            states[state] = unique(result, role)
            result[states[state]] = {}
        devices = result[states[state]]
        engine = next((c for c in device if _tag(c) == "ENGINE"), device)
        device_name = name(engine) or name(device) or f"device{len(devices)}"
        interfaces, configurations = {}, {}
        # Reuse physical port naming where the established model understands it.
        from .model import Device
        ports = Device(device, doc, 0).ports() if engine is not device else []
        port_names = {p.el: p.name for p in ports}
        for node in device.iter():
            tag = _tag(node)
            if tag in ("PORT", "INTERFACE") and (len(node) or node.attrib):
                key = name(node, ("NAME", "INTERFACE_NAME", "PORT_NAME"))
                key = key or port_names.get(node) or f"port{len(interfaces)}"
                interfaces[unique(interfaces, key)] = value(node)
            if tag in ("RUNNINGCONFIG", "STARTUPCONFIG"):
                lines = [doc.from_tree(c.text) or "" for c in node if _tag(c) == "LINE"]
                cfg = IosConfig(lines)
                configurations[tag.lower()] = {
                    "hostname": cfg.hostname(), "lines": lines,
                    "interfaces": {n: {"ip": s.ip, "subnet": s.mask,
                                       "shutdown": s.shutdown, "description": s.description}
                                   for n in cfg.interface_names()
                                   if (s := cfg.interface(n)) is not None},
                }
        devices[unique(devices, device_name)] = {
            "parameters": value(device), "interfaces": interfaces,
            "configurations": configurations,
        }
    return result
