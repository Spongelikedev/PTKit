"""Object model over a decoded Packet Tracer document.

``PTFile`` owns the container metadata (format, zlib parameters) and a
source-preserving :class:`~ptkit.xmlio.XmlDoc`. ``Network`` / ``Device`` /
``Port`` / ``Link`` are thin views that read and write the underlying lxml
elements directly, so anything not modelled here remains reachable via
``.el`` and is preserved untouched on save.

Document layout (observed in Packet Tracer 6.x-8.x files)::

    PACKETTRACER5                       (.pkt root)
      VERSION
      NETWORK/DEVICES/DEVICE/ENGINE     TYPE@model, NAME, SYS_NAME, MODULE.../PORT,
                                        RUNNINGCONFIG/LINE*, STARTUPCONFIG/LINE*,
                                        SAVE_REF_ID (7.x+)
      NETWORK/LINKS/LINK/CABLE          FROM, PORT, TO, PORT, TYPE

    PACKETTRACER5_ACTIVITY              (.pka root)
      PACKETTRACER5 x3                  user, initial, answer network (inferred)
      COMPARISONS/NODE...               assessment tree
      ACTIVITY/INSTRUCTIONS/PAGE*       instruction pages
"""

from __future__ import annotations

import difflib
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from . import codec
from .iosconfig import IosConfig, is_virtual_interface
from .xmlio import XmlDoc

PKA_NETWORK_ROLES = ("user", "initial", "answer")

# Root/network element names: PT 5.0+ appends "5"; PT 4.x files do not.
ACTIVITY_ROOTS = ("PACKETTRACER5_ACTIVITY", "PACKETTRACER_ACTIVITY")
NETWORK_TAGS = ("PACKETTRACER5", "PACKETTRACER")


class ProtectedActivityError(ValueError):
    """The activity is locked by its author (Activity Wizard password)."""


LOCKED_MESSAGE = (
    "this activity is locked by its author (Activity Wizard password), so the "
    "configuration editor targets your working ('user') network. Complete XML "
    "export and recursive topology inspection include all network states and "
    "grading data.")


def is_protected_xml(xml: bytes) -> bool:
    """Cheap check on raw XML for an author-locked activity (non-empty
    ACTIVITY@PASS under an activity root), usable before full parsing."""
    head = xml[:200].lstrip()
    if not any(head.startswith(b"<" + r.encode()) for r in ACTIVITY_ROOTS):
        return False
    return re.search(rb'<ACTIVITY\b[^>]*\bPASS="[^"]+"', xml) is not None

IOS_DEVICE_TYPES = {"Router", "Switch", "MultiLayerSwitch", "ASA", "WirelessLanController",
                    "CellTower", "CentralOfficeServer"}


# --- element helpers ---------------------------------------------------------

def port_family(port_type: str) -> str:
    """Map a <PORT><TYPE> value to the IOS interface family name,
    e.g. eCopperFastEthernet -> FastEthernet, eSmartSerial -> Serial."""
    t = port_type[1:] if port_type.startswith("e") else port_type
    for prefix in ("Copper", "Fiber"):
        if t.startswith(prefix):
            t = t[len(prefix):]
    if t.endswith("Serial"):
        return "Serial"
    if t.startswith("AccessPointWireless"):
        return "Dot11Radio"
    return t


def interface_family(name: str) -> str:
    """'GigabitEthernet0/0/1' -> 'GigabitEthernet'."""
    return re.sub(r"[\d/:.]+$", "", name)


def _indent_of(el) -> str:
    """Whitespace that precedes ``el`` on its line."""
    prev = el.getprevious()
    ws = prev.tail if prev is not None else el.getparent().text
    ws = ws or "\n"
    return ws[ws.rfind("\n") + 1:] if "\n" in ws else ws


def _set_child_text(parent, tag: str, value: str, doc: XmlDoc, create: bool = True):
    child = parent.find(tag)
    if child is None:
        if not create:
            return None
        child = etree.SubElement(parent, tag)
        _fix_new_child_ws(parent, child)
    child.text = doc.to_tree(value)
    return child


def _fix_new_child_ws(parent, child) -> None:
    """Give a freshly appended child the same indentation as its siblings."""
    sibs = [c for c in parent if c is not child]
    if sibs:
        last = sibs[-1]
        child.tail = last.tail
        last.tail = parent.text if parent.text and parent.text.strip() == "" else last.tail
    else:
        ind = _indent_of(parent)
        parent.text = "\n" + ind + " "
        child.tail = "\n" + ind


def _replace_lines(container, lines: list[str], doc: XmlDoc) -> None:
    """Make the <LINE> children of ``container`` equal ``lines``.

    Existing elements are kept for lines that did not change (matched with a
    diff), so the serializer re-emits their original bytes; only inserted or
    edited lines become new elements. This keeps per-line encoding quirks
    (e.g. PT 4.x ``&#x3;`` banner delimiters) on untouched lines intact.
    """
    old = list(container)
    if old:
        child_ws = container.text or "\n"
        close_ws = old[-1].tail or "\n"
    else:
        ind = _indent_of(container)
        child_ws = "\n" + ind + " "
        close_ws = "\n" + ind
    old_text = [doc.from_tree(e.text) or "" for e in old]
    keep = []
    matcher = difflib.SequenceMatcher(a=old_text, b=lines, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            keep += old[i1:i2]
        else:
            for line in lines[j1:j2]:
                e = etree.Element("LINE")
                e.text = doc.to_tree(line)
                keep.append(e)
    for c in old:
        container.remove(c)
    if not keep:
        container.text = None
        return
    container.text = child_ws
    for i, e in enumerate(keep):
        container.append(e)
        tail = child_ws if i < len(keep) - 1 else close_ws
        if e.tail != tail:
            e.tail = tail


# --- views -------------------------------------------------------------------

@dataclass
class Port:
    el: object
    doc: XmlDoc = field(repr=False)
    index: int
    name: str | None = None

    def _get(self, tag):
        return self.doc.from_tree(self.el.findtext(tag))

    @property
    def type(self) -> str | None:
        return self._get("TYPE")

    @property
    def ip(self) -> str | None:
        return self._get("IP") or None

    @property
    def subnet(self) -> str | None:
        return self._get("SUBNET") or None

    @property
    def mac(self) -> str | None:
        return self._get("MACADDRESS")

    @property
    def power(self) -> bool:
        return self._get("POWER") == "true"

    def set_ip(self, ip: str | None, mask: str | None) -> None:
        if ip:
            ipaddress.IPv4Interface(f"{ip}/{mask}")
        _set_child_text(self.el, "IP", ip or "", self.doc)
        _set_child_text(self.el, "SUBNET", mask or "", self.doc)

    def set_power(self, on: bool) -> None:
        _set_child_text(self.el, "POWER", "true" if on else "false", self.doc)

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name, "type": self.type,
                "ip": self.ip, "subnet": self.subnet, "mac": self.mac, "power": self.power}


class Device:
    def __init__(self, el, doc: XmlDoc, index: int):
        self.el = el
        self.doc = doc
        self.index = index
        self.engine = el.find("ENGINE")

    def _get(self, path: str) -> str | None:
        return self.doc.from_tree(self.engine.findtext(path))

    # identity
    @property
    def name(self) -> str:
        return self._get("NAME") or ""

    @name.setter
    def name(self, value: str) -> None:
        """Rename the device. Packet Tracer pairs each device with its
        physical-workspace node *by name* (verified on all 1,493 devices in
        the PT 8.2 samples), so that node is renamed too."""
        old = self.name
        if value == old:
            return
        if not value.strip():
            raise ValueError("device name must not be empty")
        siblings = self.el.getparent().iterfind("DEVICE/ENGINE/NAME")
        if any(self.doc.from_tree(n.text) == value for n in siblings):
            raise ValueError(f"another device is already named {value!r}")
        nodes = self.physical_nodes()
        _set_child_text(self.engine, "NAME", value, self.doc)
        if len(nodes) == 1:
            nodes[0].find("NAME").text = self.doc.to_tree(value)

    def physical_nodes(self) -> list:
        """Physical-workspace device nodes (TYPE 6) carrying this device's name."""
        net_root = self.el.getparent().getparent().getparent()  # DEVICES/NETWORK/PT5
        name = self.name
        return [n for n in net_root.iterfind("PHYSICALWORKSPACE//NODE")
                if n.findtext("TYPE") == "6"
                and self.doc.from_tree(n.findtext("NAME")) == name]

    @property
    def type(self) -> str:
        return self._get("TYPE") or ""

    @property
    def model(self) -> str:
        t = self.engine.find("TYPE")
        return t.get("model", "") if t is not None else ""

    @property
    def save_ref_id(self) -> str | None:
        return self._get("SAVE_REF_ID")

    @property
    def hostname(self) -> str | None:
        return self._get("SYS_NAME")

    @property
    def is_ios(self) -> bool:
        return self.engine.find("RUNNINGCONFIG") is not None

    # configuration
    def _lines(self, tag: str) -> list[str]:
        return [self.doc.from_tree(l.text) or "" for l in self.engine.iterfind(f"{tag}/LINE")]

    @property
    def running_config(self) -> list[str]:
        return self._lines("RUNNINGCONFIG")

    @property
    def startup_config(self) -> list[str]:
        return self._lines("STARTUPCONFIG")

    def set_running_config(self, lines: list[str], *, sync: bool = True) -> None:
        cont = self.engine.find("RUNNINGCONFIG")
        if cont is None:
            raise ValueError(f"{self.name}: device has no IOS running-config")
        _replace_lines(cont, lines, self.doc)
        if sync:
            self.sync_from_config()

    def set_startup_config(self, lines: list[str]) -> None:
        cont = self.engine.find("STARTUPCONFIG")
        if cont is None:
            raise ValueError(f"{self.name}: device has no startup-config")
        _replace_lines(cont, lines, self.doc)

    def write_memory(self) -> None:
        """copy running-config startup-config"""
        self.set_startup_config(self.running_config)

    def edit_config(self) -> "ConfigEditor":
        """Context manager: edit an IosConfig and write it back + sync."""
        return ConfigEditor(self)

    # ports
    def _port_elements(self) -> list:
        # Only interface ports: direct <PORT> children of a <MODULE>. Other
        # elements named PORT exist (e.g. inside service configs).
        return [p for p in self.engine.iter("PORT")
                if p.getparent() is not None and p.getparent().tag == "MODULE"]

    def ports(self) -> list[Port]:
        """Physical ports in document order, with names when resolvable.

        Port elements carry no name. For IOS devices the running-config
        lists the physical interfaces of each family (FastEthernet, Serial,
        ...) in the same order as the corresponding <PORT> elements, so
        names are assigned per family wherever the counts agree.
        """
        ports = [Port(p, self.doc, i) for i, p in enumerate(self._port_elements())]
        if self.is_ios:
            by_family: dict[str, list[str]] = {}
            for n in IosConfig(self.running_config).interface_names():
                if not is_virtual_interface(n):
                    by_family.setdefault(interface_family(n), []).append(n)
            port_groups: dict[str, list[Port]] = {}
            for p in ports:
                port_groups.setdefault(port_family(p.type or ""), []).append(p)
            for fam, group in port_groups.items():
                names = by_family.get(fam, [])
                if fam and len(names) == len(group):
                    for p, n in zip(group, names):
                        p.name = n
        else:
            # End devices name wired NICs <Family><n> (e.g. FastEthernet0).
            counts: dict[str, int] = {}
            for p in ports:
                fam = port_family(p.type or "")
                if fam in ("FastEthernet", "GigabitEthernet", "Ethernet"):
                    p.name = f"{fam}{counts.get(fam, 0)}"
                    counts[fam] = counts.get(fam, 0) + 1
        return ports

    def port(self, key: str | int) -> Port:
        ports = self.ports()
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            return ports[int(key)]
        for p in ports:
            if p.name and p.name.lower() == key.lower():
                return p
        raise KeyError(f"{self.name}: no port named {key!r}")

    # synchronisation between config text and structured XML state
    def sync_from_config(self) -> list[str]:
        """Propagate hostname and interface IPv4/shutdown state from the
        running-config into SYS_NAME and the <PORT> elements. Returns a
        list of human-readable changes."""
        changes = []
        cfg = IosConfig(self.running_config)
        host = cfg.hostname()
        # Older files have no SYS_NAME; PT derives it from the config then.
        if host and self.hostname is not None and host != self.hostname:
            _set_child_text(self.engine, "SYS_NAME", host, self.doc, create=False)
            changes.append(f"SYS_NAME -> {host}")
        for p in self.ports():
            if not p.name:
                continue
            st = cfg.interface(p.name)
            if st is None:
                continue
            if (st.ip or None, st.mask or None) != (p.ip, p.subnet):
                p.set_ip(st.ip, st.mask)
                changes.append(f"{p.name} IP -> {st.ip or '-'} {st.mask or ''}".rstrip())
            if p.power == st.shutdown:
                p.set_power(not st.shutdown)
                changes.append(f"{p.name} POWER -> {'false' if st.shutdown else 'true'}")
        return changes

    def check(self) -> list[str]:
        """Report mismatches between config text and structured state."""
        issues = []
        if not self.is_ios:
            return issues
        cfg = IosConfig(self.running_config)
        host = cfg.hostname()
        if host and self.hostname is not None and host != self.hostname:
            issues.append(f"hostname {host!r} != SYS_NAME {self.hostname!r}")
        for p in self.ports():
            st = cfg.interface(p.name) if p.name else None
            if st is None:
                continue
            if (st.ip, st.mask) != (p.ip, p.subnet):
                issues.append(f"{p.name}: config IP {st.ip}/{st.mask} != port {p.ip}/{p.subnet}")
            if p.power == st.shutdown:
                issues.append(f"{p.name}: shutdown={st.shutdown} but POWER={p.power}")
        return issues

    # endpoints (PC, Server, Laptop...) have no CLI config
    def set_endpoint_ip(self, port: str | int, ip: str, mask: str,
                        gateway: str | None = None) -> None:
        p = self.port(port)
        p.set_ip(ip, mask)
        _set_child_text(p.el, "PORT_DHCP_ENABLE", "false", self.doc, create=False)
        if gateway is not None:
            ipaddress.IPv4Address(gateway)
            _set_child_text(self.engine, "GATEWAY", gateway, self.doc)

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name, "type": self.type,
                "model": self.model, "hostname": self.hostname,
                "save_ref_id": self.save_ref_id,
                "ports": [p.to_dict() for p in self.ports()]}

    def __repr__(self) -> str:
        return f"<Device {self.name!r} {self.type}/{self.model}>"


class ConfigEditor:
    def __init__(self, device: Device):
        self.device = device
        self.config: IosConfig | None = None
        self.changes: list[str] = []

    def __enter__(self) -> IosConfig:
        self.config = IosConfig(self.device.running_config)
        return self.config

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.device.set_running_config(self.config.lines, sync=False)
            self.changes = self.device.sync_from_config()


@dataclass
class Link:
    el: object
    doc: XmlDoc = field(repr=False)

    def _cable(self, tag):
        return self.doc.from_tree(self.el.findtext(f"CABLE/{tag}"))

    @property
    def type(self) -> str | None:
        return self.doc.from_tree(self.el.findtext("TYPE"))

    @property
    def endpoints(self) -> tuple[tuple[str, str], tuple[str, str]]:
        cable = self.el.find("CABLE")
        ports = [self.doc.from_tree(p.text) for p in cable.findall("PORT")]
        return ((self._cable("FROM"), ports[0] if ports else None),
                (self._cable("TO"), ports[1] if len(ports) > 1 else None))


class Network:
    def __init__(self, el, doc: XmlDoc, role: str):
        self.el = el
        self.doc = doc
        self.role = role

    @property
    def version(self) -> str | None:
        return self.el.findtext("VERSION")

    def devices(self) -> list[Device]:
        elements = list(self.el.iterfind("NETWORK/DEVICES/DEVICE"))
        if not elements:
            # Reference wrappers may omit NETWORK/DEVICES or add extra layers.
            elements = [el for el in self.el.iter("DEVICE") if el.find("ENGINE") is not None]
        return [Device(d, self.doc, i)
                for i, d in enumerate(elements)]

    def device(self, name: str) -> Device:
        for d in self.devices():
            if d.name == name or d.hostname == name:
                return d
        names = ", ".join(d.name for d in self.devices())
        raise KeyError(f"no device {name!r} in {self.role} network (have: {names})")

    def links(self) -> list[Link]:
        return [Link(l, self.doc) for l in self.el.iterfind("NETWORK/LINKS/LINK")]

    def resolve_ref(self, ref: str) -> Device | None:
        """Map a link FROM/TO reference (SAVE_REF_ID or legacy index)."""
        devs = self.devices()
        if ref and ref.isdigit():
            i = int(ref)
            return devs[i] if i < len(devs) else None
        return next((d for d in devs if d.save_ref_id == ref), None)

    def check(self) -> list[str]:
        issues = []
        for d in self.devices():
            issues += [f"{d.name}: {m}" for m in d.check()]
            if self.el.find("PHYSICALWORKSPACE") is not None:
                n = len(d.physical_nodes())
                if n != 1:
                    issues.append(f"{d.name}: {n} physical-workspace nodes carry this name "
                                  "(expected exactly 1)")
        for i, link in enumerate(self.links()):
            for ref, port in link.endpoints:
                dev = self.resolve_ref(ref)
                if dev is None:
                    issues.append(f"link {i}: endpoint {ref!r} matches no device")
                elif dev.is_ios and port:
                    # Only judge families whose ports were all named.
                    groups: dict[str, list] = {}
                    for p in dev.ports():
                        groups.setdefault(port_family(p.type or ""), []).append(p.name)
                    names = groups.get(interface_family(port))
                    if names and all(names) and port not in names:
                        issues.append(f"link {i}: {dev.name} has no port {port!r}")
        return issues


# --- activity (read-only) ----------------------------------------------------

HIDDEN_NAME = "(name hidden by author)"
_QT_CHECKED = "2"  # COMPARISONS NAME@checkType is a Qt::CheckState

# Some tree node *names* embed configuration values (e.g. routing entries are
# named after their network/prefix). The checklist promises "what is checked,
# never the value", so such names are replaced by a neutral numbered label.
_VALUE_IN_NAME_RE = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b"                   # IPv4 address / mask / wildcard
    r"|(?<![\w:])[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){2,}(?:/\d{1,3})?"  # IPv6
)


def _redact_segment(name: str, counters: dict, parent: tuple) -> str:
    if not _VALUE_IN_NAME_RE.search(name):
        return name
    counters[parent] = counters.get(parent, 0) + 1
    return f"(entry {counters[parent]}: value hidden)"


@dataclass
class AssessmentItem:
    """One leaf of the grading tree: *what* is checked, never its value."""

    parts: tuple[str, ...]  # e.g. ("Network", "CENTRAL", "Ports", "Serial0/0/0", "IP Address")
    points: str | None
    components: str | None
    graded: bool = True

    @property
    def path(self) -> str:
        return " > ".join(self.parts)


class Activity:
    """Read-only view of a .pka activity's instructions and grading tree.

    :meth:`checklist` lists the items the author ticked for grading, which is
    what Packet Tracer shows students under Check Results > Assessment Items:
    item names, points and component, honouring the author's per-item "hide
    name" flag. Expected values (``NAME@nodeValue``) and the answer network
    are never read. For an author-locked activity, :meth:`assessment_items`
    is limited to the same graded checklist.
    """

    def __init__(self, root, doc: XmlDoc, protected: bool = False):
        self.root = root
        self.doc = doc
        self.protected = protected

    def instructions(self) -> list[str]:
        return [self.doc.from_tree(p.text) or ""
                for p in self.root.iterfind("ACTIVITY/INSTRUCTIONS/PAGE")]

    def checklist(self) -> list[AssessmentItem]:
        """Graded items only, in tree order."""
        return [i for i in self._items() if i.graded]

    def assessment_items(self) -> list[AssessmentItem]:
        """Every leaf of the tree (graded or not); only graded ones when locked."""
        return self.checklist() if self.protected else self._items()

    def _items(self) -> list[AssessmentItem]:
        out = []
        counters: dict = {}

        def walk(node, parts):
            name_el = node.find("NAME")
            name = self.doc.from_tree(name_el.text if name_el is not None else None) or "?"
            if name_el is not None and name_el.get("obfuscateName") == "true":
                name = HIDDEN_NAME
            parts = parts + (_redact_segment(name, counters, parts),)
            kids = node.findall("NODE")
            if not kids:
                graded = name_el is not None and name_el.get("checkType") == _QT_CHECKED
                out.append(AssessmentItem(parts, node.findtext("POINTS") or None,
                                          node.findtext("COMPONENTS") or None, graded))
            for k in kids:
                walk(k, parts)

        for top in self.root.iterfind("COMPARISONS/NODE"):
            walk(top, ())
        return out

    def summary(self) -> dict:
        items = self.checklist()
        pts = sum(int(i.points) for i in items if i.points and i.points.isdigit())
        return {"instruction_pages": len(self.instructions()),
                "graded_items": len(items), "total_points": pts,
                "locked_by_author": self.protected}


# --- top level ---------------------------------------------------------------

class PTFile:
    def __init__(self, decoded: codec.Decoded, doc: XmlDoc, path: Path | None = None):
        self.decoded = decoded
        self.doc = doc
        self.path = path

    @classmethod
    def from_bytes(cls, data: bytes, path: Path | None = None) -> "PTFile":
        decoded = codec.decode(data)
        return cls(decoded, XmlDoc.parse(decoded.xml), path)

    @classmethod
    def load(cls, path: str | Path) -> "PTFile":
        path = Path(path)
        return cls.from_bytes(path.read_bytes(), path)

    @classmethod
    def from_xml(cls, xml: bytes, fmt: codec.Format = codec.Format.MODERN) -> "PTFile":
        return cls(codec.Decoded(xml=xml, format=fmt), XmlDoc.parse(xml))

    @property
    def root(self):
        return self.doc.root

    @property
    def is_activity(self) -> bool:
        return self.root.tag in ACTIVITY_ROOTS

    @property
    def is_protected(self) -> bool:
        """True for an activity its author locked with an Activity Wizard
        password. The configuration editor uses the user's working network;
        XML export and recursive inspection still include the full document."""
        act = self.root.find("ACTIVITY") if self.is_activity else None
        return bool(act is not None and act.get("PASS"))

    @property
    def version(self) -> str | None:
        return self.root.findtext("VERSION")

    def networks(self) -> list[Network]:
        if not self.is_activity:
            return [Network(self.root, self.doc, "network")]
        nets = [c for c in self.root if c.tag in NETWORK_TAGS]
        if not nets:
            # Some activity variants name their states explicitly, with or
            # without an embedded PACKETTRACER document.
            named = {"USER_NETWORK": "user", "INITIAL_NETWORK": "initial",
                     "ANSWER_NETWORK": "answer", "REFERENCE_NETWORK": "reference"}
            states = [el for el in self.root.iter() if el.tag in named]
            if states:
                out = [Network(el, self.doc, named[el.tag]) for el in states]
                if self.is_protected:
                    return [n for n in out if n.role == "user"]
                return out
            return [Network(self.root, self.doc, "network")]
        roles = PKA_NETWORK_ROLES if len(nets) == 3 else [f"net{i}" for i in range(len(nets))]
        out = [Network(n, self.doc, r) for n, r in zip(nets, roles)]
        return out[:1] if self.is_protected else out

    def network(self, role: str | None = None) -> Network:
        nets = self.networks()
        if role is None:
            return nets[0]
        for n in nets:
            if n.role == role:
                return n
        if self.is_protected and role in PKA_NETWORK_ROLES[1:]:
            raise ProtectedActivityError(f"the {role!r} network is unavailable: {LOCKED_MESSAGE}")
        raise KeyError(f"no {role!r} network (have: {[n.role for n in nets]})")

    def extract_topologies(self) -> dict:
        """Read all embedded states, including variant-specific XML wrappers."""
        return self.doc.extract_topologies()

    def notes(self) -> list[str]:
        """Free text embedded in the (first) network: the network
        description and physical-workspace environment notes, de-duplicated."""
        el = self.networks()[0].el
        texts = [el.findtext("NETWORK/DESCRIPTION")]
        texts += [e.text for e in el.iterfind("PHYSICALWORKSPACE//ENVIRONMENT/NOTES")]
        out: list[str] = []
        for t in texts:
            t = self.doc.from_tree(t)
            if t and t.strip() and t not in out:
                out.append(t)
        return out

    @property
    def activity(self) -> Activity | None:
        return Activity(self.root, self.doc, self.is_protected) if self.is_activity else None

    def to_xml(self) -> bytes:
        return self.doc.serialize()

    def to_bytes(self) -> bytes:
        return codec.encode(self.to_xml(), self.decoded.format, self.decoded.zlib_params)

    def save(self, path: str | Path | None = None, *, verify: bool = True) -> Path:
        """Encode and write. With ``verify`` the output is decoded and
        re-parsed before the target is replaced."""
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no output path")
        xml = self.to_xml()
        if verify:
            XmlDoc.parse(xml)  # well-formed and losslessly re-parseable
        codec.encode_file(target, xml, self.decoded.format, self.decoded.zlib_params,
                          verify=verify)
        return target
