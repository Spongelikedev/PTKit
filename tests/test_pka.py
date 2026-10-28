""".pka activities: PT 4.x documents and author-locked (password) activities."""

import pytest

from conftest import make_pka_xml
from ptkit import PTFile, codec, ops
from ptkit.cli import main
from ptkit.model import ProtectedActivityError
from ptkit.xmlio import XmlDoc


def test_pt4_quirks_roundtrip_byte_exact():
    xml = make_pka_xml(pt4=True)
    assert b"&#x3;" in xml and xml.endswith(b"\x00")
    doc = XmlDoc.parse(xml)
    assert doc.serialize() == xml
    note = next(doc.root.iter("NOTE"))
    assert doc.from_tree(note.text) == "a\x03b"  # the reference's meaning


@pytest.mark.parametrize("fmt", list(codec.Format))
def test_pt4_activity_loads_edits_and_saves(tmp_path, fmt):
    src = tmp_path / "old.pka"
    src.write_bytes(codec.encode(make_pka_xml(pt4=True), fmt))
    pt = PTFile.load(src)
    assert pt.is_activity and pt.version == "4.1"
    assert [n.role for n in pt.networks()] == ["user", "initial", "answer"]
    assert pt.to_bytes() == src.read_bytes()
    ops.apply_snippet(pt.network().device("Router0"), "hostname Edited")
    pt.save(tmp_path / "out.pka")
    again = PTFile.load(tmp_path / "out.pka")
    assert again.network().device("Router0").hostname == "Edited"
    assert again.to_xml().endswith(b"\x00") and b"&#x3;" in again.to_xml()


@pytest.fixture
def locked(tmp_path):
    p = tmp_path / "locked.pka"
    p.write_bytes(codec.encode(make_pka_xml(password="secret!")))
    return p


def test_locked_activity_exposes_only_user_network(locked):
    pt = PTFile.load(locked)
    assert pt.is_protected
    assert [n.role for n in pt.networks()] == ["user"]
    for role in ("initial", "answer"):
        with pytest.raises(ProtectedActivityError):
            pt.network(role)
    # locked: the graded checklist (what is checked) is available, as in PT's
    # Check Results; ungraded nodes are not, and values never are.
    assert pt.activity.assessment_items() == pt.activity.checklist()
    assert [i.parts[-1] for i in pt.activity.checklist()] == [
        "Host Name", "IP Address", "(name hidden by author)"]
    s = pt.activity.summary()
    assert s["locked_by_author"] and s["graded_items"] == 3


def test_checklist_never_carries_expected_values(locked, capsys):
    pt = PTFile.load(locked)
    blob = repr(pt.activity.checklist()) + repr(pt.activity.summary())
    assert "SECRET" not in blob and "Secret Item" not in blob
    for argv in (["activity", str(locked)], ["activity", str(locked), "--json", "--all"]):
        assert main(argv) == 0
        out = capsys.readouterr()
        assert "SECRET" not in out.out and "Secret Item" not in out.out
        assert "IP Address" in out.out and "expected values are not shown" in out.out


def test_cli_all_flag_only_for_unlocked(tmp_path, capsys):
    import json
    p = tmp_path / "open.pka"
    p.write_bytes(codec.encode(make_pka_xml()))
    assert main(["activity", str(p), "--json", "--all"]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    assert {i["item"].split(" > ")[-1]: i["graded"] for i in items}["Power"] is False
    assert items[0]["item"] == "Router0 > Host Name"
    assert not any("SECRET" in json.dumps(i) for i in items)


def test_unlocked_activity_unchanged(tmp_path):
    pt = PTFile.from_xml(make_pka_xml())
    assert not pt.is_protected
    assert pt.network("answer").device("Router0").hostname == "ANSWER-KEY"


def test_locked_edit_preserves_hidden_networks(locked, tmp_path):
    pt = PTFile.load(locked)
    before = pt.to_xml()
    ops.apply_snippet(pt.network().device("Router0"), "hostname Mine")
    pt.save(tmp_path / "out.pka")
    after = PTFile.load(tmp_path / "out.pka").to_xml()
    # exactly one hostname changed (user network); answer network untouched
    assert after.count(b"hostname ANSWER-KEY") == before.count(b"hostname ANSWER-KEY") == 1
    assert after.count(b"hostname Mine") == 1


def test_locked_batch_cannot_target_answer(locked):
    pt = PTFile.load(locked)
    plan = ops.parse_plan('{"network": "answer", "operations": '
                          '[{"device": "Router0", "op": "hostname", "name": "X"}]}')
    with pytest.raises(ProtectedActivityError):
        ops.apply_plan(pt, plan)


def test_cli_decode_exports_complete_locked_activity(locked, capsys):
    assert main(["decode", str(locked), "-o", str(locked) + ".xml"]) == 0
    assert not capsys.readouterr().err
    assert (locked.parent / (locked.name + ".xml")).read_bytes() == codec.decode_file(locked).xml


def test_cli_answer_network_refused(locked, capsys):
    assert main(["show-config", str(locked), "Router0", "-n", "answer"]) == 2
    assert "locked by its author" in capsys.readouterr().err
    assert main(["show-config", str(locked), "Router0"]) == 0
    assert "ANSWER-KEY" not in capsys.readouterr().out


def test_cli_info_reports_lock(locked, capsys):
    assert main(["info", str(locked)]) == 0
    out = capsys.readouterr().out
    assert "locked_by_author" in out and "role=answer" not in out


def test_values_embedded_in_item_names_are_redacted(tmp_path, capsys):
    """Some tree nodes are *named* after their value (routing entries named by
    network/prefix). Those names must not reach any output."""
    xml = make_pka_xml(password="secret!").replace(
        b'obfuscateName="true" nodeValue="SECRET-X">Secret Item</NAME>',
        b'nodeValue="SECRET-X">10.77.0.0 255.255.255.0 area0</NAME>').replace(
        b'nodeValue="SECRET-POWER">Power</NAME>',
        b'nodeValue="SECRET-R">2001:db8:77::/64: Serial0/0/1(0)</NAME>')
    p = tmp_path / "named.pka"
    p.write_bytes(codec.encode(xml))
    pt = PTFile.load(p)
    paths = [i.path for i in pt.activity.checklist()]
    assert any(x.startswith("Network > Router0 > (entry ") and x.endswith(": value hidden)")
               for x in paths)
    assert not any("10.77" in x or "2001:db8" in x for x in paths)
    assert main(["activity", str(p), "--json"]) == 0
    out = capsys.readouterr().out
    assert "10.77" not in out and "2001:db8" not in out and "value hidden" in out
