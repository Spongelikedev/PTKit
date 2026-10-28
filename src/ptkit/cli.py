"""Command-line interface: ``ptkit <command> ...`` (see ``ptkit -h``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codec, ops, xmlbundle
from .htmltext import html_to_text
from .model import Device, PTFile
from .xmlio import XmlDoc, XmlFidelityError


def _out_path(args) -> Path:
    if getattr(args, "in_place", False):
        return Path(args.file)
    if not args.output:
        sys.exit("error: give -o OUTPUT or --in-place")
    return Path(args.output)


def _device(pt: PTFile, args) -> Device:
    return pt.network(args.network).device(args.device)


def _read_text(src: str) -> str:
    return sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")


# --- commands ----------------------------------------------------------------

def cmd_decode(args) -> int:
    d = codec.decode_file(args.file)
    out = Path(args.output or args.file + ".xml")
    out.write_bytes(d.xml)
    meta = {"format": d.format.value, "zlib_level": d.zlib_params.level if d.zlib_params else None}
    print(f"wrote {out} ({len(d.xml)} bytes XML, {meta})")
    return 0


def cmd_encode(args) -> int:
    xml = Path(args.file).read_bytes()
    XmlDoc.parse(xml)  # refuse malformed XML before producing a container
    fmt = codec.Format.LEGACY if args.legacy else codec.Format.MODERN
    params = codec.ZlibParams(level=args.level)
    blob = codec.encode_file(args.output, xml, fmt, params)
    print(f"wrote {args.output} ({len(blob)} bytes, {fmt.value})")
    return 0


def cmd_verify(args) -> int:
    """Full integrity loop: bytes -> XML -> tree -> XML -> bytes."""
    raw = Path(args.file).read_bytes()
    pt = PTFile.from_bytes(raw)
    xml_ok = pt.to_xml() == pt.decoded.xml
    bytes_ok = pt.to_bytes() == raw
    print(f"format={pt.decoded.format.value} xml_roundtrip={'ok' if xml_ok else 'FAIL'} "
          f"container_roundtrip={'identical' if bytes_ok else 'differs'}")
    if not bytes_ok and pt.decoded.zlib_params is None:
        print("note: original zlib settings not reproducible; re-encoded file is valid "
              "but not byte-identical")
    return 0 if xml_ok and (bytes_ok or pt.decoded.zlib_params is None) else 1


def cmd_info(args) -> int:
    pt = PTFile.load(args.file)
    info = {
        "file": args.file,
        "kind": "activity (.pka)" if pt.is_activity else "network (.pkt)",
        "container_format": pt.decoded.format.value,
        "pt_version": pt.version,
        "xml_bytes": len(pt.decoded.xml),
        "networks": [{"role": n.role, "devices": len(n.devices()), "links": len(n.links())}
                     for n in pt.networks()],
    }
    if pt.is_protected:
        info["locked_by_author"] = "yes: only the 'user' network is available"
    if pt.activity:
        info["activity"] = pt.activity.summary()
    _emit(info, args.json)
    return 0


def cmd_devices(args) -> int:
    net = PTFile.load(args.file).network(args.network)
    devs = [d.to_dict() for d in net.devices()]
    if args.json:
        print(json.dumps(devs, indent=2))
        return 0
    for d in devs:
        print(f"[{d['index']}] {d['name']}  {d['type']}/{d['model']}  hostname={d['hostname']}")
        for p in d["ports"]:
            if p["ip"] or args.all_ports:
                label = p["name"] or f"port{p['index']}"
                print(f"      {label:<24} {p['type'] or '':<24} {p['ip'] or '-'} "
                      f"{p['subnet'] or ''} {'up' if p['power'] else 'down'}")
    return 0


def cmd_links(args) -> int:
    net = PTFile.load(args.file).network(args.network)
    for i, link in enumerate(net.links()):
        (a, pa), (b, pb) = link.endpoints
        da, db = net.resolve_ref(a), net.resolve_ref(b)
        print(f"[{i}] {da.name if da else a}:{pa}  <->  {db.name if db else b}:{pb}  ({link.type})")
    return 0


def cmd_show_config(args) -> int:
    dev = _device(PTFile.load(args.file), args)
    lines = dev.startup_config if args.startup else dev.running_config
    print("\n".join(lines))
    return 0


def _save(pt: PTFile, args, changes: list[str]) -> int:
    out = pt.save(_out_path(args))
    for c in changes:
        print(f"  synced: {c}")
    issues = pt.network(args.network).check()
    for i in issues:
        print(f"  warning: {i}")
    print(f"wrote {out}")
    return 0


def cmd_apply(args) -> int:
    pt = PTFile.load(args.file)
    changes = ops.apply_snippet(_device(pt, args), _read_text(args.snippet),
                                write_memory=args.write_memory)
    return _save(pt, args, changes)


def cmd_replace_config(args) -> int:
    pt = PTFile.load(args.file)
    dev = _device(pt, args)
    lines = _read_text(args.config).splitlines()
    if args.startup:
        dev.set_startup_config(lines)
        changes = []
    else:
        dev.set_running_config(lines, sync=False)
        changes = dev.sync_from_config()
    return _save(pt, args, changes)


def cmd_set_hostname(args) -> int:
    pt = PTFile.load(args.file)
    dev = _device(pt, args)
    ed = dev.edit_config()
    with ed as cfg:
        cfg.set_hostname(args.hostname)
    if args.rename:
        dev.name = args.hostname
    return _save(pt, args, ed.changes)


def cmd_set_ip(args) -> int:
    pt = PTFile.load(args.file)
    changes = ops.set_ip(_device(pt, args), args.port, args.ip, args.mask,
                         gateway=args.gateway, no_shutdown=args.no_shutdown)
    return _save(pt, args, changes)


def cmd_batch(args) -> int:
    """Apply a JSON plan: {"network": role?, "operations": [...]}."""
    plan = ops.parse_plan(_read_text(args.plan))
    pt = PTFile.load(args.file)
    net, changes = ops.apply_plan(pt, plan, args.network)
    args.network = net.role if pt.is_activity else None
    return _save(pt, args, changes)


def cmd_export(args) -> int:
    pt = PTFile.load(args.file)
    res = xmlbundle.export_bundle(pt, args.output)
    for name, size in res.files.items():
        print(f"  wrote {res.folder / name} ({size:,} bytes)")
    if res.locked:
        print("note: complete activity XML exported, including all networks and grading data")
    print(f"exported to {res.folder}")
    return 0


def cmd_import(args) -> int:
    res = xmlbundle.import_path(args.source)
    for m in res.messages:
        print(f"  {m}")
    out = Path(args.output)
    if out.suffix.lower() not in (".pkt", ".pka"):
        out = out.with_suffix(res.suffix)
    xmlbundle.write_container(res, out)
    print(f"wrote {out} ({out.stat().st_size:,} bytes, {res.fmt.value} container, verified)")
    return 0


def cmd_check(args) -> int:
    pt = PTFile.load(args.file)
    issues = []
    for net in pt.networks():
        issues += [f"[{net.role}] {m}" for m in net.check()]
    print("\n".join(issues) if issues else "consistent: config text and structured state agree")
    return 1 if issues else 0


def cmd_activity(args) -> int:
    pt = PTFile.load(args.file)
    act = pt.activity
    if act is None:
        sys.exit("error: not an activity (.pka) file")
    show_all = args.all and not act.protected
    if args.all and act.protected:
        print("note: --all is unavailable for an activity locked by its author; "
              "showing the graded checklist", file=sys.stderr)
    items = act.assessment_items() if show_all else act.checklist()
    data = {
        "summary": act.summary(),
        "instructions": ([html_to_text(p) for p in act.instructions()]
                         if args.instructions else None),
        "items": [_item_row(i, show_all) for i in items],
        "note": "lists what is checked; expected values are not shown",
    }
    _emit(data, args.json)
    return 0


def _item_row(item, with_graded: bool) -> dict:
    """One checklist row; empty fields (PT 4.x has no points) are omitted and
    the "Network" root shared by every item is dropped."""
    parts = item.parts[1:] if len(item.parts) > 1 and item.parts[0] == "Network" else item.parts
    row = {"item": " > ".join(parts), "points": item.points, "component": item.components}
    if with_graded:
        row["graded"] = item.graded
    return {k: v for k, v in row.items() if v is not None}


def _emit(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
        return

    def show(obj, indent=0):
        pad = "  " * indent
        if isinstance(obj, dict):
            for k, v in obj.items():
                if v is None:
                    continue
                if isinstance(v, (dict, list)):
                    print(f"{pad}{k}:")
                    show(v, indent + 1)
                else:
                    print(f"{pad}{k}: {v}")
        elif isinstance(obj, list):
            for v in obj:
                if isinstance(v, dict):
                    print(f"{pad}- " + ", ".join(f"{k}={x}" for k, x in v.items()))
                else:
                    print(f"{pad}- {v}")

    show(data)


# --- argument parsing --------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ptkit", description="Cisco Packet Tracer .pkt/.pka toolkit")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_, *, device=False, output=False, network=True):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("file", help="input .pkt/.pka (or .xml for encode)")
        if network:
            sp.add_argument("-n", "--network", default=None,
                            help="network role in a .pka: user | initial | answer (default: first)")
        if device:
            sp.add_argument("device", help="device display name or hostname")
        if output:
            g = sp.add_mutually_exclusive_group()
            g.add_argument("-o", "--output", help="output file")
            g.add_argument("--in-place", action="store_true", help="overwrite the input file")
        sp.set_defaults(func=fn)
        return sp

    sp = add("decode", cmd_decode, "unpack a container to XML", network=False)
    sp.add_argument("-o", "--output", help="output .xml (default: FILE.xml)")

    sp = add("encode", cmd_encode, "pack XML into a container", network=False)
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--legacy", action="store_true", help="legacy XOR+zlib format (PT 5.x era)")
    sp.add_argument("--level", type=int, default=6, help="zlib level (default 6, as PT)")

    add("verify", cmd_verify, "check lossless decode/encode round-trip", network=False)

    sp = add("info", cmd_info, "summarise a file", network=False)
    sp.add_argument("--json", action="store_true")

    sp = add("devices", cmd_devices, "list devices and addressed ports")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("-a", "--all-ports", action="store_true", help="include unaddressed ports")

    add("links", cmd_links, "list cabling")

    sp = add("show-config", cmd_show_config, "print a device's IOS config", device=True)
    sp.add_argument("--startup", action="store_true")

    sp = add("apply", cmd_apply, "merge a config snippet into running-config",
             device=True, output=True)
    sp.add_argument("snippet", help="file with config-mode commands, or - for stdin")
    sp.add_argument("--write-memory", action="store_true", help="also copy to startup-config")

    sp = add("replace-config", cmd_replace_config, "replace a device's config wholesale",
             device=True, output=True)
    sp.add_argument("config", help="config text file, or - for stdin")
    sp.add_argument("--startup", action="store_true", help="replace startup-config instead")

    sp = add("set-hostname", cmd_set_hostname, "set IOS hostname", device=True, output=True)
    sp.add_argument("hostname")
    sp.add_argument("--rename", action="store_true", help="also change the display name")

    sp = add("set-ip", cmd_set_ip, "set an interface IPv4 address", device=True, output=True)
    sp.add_argument("port", help="interface name (IOS) or port index")
    sp.add_argument("ip")
    sp.add_argument("mask")
    sp.add_argument("--gateway", help="default gateway (end devices)")
    sp.add_argument("--no-shutdown", action="store_true", help="also enable the interface")

    sp = add("batch", cmd_batch, "apply a JSON plan of operations", output=True)
    sp.add_argument("plan", help="JSON plan file, or - for stdin")

    add("check", cmd_check, "verify config text and structured state agree", network=False)

    sp = add("export", cmd_export, "unpack to an editable XML folder (+ manifest)",
             network=False)
    sp.add_argument("-o", "--output", required=True, help="output folder")

    sp = sub.add_parser("import", help="validate an XML export folder/file and re-encode it",
                        description="validate an XML export folder/file and re-encode it")
    sp.add_argument("source", help="export folder (with manifest.json) or a single .xml")
    sp.add_argument("-o", "--output", required=True, help="output .pkt/.pka")
    sp.set_defaults(func=cmd_import)

    sp = add("activity", cmd_activity,
             "show .pka instructions and the graded checklist (what is checked, not values)",
             network=False)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--instructions", action="store_true", help="include instruction text")
    sp.add_argument("--all", action="store_true",
                    help="list every tree node incl. ungraded ones (unlocked activities only)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (codec.CodecError, XmlFidelityError, KeyError, ValueError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
