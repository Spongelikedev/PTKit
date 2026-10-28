"""Reference state selection and conversion to the existing batch plan format."""
from __future__ import annotations

from . import ops


def preferred_state(states: dict) -> str:
    """Prefer explicit answers/references, then initial/baseline, then current."""
    for words in (("answer", "reference", "target"), ("initial", "baseline")):
        for key in states:
            if any(word in key.lower() for word in words):
                return key
    return next(iter(states), "")


def _text(value):
    if isinstance(value, dict):
        value = value.get("#text")
    return value if isinstance(value, str) and value.strip() else None


def batch_plan(devices: dict, target, source: str) -> tuple[dict, list[str]]:
    """Map supported reference values to operations on the chosen working network.

    Preserve the entire displayed dictionary as review metadata. IOS config text
    supplies routing and virtual interfaces; structured addresses are used when
    there is no running config. Unsupported fields stay visible in the metadata.
    """
    operations, notes = [], []
    for name, reference in devices.items():
        try:
            device = target.device(name)
        except KeyError:
            notes.append(f"{name}: no matching device in the target network")
            continue
        configs = reference.get("configurations", {})
        running = configs.get("runningconfig", {})
        lines = running.get("lines", [])
        has_config = device.is_ios and any(line.strip() not in ("", "!", "end") for line in lines)
        if has_config:
            operations.append({"device": name, "op": "merge", "config": "\n".join(lines)})
        parameters = reference.get("parameters") or {}
        engine = parameters.get("engine", parameters) if isinstance(parameters, dict) else {}
        if not isinstance(engine, dict):
            engine = {}
        hostname = _text(engine.get("sys_name")) or _text(engine.get("hostname"))
        if device.is_ios and hostname and (not has_config or not running.get("hostname")):
            operations.append({"device": name, "op": "hostname", "name": hostname})
        ports = device.ports()
        for key, values in reference.get("interfaces", {}).items():
            if not isinstance(values, dict):
                continue
            # Running-config remains authoritative for IOS interfaces it names.
            if has_config and key in running.get("interfaces", {}):
                continue
            ip = _text(values.get("ip")) or _text(values.get("ip_address"))
            mask = (_text(values.get("subnet")) or _text(values.get("subnet_mask"))
                    or _text(values.get("mask")))
            if not ip or not mask:
                continue
            port = next((p for p in ports if p.name == key), None)
            if port is None and key.startswith("port") and key[4:].isdigit():
                index = int(key[4:])
                port = ports[index] if index < len(ports) else None
            if port is None or (device.is_ios and not port.name):
                notes.append(f"{name}/{key}: no matching editable port")
                continue
            op = {"device": name, "op": "set_ip", "port": port.name or port.index,
                  "ip": ip, "mask": mask}
            if not device.is_ios and (gateway := _text(engine.get("gateway"))):
                op["gateway"] = gateway
            operations.append(op)
    if not operations:
        raise ValueError("No reference values map to editable devices in the selected target network.")
    plan = {"network": target.role, "operations": operations,
            "reference_source": source, "reference_parameters": devices}
    if notes:
        plan["reference_notes"] = notes
    ops.validate_plan(plan)
    return plan, notes
