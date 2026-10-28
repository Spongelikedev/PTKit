import pytest
from lxml import etree

from ptkit.xmlio import XmlDoc, XmlFidelityError


def test_unmodified_document_is_byte_identical(pkt_xml, pka_xml):
    for x in (pkt_xml, pka_xml):
        assert XmlDoc.parse(x).serialize() == x


@pytest.mark.parametrize("xml", [
    b"<A>\n <B></B>\n <C/>\n</A>\n",             # explicit-empty vs self-closing
    b'<A x="1" >t > u</A>',                      # space before '>', raw '>'
    b"<A>a &gt; b &amp; c</A>",                  # escaped '>'
    b"<A><B><![CDATA[x < y && z]]></B></A>",     # CDATA
    b"<A>bell\x07 and cr\r\n</A>",               # forbidden control chars, CR
    b'<?xml version="1.0"?>\n<A/>\n',            # declaration, self-closing root
    b"<A><!-- note --><B/></A>",                 # comment
    b"<A b='q\"uote'>x</A>",                     # single-quoted attribute
    "<A>café </A>".encode(),          # non-ASCII + a PUA char
])
def test_quirks_roundtrip(xml):
    assert XmlDoc.parse(xml).serialize() == xml


def test_control_characters_visible_through_api():
    doc = XmlDoc.parse(b"<A><H>a\x03b</H></A>")
    h = doc.root.find("H")
    assert doc.from_tree(h.text) == "a\x03b"
    h.text = doc.to_tree("x\x04y")
    assert doc.serialize() == b"<A><H>x\x04y</H></A>"


def test_edits_touch_only_changed_nodes():
    src = b'<A>\n <B k="v" >a > b</B>\n <C></C>\n <D/>\n</A>\n'
    doc = XmlDoc.parse(src)
    doc.root.find("D").text = "new & <x>"
    assert doc.serialize() == (
        b'<A>\n <B k="v" >a > b</B>\n <C></C>\n <D>new &amp; &lt;x&gt;</D>\n</A>\n')
    doc.root.find("B").set("k", 'w"')
    assert b'<B k="w&quot;">a > b</B>' in doc.serialize()


def test_new_and_removed_elements():
    doc = XmlDoc.parse(b"<A>\n <B/>\n</A>")
    b = doc.root.find("B")
    c = etree.SubElement(doc.root, "C")
    c.text = "1"
    b.tail, c.tail = "\n ", "\n"
    assert doc.serialize() == b"<A>\n <B/>\n <C>1</C>\n</A>"
    doc.root.remove(c)
    b.tail = "\n"
    assert doc.serialize() == b"<A>\n <B/>\n</A>"


def test_output_after_edit_reparses(pkt_xml):
    doc = XmlDoc.parse(pkt_xml)
    for el in doc.root.iter("NAME"):
        el.text = "renamed"
    again = XmlDoc.parse(doc.serialize())
    assert all(e.text == "renamed" for e in again.root.iter("NAME"))


@pytest.mark.parametrize("bad", [b"<A>", b"<A></B>", b"junk<A/>", b"<A/>junk",
                                 b"<!DOCTYPE A><A/>", b"\xff\xfe<A/>"])
def test_malformed_input_rejected(bad):
    with pytest.raises(XmlFidelityError):
        XmlDoc.parse(bad)
