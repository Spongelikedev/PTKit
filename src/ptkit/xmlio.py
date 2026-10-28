"""Source-preserving XML parse/serialize for Packet Tracer documents.

Packet Tracer's Qt serializer has quirks no generic XML writer reproduces:
``>`` is escaped in some files and raw in others, some start tags carry a
space before ``>``, ``<X></X>`` and ``<X/>`` are distinct, C0 control
characters forbidden by XML 1.0 appear in text, and CDATA is used for script
data. A naive lxml round-trip therefore changes thousands of bytes.

:class:`XmlDoc` parses with lxml (so callers get the normal ElementTree API)
but remembers the original bytes of every start tag, end tag, text run and
tail run. On :meth:`XmlDoc.serialize` each piece whose parsed value is still
unchanged is emitted verbatim; only edited values and newly created elements
are re-encoded. An unmodified document therefore serialises to exactly the
input bytes, and :meth:`XmlDoc.parse` verifies that before returning.

Control characters are mapped into a Private Use Area block that the
document does not otherwise use (see :meth:`XmlDoc.to_tree` /
:meth:`XmlDoc.from_tree` for values crossing the API boundary). That applies
both to raw C0 bytes and to character references that XML 1.0 forbids (PT
4.x writes e.g. ``&#x3;``): the references are rewritten only in the copy
lxml parses, so the preserved raw spans still reproduce them exactly.
Old PT also NUL-terminates the document; trailing NULs/whitespace after the
root element are kept verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

_CTRL = bytes(c for c in range(0x20) if c not in (0x09, 0x0A))
_CTRL_RE = re.compile(b"[" + re.escape(_CTRL) + b"]")
_CTRL_STR_RE = re.compile("[\x00-\x08\x0b-\x1f]")
# Character references to code points XML 1.0 forbids (all C0 except TAB,
# LF, CR). CR (&#13;) is legal and left alone.
_CHARREF_RE = re.compile(rb"&#(?:[xX]([0-9A-Fa-f]{1,6})|([0-9]{1,7}));")
_FORBIDDEN_REFS = {c for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}
# Markup tokens in document order. CDATA is matched only so that '<' inside
# it is skipped; it is part of the surrounding text run (lxml merges CDATA
# into .text/.tail). Attribute values may legally contain '>'.
_TOKEN_RE = re.compile(
    rb"(?P<cdata><!\[CDATA\[.*?\]\]>)"
    rb"|(?P<comment><!--.*?-->)"
    rb"|(?P<pi><\?.*?\?>)"
    rb"|(?P<end></[^>]*>)"
    rb"|(?P<start><[A-Za-z_](?:[^>\"']|\"[^\"]*\"|'[^']*')*>)",
    re.S,
)

_PARSER = etree.XMLParser(
    huge_tree=True,
    remove_blank_text=False,
    resolve_entities=False,
    no_network=True,
    strip_cdata=False,
)


class XmlFidelityError(Exception):
    """The document cannot be represented losslessly."""


@dataclass(slots=True)
class _Src:
    tag: str
    attrs: tuple
    text: str | None
    tail: str | None
    raw_start: bytes
    raw_text: bytes
    raw_end: bytes  # b"" for self-closing elements
    raw_tail: bytes


def _pick_pua_base(text: str) -> int:
    for base in range(0xE000, 0xF900, 0x20):
        if not re.search(f"[{chr(base)}-{chr(base + 0x1F)}]", text):
            return base
    raise XmlFidelityError("no free Private Use Area block for control characters")


def _ref_code(m: re.Match) -> int:
    """Code point of a _CHARREF_RE match (hex or decimal form)."""
    return int(m.group(1), 16) if m.group(1) else int(m.group(2))


def _unprotect(data: bytes, base: int) -> bytes:
    """Map PUA stand-ins in ``data`` back to the control bytes they replaced."""
    if not data:
        return data
    pat = f"[{chr(base)}-{chr(base + 0x1F)}]"
    return re.sub(pat, lambda m: chr(ord(m.group()) - base),
                  data.decode("utf-8")).encode("utf-8")


def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    return (_esc_text(s).replace('"', "&quot;")
            .replace("\n", "&#10;").replace("\t", "&#9;"))


class XmlDoc:
    def __init__(self, root, prolog: bytes, epilog: bytes, pua_base: int, src: dict,
                 ctrl_as_refs: bool = False):
        self.root = root
        self.tree = etree.ElementTree(root)
        self._prolog = prolog
        self._epilog = epilog
        self._pua = pua_base
        self._ctrl_back = re.compile(f"[{chr(pua_base)}-{chr(pua_base + 0x1F)}]")
        # How *new* text encodes control characters, matching the document:
        # PT 4.x writes "&#x3;" and its reader rejects raw bytes; newer PT
        # writes raw bytes. CR always stays raw (it is legal XML).
        self._ctrl_as_refs = ctrl_as_refs
        forbidden = "".join(chr(pua_base + c) for c in sorted(_FORBIDDEN_REFS))
        self._ctrl_fwd = re.compile(f"[{re.escape(forbidden)}]")
        # Keyed by element proxy. Holding the proxies keeps them alive, so
        # lxml hands back the same Python object on later traversals.
        self._src = src

    # -- parsing ----------------------------------------------------------
    @classmethod
    def parse(cls, xml: bytes, *, verify: bool = True) -> "XmlDoc":
        if b"<!DOCTYPE" in xml:
            raise XmlFidelityError("DOCTYPE declarations are not supported")
        try:
            text = xml.decode("utf-8")
        except UnicodeDecodeError as e:
            raise XmlFidelityError(f"document is not valid UTF-8: {e}") from e
        base = _pick_pua_base(text)
        buf = _CTRL_RE.sub(lambda m: chr(base + m.group()[0]).encode("utf-8"), xml)

        toks = [m for m in _TOKEN_RE.finditer(buf) if m.lastgroup != "cdata"]
        root_i = next((i for i, m in enumerate(toks) if m.lastgroup == "start"), None)
        if root_i is None:
            raise XmlFidelityError("no root element")

        depth, root_last = 0, None
        for i in range(root_i, len(toks)):
            m = toks[i]
            if m.lastgroup == "start":
                if not m.group().endswith(b"/>"):
                    depth += 1
                elif depth == 0:
                    root_last = i
                    break
            elif m.lastgroup == "end":
                depth -= 1
                if depth == 0:
                    root_last = i
                    break
        if root_last is None:
            raise XmlFidelityError("root element is not closed")
        r0, r1 = toks[root_i].start(), toks[root_last].end()
        prolog, epilog = buf[:r0], buf[r1:]
        if prolog.strip() and not prolog.lstrip().startswith(b"<?xml"):
            raise XmlFidelityError("unexpected content before root element")
        if _unprotect(epilog, base).strip(b" \t\r\n\x00"):
            raise XmlFidelityError("unexpected content after root element")

        def fix_ref(m: re.Match) -> bytes:
            code = _ref_code(m)
            return chr(base + code).encode("utf-8") if code in _FORBIDDEN_REFS else m.group()

        # lxml gets a copy with forbidden references mapped like raw control
        # bytes; raw spans (for serialising) keep coming from ``buf``.
        try:
            root = etree.fromstring(_CHARREF_RE.sub(fix_ref, buf[r0:r1]), _PARSER)
        except etree.XMLSyntaxError as e:
            raise XmlFidelityError(f"XML syntax error: {e}") from e

        def run_after(i: int) -> bytes:
            if i >= root_last:
                return b""
            return buf[toks[i].end():toks[i + 1].start()]

        src: dict = {}
        pos = root_i

        def visit(node) -> None:
            nonlocal pos
            tok = toks[pos]
            if isinstance(node.tag, str):
                if tok.lastgroup != "start":
                    raise XmlFidelityError(f"token mismatch at <{node.tag}>")
                start_i = pos
                pos += 1
                if tok.group().endswith(b"/>"):
                    raw_text, raw_end, last = b"", b"", start_i
                else:
                    raw_text = run_after(start_i)
                    for child in node:
                        visit(child)
                    if toks[pos].lastgroup != "end":
                        raise XmlFidelityError(f"token mismatch closing <{node.tag}>")
                    raw_end, last = toks[pos].group(), pos
                    pos += 1
                src[node] = _Src(node.tag, tuple(node.attrib.items()), node.text,
                                 node.tail, tok.group(), raw_text, raw_end,
                                 run_after(last))
            else:  # comment or processing instruction
                if tok.lastgroup not in ("comment", "pi"):
                    raise XmlFidelityError("token mismatch at comment/PI")
                src[node] = _Src("", (), node.text, node.tail, tok.group(), b"",
                                 b"", run_after(pos))
                pos += 1

        visit(root)
        if pos != root_last + 1:
            raise XmlFidelityError("token stream not fully consumed")
        body = xml.rstrip(b" \t\r\n\x00")
        n_refs = sum(1 for m in _CHARREF_RE.finditer(body) if _ref_code(m) in _FORBIDDEN_REFS)
        n_raw = sum(1 for b in body if b in _FORBIDDEN_REFS)
        doc = cls(root, prolog, epilog, base, src, ctrl_as_refs=n_refs > 0 and n_raw == 0)
        if verify and doc.serialize() != xml:
            raise XmlFidelityError("document does not round-trip unchanged")
        return doc

    # -- serialising --------------------------------------------------------
    def serialize(self) -> bytes:
        out: list[bytes] = [self._prolog]
        self._emit(self.root, out, is_root=True)
        out.append(self._epilog)
        data = b"".join(out).decode("utf-8")
        return self._ctrl_back.sub(lambda m: chr(ord(m.group()) - self._pua),
                                   data).encode("utf-8")

    def fragment(self, el) -> bytes:
        """Exact source bytes of one element (start tag to end tag, without its
        tail), with control characters restored: the same bytes it occupies
        in :meth:`serialize` output."""
        out: list[bytes] = []
        self._emit(el, out, is_root=True)
        data = b"".join(out).decode("utf-8")
        return self._ctrl_back.sub(lambda m: chr(ord(m.group()) - self._pua),
                                   data).encode("utf-8")

    def _emit(self, node, out: list, is_root: bool = False) -> None:
        s = self._src.get(node)
        if not isinstance(node.tag, str):
            if s is not None and node.text == s.text:
                out.append(s.raw_start)
            else:
                out.append(etree.tostring(node, with_tail=False))
        else:
            tag = node.tag.encode("utf-8")
            has_content = bool(node.text) or len(node) > 0
            attrs_same = (s is not None and s.tag == node.tag
                          and s.attrs == tuple(node.attrib.items()))
            orig_paired = s is not None and s.raw_end != b""
            if not orig_paired and not has_content:
                out.append(s.raw_start if attrs_same else self._build_start(node, True))
            else:
                if orig_paired:
                    start = s.raw_start if attrs_same else self._build_start(node, False)
                    end = s.raw_end if s.tag == node.tag else b"</" + tag + b">"
                else:
                    start = (s.raw_start[:-2].rstrip() + b">" if attrs_same
                             else self._build_start(node, False))
                    end = b"</" + tag + b">"
                out.append(start)
                if orig_paired and node.text == s.text:
                    out.append(s.raw_text)
                elif node.text:
                    out.append(self._enc(_esc_text(node.text)))
                for child in node:
                    self._emit(child, out)
                out.append(end)
        if is_root:
            return
        if s is not None and node.tail == s.tail:
            out.append(s.raw_tail)
        elif node.tail:
            out.append(self._enc(_esc_text(node.tail)))

    def _enc(self, escaped: str) -> bytes:
        """Encode already-escaped new text, writing control characters as
        character references when the document does so."""
        if self._ctrl_as_refs:
            escaped = self._ctrl_fwd.sub(lambda m: f"&#x{ord(m.group()) - self._pua:x};",
                                         escaped)
        return escaped.encode("utf-8")

    def _build_start(self, node, self_close: bool) -> bytes:
        parts = [node.tag] + [f'{k}="{_esc_attr(v)}"' for k, v in node.attrib.items()]
        return self._enc("<" + " ".join(parts) + ("/>" if self_close else ">"))

    # -- value mapping for callers -----------------------------------------

    def extract_topologies(self) -> dict:
        """Inspect all embedded network states as normalized dictionaries."""
        from .topology import extract_topologies
        return extract_topologies(self)
    def to_tree(self, s: str | None) -> str | None:
        """Map a user-supplied string into the tree's protected form."""
        if not s:
            return s
        return _CTRL_STR_RE.sub(lambda m: chr(self._pua + ord(m.group())), s)

    def from_tree(self, s: str | None) -> str | None:
        """Map a string read from the tree back to its real characters."""
        if not s:
            return s
        return self._ctrl_back.sub(lambda m: chr(ord(m.group()) - self._pua), s)
