"""XML export folders and re-encoding (ptkit export / import)."""

import json

import pytest

from conftest import make_pka_xml
from ptkit import PTFile, codec, xmlbundle
from ptkit.cli import main
from ptkit.xmlbundle import BundleError


@pytest.fixture
def pkt(tmp_path, pkt_xml):
    p = tmp_path / "lab.pkt"
    p.write_bytes(codec.encode(pkt_xml))
    return p


def _pka(tmp_path, name="act.pka", **kw):
    p = tmp_path / name
    p.write_bytes(codec.encode(make_pka_xml(**kw)))
    return p


def _edit(path, old: bytes, new: bytes):
    data = path.read_bytes()
    assert data.count(old) >= 1, old
    path.write_bytes(data.replace(old, new, 1))


def test_pkt_roundtrip_is_byte_identical(tmp_path, pkt):
    res = xmlbundle.export_bundle(PTFile.load(pkt), tmp_path / "x")
    assert set(res.files) == {"document.xml", "manifest.json"}
    out = xmlbundle.write_container(xmlbundle.import_path(tmp_path / "x"), tmp_path / "re.pkt")
    assert out.read_bytes() == pkt.read_bytes()


def test_unlocked_pka_exports_every_network(tmp_path):
    src = _pka(tmp_path)
    res = xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    assert set(res.files) >= {"document.xml", "network-user.xml", "network-initial.xml",
                              "network-answer.xml"}
    assert b"ANSWER-KEY" in (tmp_path / "x" / "network-answer.xml").read_bytes()
    out = xmlbundle.write_container(xmlbundle.import_path(tmp_path / "x"), tmp_path / "re.pka")
    assert out.read_bytes() == src.read_bytes()


def test_edited_document_xml_is_used(tmp_path, pkt):
    xmlbundle.export_bundle(PTFile.load(pkt), tmp_path / "x")
    _edit(tmp_path / "x" / "document.xml", b"hostname Router<", b"hostname Edited<")
    _edit(tmp_path / "x" / "document.xml", b"<SYS_NAME>Router<", b"<SYS_NAME>Edited<")
    res = xmlbundle.import_path(tmp_path / "x")
    assert any("edited document.xml" in m for m in res.messages)
    out = xmlbundle.write_container(res, tmp_path / "re.pkt")
    assert PTFile.load(out).network().device("Router0").hostname == "Edited"


def test_edited_network_file_is_spliced(tmp_path):
    src = _pka(tmp_path)
    xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    _edit(tmp_path / "x" / "network-initial.xml", b"<SYS_NAME>Router<", b"<SYS_NAME>Init2<")
    res = xmlbundle.import_path(tmp_path / "x")
    out = PTFile.load(xmlbundle.write_container(res, tmp_path / "re.pka"))
    assert [n.device("Router0").hostname for n in out.networks()] == \
        ["Router", "Init2", "ANSWER-KEY"]


def test_editing_document_and_network_is_ambiguous(tmp_path):
    src = _pka(tmp_path)
    xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    _edit(tmp_path / "x" / "document.xml", b"<SYS_NAME>Router<", b"<SYS_NAME>A<")
    _edit(tmp_path / "x" / "network-user.xml", b"<SYS_NAME>Router<", b"<SYS_NAME>B<")
    with pytest.raises(BundleError, match="both document.xml and network-user.xml"):
        xmlbundle.import_path(tmp_path / "x")


def test_locked_export_includes_all_networks_and_grading(tmp_path):
    src = _pka(tmp_path, password="secret!")
    res = xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    assert res.locked and set(res.files) == {"document.xml", "network-user.xml",
                                           "network-initial.xml", "network-answer.xml",
                                           "manifest.json"}
    assert (tmp_path / "x" / "document.xml").read_bytes() == codec.decode_file(src).xml
    assert b"ANSWER-KEY" in (tmp_path / "x" / "network-answer.xml").read_bytes()
    assert b"SECRET" in (tmp_path / "x" / "document.xml").read_bytes()
    manifest = json.loads((tmp_path / "x" / "manifest.json").read_text())
    assert manifest["locked_by_author"] and manifest["networks"] == ["user", "initial", "answer"]


def test_locked_user_network_edit_spliced_hidden_parts_untouched(tmp_path):
    src = _pka(tmp_path, password="secret!")
    original = codec.decode_file(src).xml
    xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    _edit(tmp_path / "x" / "network-user.xml", b"<SYS_NAME>Router<", b"<SYS_NAME>Mine<")
    out = xmlbundle.write_container(xmlbundle.import_path(tmp_path / "x"), tmp_path / "re.pka")
    new = codec.decode_file(out).xml
    assert PTFile.load(out).network().device("Router0").hostname == "Mine"
    # everything except that one value is byte-identical
    assert new.replace(b"<SYS_NAME>Mine<", b"<SYS_NAME>Router<", 1) == original


def test_locked_import_requires_unchanged_source(tmp_path):
    src = _pka(tmp_path, password="secret!")
    xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    # Simulate a legacy user-only bundle: those still require the source.
    manifest_path = tmp_path / "x" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = {k: v for k, v in manifest["files"].items() if k == "network-user.xml"}
    manifest["networks"] = ["user"]
    manifest["source"] = str(src.resolve())  # old bundles recorded the absolute path
    manifest_path.write_text(json.dumps(manifest))
    src.write_bytes(src.read_bytes() + b"")  # same bytes: fine
    xmlbundle.import_path(tmp_path / "x")
    src.write_bytes(codec.encode(make_pka_xml(password="other")))
    with pytest.raises(BundleError, match="changed since the export"):
        xmlbundle.import_path(tmp_path / "x")


def test_locked_can_splice_answer_but_fragments_must_be_valid(tmp_path):
    src = _pka(tmp_path, password="secret!")
    pt = PTFile.load(src)
    frag = (tmp_path / "f.xml")
    xmlbundle.export_bundle(pt, tmp_path / "x")
    frag.write_bytes((tmp_path / "x" / "network-user.xml").read_bytes())
    xml = xmlbundle.splice_network(pt, "answer", frag.read_bytes())
    assert b"ANSWER-KEY" not in xml
    # a fragment can't break out of its element: it must be one well-formed network
    with pytest.raises(BundleError, match="invalid XML"):
        xmlbundle.splice_network(pt, "user", b"</PACKETTRACER5><COMPARISONS/><PACKETTRACER5>")
    with pytest.raises(BundleError, match="root element is <COMPARISONS>"):
        xmlbundle.splice_network(pt, "user", b"<COMPARISONS/>")


def test_single_full_locked_document_can_be_imported(tmp_path):
    f = tmp_path / "doc.xml"
    f.write_bytes(make_pka_xml(password="secret!"))
    assert xmlbundle.import_path(f).xml == f.read_bytes()


def test_locked_complete_bundle_edits_without_original_source(tmp_path):
    src = _pka(tmp_path, password="secret!")
    original = codec.decode_file(src).xml
    xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    src.unlink()
    _edit(tmp_path / "x" / "network-answer.xml", b"ANSWER-KEY", b"UpdatedAnswer")
    result = xmlbundle.import_path(tmp_path / "x")
    assert result.xml.replace(b"UpdatedAnswer", b"ANSWER-KEY", 1) == original
    out = xmlbundle.write_container(result, tmp_path / "out.pka")
    assert codec.decode_file(out).xml == result.xml


def test_locked_grading_data_edit_roundtrips(tmp_path):
    src = _pka(tmp_path, password="secret!")
    original = codec.decode_file(src).xml
    xmlbundle.export_bundle(PTFile.load(src), tmp_path / "x")
    _edit(tmp_path / "x" / "document.xml", b"SECRET", b"NEW_EXPECTED_VALUE")
    result = xmlbundle.import_path(tmp_path / "x")
    assert result.xml.replace(b"NEW_EXPECTED_VALUE", b"SECRET", 1) == original
    assert codec.decode_file(xmlbundle.write_container(result, tmp_path / "out.pka")).xml == result.xml


def test_single_file_import_and_format_guess(tmp_path, pkt_xml):
    f = tmp_path / "net.xml"
    f.write_bytes(b"\xef\xbb\xbf<?xml version='1.0'?>\n" + pkt_xml)  # as an editor saves it
    res = xmlbundle.import_path(f)
    assert res.xml == pkt_xml  # BOM + declaration removed, trailing newline kept
    assert res.suffix == ".pkt" and res.fmt is codec.Format.MODERN
    assert xmlbundle.guess_format(make_pka_xml(pt4=True)) is codec.Format.LEGACY


@pytest.mark.parametrize("bad,match", [
    (b"<PACKETTRACER5><NETWORK></PACKETTRACER5>", "invalid XML"),
    (b"<html><body/></html>", "not a Packet Tracer document"),
])
def test_validation_errors_are_reported(tmp_path, bad, match):
    f = tmp_path / "bad.xml"
    f.write_bytes(bad)
    with pytest.raises(BundleError, match=match):
        xmlbundle.import_path(f)


def test_folder_without_manifest(tmp_path):
    with pytest.raises(BundleError, match="no manifest.json"):
        xmlbundle.import_path(tmp_path)


def test_cli_export_import(tmp_path, pkt, capsys):
    assert main(["export", str(pkt), "-o", str(tmp_path / "x")]) == 0
    assert main(["import", str(tmp_path / "x"), "-o", str(tmp_path / "re")]) == 0
    assert (tmp_path / "re.pkt").read_bytes() == pkt.read_bytes()
    assert "verified" in capsys.readouterr().out
    (tmp_path / "bad.xml").write_bytes(b"<nope")
    assert main(["import", str(tmp_path / "bad.xml"), "-o", str(tmp_path / "z.pkt")]) == 2
    assert "invalid XML" in capsys.readouterr().err
