"""Export a container to an editable XML folder, and import it back.

Export folder layout::

    manifest.json          what was exported, from which file, checksums
    document.xml           the complete document, including activity/grading data
    network-user.xml       each network as its own document (.pka only)
    network-initial.xml
    network-answer.xml

Import accepts an export folder (via its manifest) or a single XML file:

* folder, complete export: if ``document.xml`` was edited it is used as-is;
  otherwise every edited ``network-*.xml`` is spliced back into the document.
  Editing both is ambiguous and rejected.
* legacy user-only export: only ``network-user.xml`` was exported; on import it
  is spliced into the original container (which must be unchanged since the
  export), so the hidden networks and grading data are preserved byte-for-byte.
* single XML file: a complete document is encoded on its own (a
  ``PACKETTRACER5`` network file becomes a .pkt).

Splicing swaps exactly one network element for the imported bytes, so an
import can only change the network it names.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path

from lxml import etree

from . import codec
from .model import NETWORK_TAGS, PKA_NETWORK_ROLES, PTFile, Network
from .xmlio import XmlDoc, XmlFidelityError

MANIFEST = "manifest.json"
DOCUMENT = "document.xml"
BUNDLE_VERSION = 1
_SPLICE_MARK = "PTKIT_SPLICE_7f3c1e"


class BundleError(ValueError):
    """The export folder or XML file cannot be imported."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def network_file(role: str) -> str:
    return f"network-{role}.xml"


def _network_elements(pt: PTFile) -> list:
    """All network elements of the document in order, including ones the
    model hides for locked activities (needed to splice correctly)."""
    if not pt.is_activity:
        return [pt.root]
    return [c for c in pt.root if c.tag in NETWORK_TAGS]


@dataclasses.dataclass
class ExportResult:
    folder: Path
    files: dict[str, int]  # name -> size in bytes
    locked: bool


def export_bundle(pt: PTFile, folder: str | Path, source: Path | None = None) -> ExportResult:
    """Write ``pt`` (as currently held in memory) to ``folder``."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(source or pt.path) if (source or pt.path) else None
    locked = pt.is_protected
    files: dict[str, bytes] = {DOCUMENT: pt.to_xml()}
    nets = _network_elements(pt) if pt.is_activity else []
    roles = list(PKA_NETWORK_ROLES) if len(nets) == 3 else [f"net{i}" for i in range(len(nets))]
    if pt.is_activity:
        for role, el in zip(roles, nets):
            files[network_file(role)] = pt.doc.fragment(el)

    manifest = {
        "ptkit_bundle": BUNDLE_VERSION,
        # Complete bundles do not need a machine-specific source path to import.
        "source": source.name if source else None,
        "source_sha256": _sha(source.read_bytes()) if source and source.is_file() else None,
        "kind": "pka" if pt.is_activity else "pkt",
        "container_format": pt.decoded.format.value,
        "zlib_level": pt.decoded.zlib_params.level if pt.decoded.zlib_params else None,
        "pt_version": pt.version,
        "locked_by_author": locked,
        "networks": roles,
        "files": {name: _sha(data) for name, data in files.items()},
    }
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    sizes = {name: len(data) for name, data in files.items()}
    sizes[MANIFEST] = (folder / MANIFEST).stat().st_size
    return ExportResult(folder, sizes, locked)


@dataclasses.dataclass
class ImportResult:
    xml: bytes
    fmt: codec.Format
    params: codec.ZlibParams | None
    suffix: str            # ".pkt" or ".pka"
    messages: list[str]    # human-readable steps taken / warnings


def _clean_xml_file(data: bytes, *, keep_tail: bool = False) -> bytes:
    """Undo common editor changes: a UTF-8 BOM, an XML declaration and
    surrounding whitespace. ``keep_tail`` preserves trailing bytes (a full PT
    document ends with a newline, PT 4.x additionally with a NUL)."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.lstrip() if keep_tail else data.strip()
    if data.startswith(b"<?xml"):
        data = data[data.index(b"?>") + 2:].lstrip()
    return data


def _parse_checked(data: bytes, what: str) -> XmlDoc:
    try:
        return XmlDoc.parse(data)
    except XmlFidelityError as e:
        raise BundleError(f"{what}: invalid XML: {e}") from e


def splice_network(pt: PTFile, role: str, fragment: bytes) -> bytes:
    """Return the document XML with network ``role`` replaced by ``fragment``."""
    nets = _network_elements(pt)
    roles = list(PKA_NETWORK_ROLES) if len(nets) == 3 else [f"net{i}" for i in range(len(nets))]
    if role not in roles:
        raise BundleError(f"no {role!r} network in the source (have: {roles})")
    target = nets[roles.index(role)]
    fragment = _clean_xml_file(fragment)
    frag_doc = _parse_checked(fragment, network_file(role))
    if frag_doc.root.tag != target.tag:
        raise BundleError(f"{network_file(role)}: root element is <{frag_doc.root.tag}>, "
                          f"expected <{target.tag}>")
    work = PTFile(pt.decoded, XmlDoc.parse(pt.to_xml()), pt.path)  # don't touch ``pt``
    work_target = _network_elements(work)[roles.index(role)]
    mark = etree.Element(_SPLICE_MARK)
    mark.tail = work_target.tail
    work_target.getparent().replace(work_target, mark)
    out = work.to_xml()
    token = f"<{_SPLICE_MARK}/>".encode()
    if out.count(token) != 1:
        raise BundleError("internal error: splice marker not unique")
    return out.replace(token, fragment)


def _validate_document(xml: bytes, what: str, messages: list[str]) -> PTFile:
    _parse_checked(xml, what)
    try:
        pt = PTFile.from_xml(xml)
        roots = ("PACKETTRACER5", "PACKETTRACER", "PACKETTRACER5_ACTIVITY",
                 "PACKETTRACER_ACTIVITY")
        if pt.root.tag not in roots:
            raise BundleError(f"{what}: root <{pt.root.tag}> is not a Packet Tracer document")
        elements = _network_elements(pt)
        nets = [Network(el, pt.doc, f"net{i}") for i, el in enumerate(elements)]
        devices = sum(len(n.devices()) for n in nets)
        issues = [f"[{n.role}] {m}" for n in nets for m in n.check()]
    except BundleError:
        raise
    except Exception as e:  # model-level problem: report, don't crash
        raise BundleError(f"{what}: not a usable Packet Tracer document: {e}") from e
    messages.append(f"validated {what}: well-formed, {len(nets)} network(s), "
                    f"{devices} device(s)")
    messages += [f"warning: {i}" for i in issues]
    return pt


def guess_format(xml: bytes) -> codec.Format:
    """Container format for XML with no recorded origin: PT 4.x documents get
    the legacy container they were made for; everything else modern."""
    m = re.search(rb"<VERSION>\s*(\d+)\.", xml[:400])
    return codec.Format.LEGACY if m and int(m.group(1)) < 5 else codec.Format.MODERN


def import_path(path: str | Path) -> ImportResult:
    path = Path(path)
    if path.is_dir() or path.name == MANIFEST:
        return _import_folder(path if path.is_dir() else path.parent)
    return _import_file(path)


def _import_file(path: Path) -> ImportResult:
    if not path.is_file():
        raise BundleError(f"{path}: not found")
    xml = _clean_xml_file(path.read_bytes(), keep_tail=True)
    messages: list[str] = []
    pt = _validate_document(xml, path.name, messages)
    fmt = guess_format(xml)
    messages.append(f"no manifest: using the {fmt.value} container format")
    return ImportResult(xml, fmt, None, ".pka" if pt.is_activity else ".pkt", messages)


def _import_folder(folder: Path) -> ImportResult:
    mpath = folder / MANIFEST
    if not mpath.is_file():
        raise BundleError(f"{folder}: no {MANIFEST} (not a ptkit export folder); "
                          "import a single XML file instead")
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as e:
        raise BundleError(f"{MANIFEST}: invalid JSON: {e}") from e
    if manifest.get("ptkit_bundle") != BUNDLE_VERSION:
        raise BundleError(f"{MANIFEST}: unsupported bundle version")
    fmt = codec.Format(manifest["container_format"])
    params = (codec.ZlibParams(level=manifest["zlib_level"])
              if manifest.get("zlib_level") is not None else None)
    suffix = "." + manifest["kind"]
    messages: list[str] = []

    def changed(name: str) -> bool:
        f = folder / name
        if not f.is_file():
            raise BundleError(f"{name} is missing from the export folder")
        return _sha(f.read_bytes()) != manifest["files"][name]

    recorded = manifest["files"]
    net_files = {r: network_file(r) for r in manifest.get("networks", [])
                 if network_file(r) in recorded}
    edited_nets = [r for r, name in net_files.items() if changed(name)]

    if manifest.get("locked_by_author") and DOCUMENT not in recorded:
        source = Path(manifest.get("source") or "")
        if not source.is_file():
            raise BundleError(f"locked activity: the original file is needed to re-encode "
                              f"and was not found at {source}")
        raw = source.read_bytes()
        if _sha(raw) != manifest.get("source_sha256"):
            raise BundleError(f"locked activity: {source.name} has changed since the export; "
                              "export again and redo your edits")
        pt = PTFile.from_bytes(raw, source)
        xml = splice_network(pt, "user", (folder / network_file("user")).read_bytes())
        messages.append(f"spliced {network_file('user')} into {source.name}"
                        + ("" if edited_nets else " (unchanged)"))
    else:
        doc_edited = changed(DOCUMENT)
        if doc_edited and edited_nets:
            raise BundleError(f"both {DOCUMENT} and {', '.join(net_files[r] for r in edited_nets)} "
                              "were edited; keep your changes in one of them")
        xml = (folder / DOCUMENT).read_bytes()
        if doc_edited:
            messages.append(f"using edited {DOCUMENT}")
        elif edited_nets:
            pt = PTFile.from_xml(xml, fmt)
            for role in edited_nets:
                xml = splice_network(pt, role, (folder / net_files[role]).read_bytes())
                pt = PTFile.from_xml(xml, fmt)
                messages.append(f"spliced edited {net_files[role]} into the document")
        else:
            messages.append("no files were edited since the export; re-encoding as-is")
    _validate_document(xml, "imported document", messages)
    return ImportResult(xml, fmt, params, suffix, messages)


def write_container(result: ImportResult, out: str | Path) -> Path:
    """Encode, write atomically and verify (decode + lossless re-parse)."""
    out = Path(out)
    codec.encode_file(out, result.xml, result.fmt, result.params, verify=True)
    again = PTFile.load(out)
    if again.to_xml() != result.xml:
        raise BundleError("read-back verification failed: output differs from the input XML")
    return out
