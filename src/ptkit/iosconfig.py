"""Text-level editing of IOS-style configuration line lists.

Packet Tracer stores a device's running/startup configuration as one
``<LINE>`` per config line and replays it through its CLI when the file is
loaded, so these lists are the authoritative IOS state. This module edits
them the way ``configure terminal`` would for the common cases (hostname,
interface addressing, shutdown, adding/removing block sub-commands). It is a
structural merge, not an IOS emulator: commands are not validated.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

# Interface name prefixes that do not correspond to a physical <PORT>.
VIRTUAL_IFACE_PREFIXES = (
    "vlan", "loopback", "tunnel", "port-channel", "virtual-", "dialer",
    "nvi", "bvi", "multilink", "null",
)


def is_virtual_interface(name: str) -> bool:
    n = name.lower()
    return "." in n or n.startswith(VIRTUAL_IFACE_PREFIXES)


def _is_sub(line: str) -> bool:
    return line.startswith(" ")


@dataclass
class InterfaceState:
    name: str
    ip: str | None = None
    mask: str | None = None
    shutdown: bool = False
    description: str | None = None


class IosConfig:
    """Mutable view over a list of IOS config lines."""

    def __init__(self, lines: list[str]):
        self.lines = list(lines)

    # -- queries --------------------------------------------------------
    def find_block(self, header: str) -> tuple[int, int] | None:
        """Return (start, end) of the block whose header line equals
        ``header`` (case-insensitive, whitespace-normalised); ``end`` is
        exclusive and covers the indented sub-commands."""
        want = " ".join(header.split()).lower()
        for i, line in enumerate(self.lines):
            if not _is_sub(line) and " ".join(line.split()).lower() == want:
                j = i + 1
                while j < len(self.lines) and _is_sub(self.lines[j]):
                    j += 1
                return i, j
        return None

    def interface_names(self) -> list[str]:
        return [l.split(None, 1)[1].strip() for l in self.lines
                if l.lower().startswith("interface ") and len(l.split()) > 1]

    def interface(self, name: str) -> InterfaceState | None:
        span = self.find_block(f"interface {name}")
        if span is None:
            return None
        st = InterfaceState(name=name)
        for line in self.lines[span[0] + 1 : span[1]]:
            parts = line.split()
            if parts[:2] == ["ip", "address"] and len(parts) >= 4 and parts[2] not in ("dhcp",):
                if len(parts) == 4:  # ignore "secondary"
                    st.ip, st.mask = parts[2], parts[3]
            elif parts == ["shutdown"]:
                st.shutdown = True
            elif parts[:1] == ["description"]:
                st.description = line.strip()[len("description "):]
        return st

    def hostname(self) -> str | None:
        for line in self.lines:
            if line.startswith("hostname "):
                return line.split(None, 1)[1].strip()
        return None

    # -- edits ----------------------------------------------------------
    def _global_insert_point(self) -> int:
        """Index before which new top-level blocks go: before the line/end
        section, mirroring where IOS prints routing/ACL blocks."""
        for i, line in enumerate(self.lines):
            low = line.lower()
            if low.startswith(("line ", "end")) and not _is_sub(line):
                # back up over the '!' separators preceding the section
                while i > 0 and self.lines[i - 1].strip() == "!":
                    i -= 1
                return i
        return len(self.lines)

    def set_hostname(self, name: str) -> None:
        if not name or " " in name:
            raise ValueError("hostname must be a single non-empty word")
        for i, line in enumerate(self.lines):
            if line.startswith("hostname "):
                self.lines[i] = f"hostname {name}"
                return
        # IOS prints hostname right after the service lines
        idx = next((i for i, l in enumerate(self.lines)
                    if not l.startswith(("version", "service", "no service", "!"))),
                   len(self.lines))
        self.lines[idx:idx] = [f"hostname {name}", "!"]

    def ensure_block(self, header: str) -> tuple[int, int]:
        span = self.find_block(header)
        if span is not None:
            return span
        if header.lower().startswith("interface "):
            # keep interfaces together: after the last existing interface block
            last = None
            for i, line in enumerate(self.lines):
                if line.lower().startswith("interface "):
                    last = i
            if last is not None:
                j = last + 1
                while j < len(self.lines) and _is_sub(self.lines[j]):
                    j += 1
                self.lines[j:j] = ["!", header]
                return j + 1, j + 2
        idx = self._global_insert_point()
        self.lines[idx:idx] = ["!", header]
        return idx + 1, idx + 2

    def set_block_command(self, header: str, command: str) -> None:
        """Apply one sub-command inside ``header`` with IOS replace semantics:
        ``no X`` removes lines starting with ``X``; ``ip address`` and
        ``description`` replace their previous value; ``shutdown`` /
        ``no shutdown`` toggle; anything else is appended if absent."""
        command = command.strip()
        start, end = self.ensure_block(header)
        body = self.lines[start + 1 : end]
        words = command.split()
        if words[:1] == ["no"] and words[1:] != ["ip", "address"]:
            target = " ".join(words[1:])
            body = [l for l in body if not l.strip().startswith(target)]
            if words[1:] == ["shutdown"]:
                pass  # IOS shows nothing for an enabled interface
        elif words[:2] == ["ip", "address"] or words == ["no", "ip", "address"]:
            if words[2:3] != ["secondary"] and "secondary" not in words:
                body = [l for l in body
                        if not (l.strip().startswith("ip address") and "secondary" not in l)
                        and l.strip() != "no ip address"]
            # IOS lists the address first-ish; keep it near the top
            pos = next((k for k, l in enumerate(body)
                        if not l.strip().startswith(("description", "mac-address"))), len(body))
            body.insert(pos, " " + command)
        elif words[:1] == ["description"]:
            body = [l for l in body if not l.strip().startswith("description")]
            body.insert(0, " " + command)
        elif words == ["shutdown"]:
            if " shutdown" not in body:
                body.append(" shutdown")
        elif " " + command not in body:
            body.append(" " + command)
        self.lines[start + 1 : end] = body

    def set_interface_ip(self, name: str, ip: str, mask: str) -> None:
        ipaddress.IPv4Interface(f"{ip}/{mask}")  # validates both
        self.set_block_command(f"interface {name}", f"ip address {ip} {mask}")

    def set_interface_shutdown(self, name: str, shutdown: bool) -> None:
        self.set_block_command(f"interface {name}", "shutdown" if shutdown else "no shutdown")

    def merge(self, snippet: str) -> None:
        """Merge a config snippet written as it would be typed in
        ``configure terminal`` (indented lines belong to the preceding
        block header)."""
        header = None
        for raw in snippet.splitlines():
            if not raw.strip() or raw.strip() in ("!", "end", "exit"):
                if raw.strip() == "exit":
                    header = None
                continue
            if raw[:1].isspace():
                if header is None:
                    raise ValueError(f"indented line without a block header: {raw!r}")
                self.set_block_command(header, raw)
                continue
            line = raw.rstrip()
            words = line.split()
            header = None
            if words[0] == "hostname" and len(words) == 2:
                self.set_hostname(words[1])
            elif words[0] == "no":
                target = " ".join(words[1:])
                span = self.find_block(target)
                if span is not None:
                    a, b = span
                    if a > 0 and self.lines[a - 1].strip() == "!":
                        a -= 1
                    del self.lines[a:b]
                else:
                    self.lines = [l for l in self.lines
                                  if _is_sub(l) or not l.startswith(target)]
            elif _BLOCK_RE.match(line):
                header = line
                self.ensure_block(line)
            elif line not in self.lines:
                idx = self._global_insert_point()
                self.lines[idx:idx] = ["!", line] if idx and self.lines[idx - 1] != "!" else [line]


# Top-level commands that open a sub-command block.
_BLOCK_RE = re.compile(
    r"^(interface|router|line|ip dhcp pool|ip access-list|ipv6 dhcp pool|"
    r"ipv6 router|class-map|policy-map|crypto|vlan \d|key chain|ip sla|track|"
    r"zone|parameter-map|aaa group|radius server)\b",
    re.I,
)
