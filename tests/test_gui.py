"""Drive the Tkinter GUI end to end (tasks run inline via synchronous=True)."""

import json
import os
import threading

import pytest

from ptkit import PTFile, codec, ops
from ptkit.gui import BATCH_LABEL, PtkitApp, html_to_text

tk = pytest.importorskip("tkinter")


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setenv("PTKIT_LOG_FILE", str(tmp_path / "gui.log"))


@pytest.fixture
def root():
    # Creating many Tk() instances in one process occasionally fails Tcl init
    # ("invalid command name tcl_findLibrary"); a retry succeeds. Only skip
    # when Tk is genuinely unavailable (e.g. no display).
    for attempt in range(3):
        try:
            r = tk.Tk()
            break
        except tk.TclError as e:
            if attempt == 2:
                pytest.skip(f"Tk unavailable: {e}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def app(root):
    return PtkitApp(root, synchronous=True)


@pytest.fixture
def pkt(tmp_path, pkt_xml):
    p = tmp_path / "lab.pkt"
    p.write_bytes(codec.encode(pkt_xml))
    return p


@pytest.fixture
def pka(tmp_path, pka_xml):
    p = tmp_path / "act.pka"
    p.write_bytes(codec.encode(pka_xml))
    return p


def log_text(app):
    return app.log_text.get("1.0", "end")


def load(app, path):
    app.in_var.set(str(path))
    app.load_clicked()
    assert app.working is not None, log_text(app)


def select(app, startswith):
    label = next(v for v in app.target_combo["values"] if v.startswith(startswith))
    app.target_var.set(label)
    app._target_changed()


def test_load_populates_panels(app, pkt):
    load(app, pkt)
    assert any(app.meta_tree.item(i, "text") == "Embedded topology parameters"
               for i in app.meta_tree.get_children())
    assert app.out_var.get().endswith("lab-ptkit.pkt")
    targets = app.target_combo["values"]
    assert targets[0] == BATCH_LABEL and any(t.startswith("Router0") for t in targets)
    # first IOS device is preselected, its running-config shown read-only
    assert app.target_var.get().startswith("Router0")
    assert "hostname Router" in app.cfg_view.get("1.0", "end")
    assert str(app.cfg_view["state"]) == "disabled"
    meta = {app.meta_tree.item(i, "text"): app.meta_tree.item(i, "values")
            for i in app.meta_tree.get_children()}
    assert meta["Kind"] == ("Network (.pkt)",)
    assert "Decoded modern container" in log_text(app)


def test_device_mode_apply_and_compile(app, pkt, tmp_path):
    load(app, pkt)
    out = tmp_path / "out.pkt"
    app.out_var.set(str(out))
    select(app, "Router0")
    app.cmd_text.insert("1.0", "hostname Core\ninterface FastEthernet0/1\n"
                               " ip address 10.9.9.1 255.255.255.0\n no shutdown\n")
    app.write_mem.set(True)
    app.apply_clicked()
    log = log_text(app)
    assert "consistent: config text and structured state agree" in log
    assert "Read-back OK, 0 consistency issue(s)" in log
    r = PTFile.load(out).network().device("Router0")
    assert r.hostname == "Core"
    assert r.port("FastEthernet0/1").ip == "10.9.9.1" and r.port("FastEthernet0/1").power
    assert r.startup_config == r.running_config
    # the view refreshes to the new working copy
    assert "hostname Core" in app.cfg_view.get("1.0", "end")
    assert app.applied_sets == 1


def test_batch_mode_multi_device(app, pkt, tmp_path):
    load(app, pkt)
    out = tmp_path / "batch.pkt"
    app.out_var.set(str(out))
    app.target_var.set(BATCH_LABEL)
    app._target_changed()
    assert app.batch_panel.winfo_manager() == "grid"
    plan = {"operations": [
        {"device": "Router0", "op": "hostname", "name": "Edge"},
        {"device": "Router0", "op": "set_ip", "port": "FastEthernet0/1",
         "ip": "172.16.1.1", "mask": "255.255.255.0", "no_shutdown": True},
        {"device": "PC0", "op": "set_ip", "port": 0, "ip": "192.168.1.77",
         "mask": "255.255.255.0", "gateway": "192.168.1.1"},
    ]}
    app.batch_text.delete("1.0", "end")
    app.batch_text.insert("1.0", json.dumps(plan))
    assert app.validate_clicked()
    app.apply_clicked()
    net = PTFile.load(out).network()
    assert net.device("Router0").hostname == "Edge"
    assert net.device("PC0").port(0).ip == "192.168.1.77"
    assert net.check() == []


def test_example_plan_applies_cleanly(app, pkt, tmp_path):
    load(app, pkt)
    app.out_var.set(str(tmp_path / "ex.pkt"))
    select(app, BATCH_LABEL)
    app._insert_example_plan()
    app.apply_clicked()
    assert (tmp_path / "ex.pkt").exists(), log_text(app)


def test_failed_apply_writes_nothing_and_keeps_working_copy(app, pkt, tmp_path):
    load(app, pkt)
    before = app.working.to_xml()
    out = tmp_path / "never.pkt"
    app.out_var.set(str(out))
    select(app, BATCH_LABEL)
    app.batch_text.delete("1.0", "end")
    # first op is valid, second targets a missing device -> whole apply must roll back
    app.batch_text.insert("1.0", json.dumps({"operations": [
        {"device": "Router0", "op": "hostname", "name": "X"},
        {"device": "Ghost", "op": "hostname", "name": "Y"}]}))
    assert not app.validate_clicked()
    app.apply_clicked()
    assert "Apply failed" in log_text(app)
    assert not out.exists()
    assert app.working.to_xml() == before


def test_invalid_json_reported(app, pkt):
    load(app, pkt)
    select(app, BATCH_LABEL)
    app.batch_text.delete("1.0", "end")
    app.batch_text.insert("1.0", '{"operations": [ {"device": "Router0", "op": "explode"} ]}')
    app.apply_clicked()
    assert "unknown op 'explode'" in log_text(app)


def test_end_device_cannot_take_cli(app, pkt):
    load(app, pkt)
    select(app, "PC0")
    assert str(app.cmd_text["state"]) == "disabled"
    app.apply_clicked()
    assert "end devices are configured via Batch mode" in log_text(app)


def test_pka_instructions_networks_and_assessment(app, pka, tmp_path):
    load(app, pka)
    assert "Configure Router0." in app.instr_text.get("1.0", "end")
    assert list(app.net_combo["values"]) == ["user", "initial", "answer"]
    assert app.assess_tree.get_children()  # read-only grading tree shown
    # edit only the "initial" network
    app.net_var.set("initial")
    app._refresh_targets()
    select(app, "Router0")
    app.cmd_text.insert("1.0", "hostname Lab1\n")
    app.out_var.set(str(tmp_path / "act-out.pka"))
    app.apply_clicked()
    nets = PTFile.load(tmp_path / "act-out.pka").networks()
    assert [n.device("Router0").hostname for n in nets] == ["Router", "Lab1", "ANSWER-KEY"]


def test_rejects_bad_output_extension(app, pkt, tmp_path):
    load(app, pkt)
    app.out_var.set(str(tmp_path / "out.txt"))
    app.cmd_text.insert("1.0", "hostname Z\n")
    app.apply_clicked()
    assert "should end in .pkt or .pka" in log_text(app)


def test_threaded_mode_marshals_through_queue(root, pkt, tmp_path):
    app = PtkitApp(root, synchronous=False)
    app.in_var.set(str(pkt))
    app.load_clicked()
    deadline = threading.Event()
    for _ in range(200):  # pump the Tk loop until the worker result is applied
        root.update()
        if app.working is not None and not app._busy:
            break
        deadline.wait(0.02)
    assert app.working is not None
    assert "Decoded modern container" in log_text(app)


def test_html_to_text():
    assert html_to_text("<p>Hi <b>there</b></p><ol><li>one</li><li>two</li></ol>") == \
        "Hi there\n\n  • one\n  • two"
    assert html_to_text("a &amp; b") == "a & b"


def test_clone_is_independent(pkt):
    pt = PTFile.load(pkt)
    c = ops.clone(pt)
    c.network().device("Router0").name = "Changed"
    assert pt.network().device("Router0").name == "Router0"
    assert c.decoded.format is pt.decoded.format


def test_default_output_falls_back_when_input_dir_read_only(monkeypatch, tmp_path):
    from ptkit import gui
    src = tmp_path / "ro" / "lab.pkt"
    src.parent.mkdir()
    monkeypatch.setattr(gui, "is_writable_dir", lambda d: d != src.parent)
    monkeypatch.setattr(gui, "fallback_output_dir", lambda: tmp_path / "docs" / "ptkit")
    assert gui.default_output_path(src) == tmp_path / "docs" / "ptkit" / "lab-ptkit.pkt"
    monkeypatch.setattr(gui, "is_writable_dir", lambda d: True)
    assert gui.default_output_path(src) == src.parent / "lab-ptkit.pkt"


def test_apply_creates_missing_output_folder(app, pkt, tmp_path):
    load(app, pkt)
    out = tmp_path / "new" / "deeper" / "out.pkt"
    app.out_var.set(str(out))
    app.cmd_text.insert("1.0", "hostname Z\n")
    app.apply_clicked()
    assert out.exists(), log_text(app)


def test_apply_refuses_read_only_folder_with_guidance(app, pkt, tmp_path, monkeypatch):
    from ptkit import gui
    load(app, pkt)
    out = tmp_path / "locked" / "out.pkt"
    out.parent.mkdir()
    monkeypatch.setattr(gui, "is_writable_dir", lambda d: d != out.parent)
    app.out_var.set(str(out))
    app.cmd_text.insert("1.0", "hostname Z\n")
    app.apply_clicked()
    assert "Cannot write to" in log_text(app) and "Browse" in log_text(app)
    assert not out.exists()


def test_is_writable_dir_real_locations(tmp_path):
    from ptkit.gui import is_writable_dir
    assert is_writable_dir(tmp_path)
    assert not is_writable_dir(tmp_path / "does-not-exist")


def test_callback_errors_reach_log_and_file(app, tmp_path):
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        app._on_tk_error(type(e), e, e.__traceback__)
    assert "RuntimeError: boom" in log_text(app)
    assert "RuntimeError: boom" in (tmp_path / "gui.log").read_text(encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("ProgramFiles"), reason="Windows only")
def test_is_writable_dir_program_files_returns_promptly():
    """Regression: a tempfile-based probe looped ~forever on Program Files."""
    import time as _time
    from pathlib import Path as _P
    from ptkit.gui import is_writable_dir
    t0 = _time.perf_counter()
    # Administrators and CI runners may legitimately have write permission.
    assert isinstance(is_writable_dir(_P(os.environ["ProgramFiles"])), bool)
    assert _time.perf_counter() - t0 < 2


@pytest.mark.parametrize("size", ["1000x700", "1600x1000"])
def test_device_panel_controls_visible_when_rendered(root, pkt, size):
    """Regression: the write-memory checkbox was packed after the expanding
    text box and squeezed to zero height in a real window."""
    app = PtkitApp(root, synchronous=True)
    root.deiconify()
    root.geometry(size)
    load(app, pkt)
    root.update()
    chk = app.write_mem_check
    assert chk.winfo_ismapped() and chk.winfo_height() > 5
    assert app.apply_btn.winfo_ismapped() and app.apply_btn.winfo_height() > 5
    root.withdraw()


def test_default_target_prefers_router(app, pkt):
    load(app, pkt)
    assert app.target_var.get().startswith("Router0")


def test_initial_browse_dir_prefers_current_then_pt_saves(app, tmp_path, monkeypatch):
    from pathlib import Path as _P
    saves = tmp_path / "Cisco Packet Tracer 9.0.0" / "saves"
    saves.mkdir(parents=True)
    monkeypatch.setattr(_P, "home", classmethod(lambda cls: tmp_path))
    assert app._initial_browse_dir() == str(saves)
    other = tmp_path / "labs"
    other.mkdir()
    app.in_var.set(str(other / "x.pkt"))
    assert app._initial_browse_dir() == str(other)


def test_locked_pka_in_gui(app, tmp_path):
    from conftest import make_pka_xml
    p = tmp_path / "locked.pka"
    p.write_bytes(codec.encode(make_pka_xml(password="secret!")))
    load(app, p)
    assert list(app.net_combo["values"]) == ["user"]
    assert "XML export includes all network states" in log_text(app)
    rows = [app.assess_tree.item(i, "text") for i in app.assess_tree.get_children()]
    assert rows == ["Router0"]  # graded checklist shown, without values
    assert "SECRET" not in app.tree_to_text(app.assess_tree)
    # the user's own network is fully editable
    select(app, "Router0")
    assert "ANSWER-KEY" not in app.cfg_view.get("1.0", "end")
    app.cmd_text.insert("1.0", "hostname Mine\n")
    app.out_var.set(str(tmp_path / "out.pka"))
    app.apply_clicked()
    assert PTFile.load(tmp_path / "out.pka").network().device("Router0").hostname == "Mine"
    # a batch plan aimed at the answer network is refused, nothing written
    select(app, BATCH_LABEL)
    app.batch_text.delete("1.0", "end")
    app.batch_text.insert("1.0", '{"network": "answer", "operations": '
                                 '[{"device": "Router0", "op": "hostname", "name": "X"}]}')
    app.out_var.set(str(tmp_path / "never.pka"))
    app.apply_clicked()
    assert "locked by its author" in log_text(app) and not (tmp_path / "never.pka").exists()


def test_netacad_comment_banner_is_hidden_like_packet_tracer():
    """Real pattern from a NetAcad .pka: the banner mentions '(<!-- and -- >)'.
    Browsers/PT end the comment only at the first '-->'; Python's HTMLParser
    alone would end it at '-- >' and leak the rest."""
    from ptkit.gui import strip_html_comments
    page = ("<h2>Activity 2.6.1</h2>\n<!-- \n*****\nText between these two sets of symbols "
            "(<!-- and -- >) are comments for the author\n*****\n-->\n\n"
            "<!-- Standard NOTE TO USER -->\n<p>NOTE TO USER: read this</p>\n"
            "<!--\n=====\n-->\n<h3>Introduction</h3>\n<p>Body</p>")
    text = html_to_text(page)
    assert text.startswith("Activity 2.6.1")
    assert "NOTE TO USER: read this" in text and "Introduction" in text and "Body" in text
    assert "comments for the author" not in text and "--" not in text
    assert strip_html_comments("a<!-- x -->b<!-- y") == "ab"


def test_new_text_follows_document_control_char_style():
    from ptkit.xmlio import XmlDoc
    ref_doc = XmlDoc.parse(b"<A><L>banner &#x3;hi&#x3;</L><L>x</L></A>\x00")
    ref_doc.root[1].text = ref_doc.to_tree("motd \x03new\x03")
    assert ref_doc.serialize() == b"<A><L>banner &#x3;hi&#x3;</L><L>motd &#x3;new&#x3;</L></A>\x00"
    raw_doc = XmlDoc.parse(b"<A><L>banner \x03hi\x03</L><L>x</L></A>")
    raw_doc.root[1].text = raw_doc.to_tree("motd \x03new\x03")
    assert raw_doc.serialize() == b"<A><L>banner \x03hi\x03</L><L>motd \x03new\x03</L></A>"


def test_config_edit_rewrites_only_changed_lines(pkt_xml):
    import difflib
    from ptkit import ops
    pt = PTFile.from_xml(pkt_xml)
    ops.apply_snippet(pt.network().device("Router0"), "int fa0/0\n description up\n")
    diff = [l for l in difflib.unified_diff(pkt_xml.decode().splitlines(),
                                             pt.to_xml().decode().splitlines(), lineterm="", n=0)
            if not l.startswith(("---", "+++", "@@"))]
    assert diff == ["+      <LINE> description up</LINE>"]


def test_grading_checklist_tab_groups_by_device_without_values(app, pka):
    load(app, pka)
    tree = app.assess_tree
    groups = tree.get_children()
    assert [tree.item(g, "text") for g in groups] == ["Router0"]
    rows = [(tree.item(i, "text"), tree.item(i, "values")) for i in tree.get_children(groups[0])]
    assert [r[0] for r in rows] == ["Host Name", "Ports > FastEthernet0/0 > IP Address",
                                    "(name hidden by author)"]
    assert "3 graded item(s)" in app.check_note.cget("text")
    text = app.tree_to_text(tree)
    assert "Ports > FastEthernet0/0 > IP Address  [3 pts, Connectivity]" in text
    assert "SECRET" not in text and "Power" not in text


def test_locked_activity_still_shows_checklist(app, tmp_path):
    from conftest import make_pka_xml
    p = tmp_path / "locked.pka"
    p.write_bytes(codec.encode(make_pka_xml(password="secret!")))
    load(app, p)
    assert len(app.assess_tree.get_children(app.assess_tree.get_children()[0])) == 3
    assert "SECRET" not in app.tree_to_text(app.assess_tree)


def test_copy_tab_buttons_and_readonly_selection(app, pka, root):
    load(app, pka)
    app.info_nb.select(0)
    app.copy_current_tab()
    assert root.clipboard_get().startswith("Configure Router0.")
    app.info_nb.select(2)
    app.copy_current_tab()
    clip = root.clipboard_get()
    assert "graded item(s)" in clip and "Router0" in clip and "SECRET" not in clip
    app.info_nb.select(1)
    app.copy_current_tab()
    assert "Kind: Activity (.pka)" in root.clipboard_get()
    # read-only panes stay read-only but are selectable/copyable
    t = app.cfg_view
    assert str(t["state"]) == "disabled"
    app._select_all(t)
    t.event_generate("<<Copy>>")
    assert "hostname Router" in root.clipboard_get()
    # Ctrl+C on selected checklist rows copies just those rows
    tree = app.assess_tree
    first = tree.get_children(tree.get_children()[0])[0]
    tree.selection_set(first)
    app._copy_tree(tree, selected_only=True)
    assert root.clipboard_get() == "Host Name  [2 pts, Other]"


def test_gui_export_then_import_reencodes_and_loads(app, pkt, tmp_path):
    load(app, pkt)
    app.export_xml_clicked(folder=tmp_path / "lab-xml")
    log = log_text(app)
    assert "wrote document.xml" in log and "Exported 2 file(s)" in log
    doc = tmp_path / "lab-xml" / "document.xml"
    doc.write_bytes(doc.read_bytes().replace(b"<SYS_NAME>Router<", b"<SYS_NAME>FromXml<"))
    app.import_xml_clicked(source=tmp_path / "lab-xml", out=tmp_path / "back.pkt")
    log = log_text(app)
    assert "using edited document.xml" in log and "verified identical" in log
    assert app.loaded_path == tmp_path / "back.pkt"  # the new file is loaded
    assert app.working.network().device("Router0").hostname == "FromXml"


def test_gui_export_includes_session_edits(app, pkt, tmp_path):
    load(app, pkt)
    app.out_var.set(str(tmp_path / "edited.pkt"))
    app.cmd_text.insert("1.0", "hostname Sess\n")
    app.apply_clicked()
    app.export_xml_clicked(folder=tmp_path / "x")
    assert b"hostname Sess" in (tmp_path / "x" / "document.xml").read_bytes()
    assert "1 change set(s) applied in this session" in log_text(app)


def test_gui_import_validation_error_is_logged(app, tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_bytes(b"<PACKETTRACER5><NETWORK></PACKETTRACER5>")
    app.import_xml_clicked(source=bad, out=tmp_path / "never.pkt")
    assert "validation error" in log_text(app) and "invalid XML" in log_text(app)
    assert not (tmp_path / "never.pkt").exists()


def test_gui_locked_export_import(app, tmp_path):
    from conftest import make_pka_xml
    p = tmp_path / "locked.pka"
    p.write_bytes(codec.encode(make_pka_xml(password="secret!")))
    load(app, p)
    app.export_xml_clicked(folder=tmp_path / "lx")
    assert "complete activity XML exported" in log_text(app)
    assert sorted(f.name for f in (tmp_path / "lx").iterdir()) == ["document.xml", "manifest.json",
                          "network-answer.xml", "network-initial.xml", "network-user.xml"]
    assert b"SECRET" in (tmp_path / "lx" / "document.xml").read_bytes()
    assert b"ANSWER-KEY" in (tmp_path / "lx" / "network-answer.xml").read_bytes()
    app.import_xml_clicked(source=tmp_path / "lx", out=tmp_path / "back.pka")
    assert "no files were edited" in log_text(app)
    assert (tmp_path / "back.pka").read_bytes() == p.read_bytes()


def test_gui_import_fixes_wrong_extension(app, pka, tmp_path):
    load(app, pka)
    app.export_xml_clicked(folder=tmp_path / "ax")
    app.import_xml_clicked(source=tmp_path / "ax", out=tmp_path / "wrong.pkt")
    assert (tmp_path / "wrong.pka").exists() and "saving as .pka" in log_text(app)


def test_reference_tab_selects_answer_and_copies_working_plan(app, pka, root):
    load(app, pka)
    assert app.reference_var.get() == 'answer'
    assert 'Explicit reference topology' in app.reference_note.cget('text')
    assert 'ANSWER-KEY' in app.tree_to_text(app.reference_tree)
    app.info_nb.select(3)
    app.copy_current_tab()
    assert 'ANSWER-KEY' in root.clipboard_get()
    app.copy_reference_batch()
    plan = ops.parse_plan(root.clipboard_get())
    assert plan['network'] == 'user'
    assert plan['reference_source'] == 'answer'
    assert any('hostname ANSWER-KEY' in op.get('config', '') for op in plan['operations'])
    # Copy does not change the file or the working network.
    assert app.working.network().device('Router0').hostname == 'Router'
    clone = ops.clone(app.working)
    ops.apply_plan(clone, plan)
    assert clone.network().device('Router0').hostname == 'ANSWER-KEY'


def test_reference_pkt_baseline_stays_fixed_after_apply(app, pkt, root, tmp_path):
    load(app, pkt)
    assert app.reference_var.get() == 'network'
    assert 'Baseline topology' in app.reference_note.cget('text')
    original = json.dumps(app.reference_states)
    app.cmd_text.insert('1.0', 'hostname Edited\n')
    app.out_var.set(str(tmp_path / 'edited.pkt'))
    app.apply_clicked()
    assert app.working.network().device('Router0').hostname == 'Edited'
    assert json.dumps(app.reference_states) == original
    app.copy_reference_batch()
    assert 'hostname Router' in root.clipboard_get()


def test_reference_locked_activity_available_and_replaced_on_load(app, pkt, root, tmp_path):
    from conftest import make_pka_xml
    source = tmp_path / 'locked-ref.pka'
    source.write_bytes(codec.encode(make_pka_xml(password='secret')))
    load(app, source)
    assert 'ANSWER-KEY' in app.tree_to_text(app.reference_tree)
    app.copy_reference_batch()
    assert ops.parse_plan(root.clipboard_get())['network'] == 'user'
    load(app, pkt)
    assert 'ANSWER-KEY' not in app.tree_to_text(app.reference_tree)
    assert list(app.reference_states) == ['network']


def test_reference_source_switch_and_no_file(app, pka):
    assert str(app.reference_copy_btn['state']) == 'disabled'
    app.copy_reference_batch()
    load(app, pka)
    app.reference_var.set('initial')
    app._show_reference()
    assert 'ANSWER-KEY' not in app.tree_to_text(app.reference_tree)
    assert 'initial' in app.reference_note.cget('text')


def test_reference_named_activity_wrappers_load(app, pkt_xml, root, tmp_path):
    from lxml import etree
    # Real device schemas wrapped in named states rather than three PT roots.
    from ptkit.xmlio import XmlDoc
    doc = XmlDoc.parse(pkt_xml)
    activity = etree.Element('PACKETTRACER5_ACTIVITY')
    for role in ('USER_NETWORK', 'INITIAL_NETWORK', 'ANSWER_NETWORK'):
        node = etree.SubElement(activity, role)
        node.append(etree.fromstring(doc.fragment(doc.root).replace(b'\x03', b'')))
    source = tmp_path / 'named.pka'
    source.write_bytes(codec.encode(etree.tostring(activity)))
    load(app, source)
    assert app.reference_var.get() == 'answer'
    assert app.working.network('user').device('Router0').hostname == 'Router'
    app.copy_reference_batch()
    assert ops.parse_plan(root.clipboard_get())['network'] == 'user'


@pytest.mark.parametrize('geometry', ['1000x700', '1600x1000'])
def test_reference_panel_controls_fit_window(app, pkt, root, geometry):
    root.geometry(geometry)
    root.deiconify()
    load(app, pkt)
    app.info_nb.select(3)
    root.update()
    for widget in (app.reference_tree, app.reference_copy_btn, app.reference_combo, app.solve_btn):
        assert widget.winfo_ismapped()
        assert widget.winfo_width() > 50
        assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
        assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
    assert app.reference_tree.winfo_height() > 50


def test_solve_locked_pka_creates_solution_copy(app, root, tmp_path):
    from conftest import make_pka_xml
    source = tmp_path / 'solve-start.pka'
    source.write_bytes(codec.encode(make_pka_xml(password='secret')))
    original = source.read_bytes()
    assert str(app.solve_btn['state']) == 'disabled'
    load(app, source)
    assert str(app.solve_btn['state']) == 'normal'
    out = tmp_path / 'solved.pka'
    app.solve_clicked(out=out)
    assert PTFile.load(out).network('user').device('Router0').hostname == 'SECRET-HOSTNAME'
    assert source.read_bytes() == original
    assert app.working.network('user').device('Router0').hostname == 'Router'
    assert 'Read-back verified' in log_text(app)
    assert str(app.solve_btn['state']) == 'normal'


def test_solve_rejects_standard_pkt_and_source_overwrite(app, pkt, tmp_path):
    from conftest import make_pka_xml
    load(app, pkt)
    assert str(app.solve_btn['state']) == 'disabled'
    out = tmp_path / 'not-solved.pka'
    app.solve_clicked(out=out)
    assert not out.exists()
    source = tmp_path / 'start.pka'
    source.write_bytes(codec.encode(make_pka_xml()))
    load(app, source)
    original = source.read_bytes()
    app.solve_clicked(out=source)
    assert 'different output file' in log_text(app)
    assert source.read_bytes() == original


def test_solve_save_dialog_cancel_does_not_change_working(app, pka, monkeypatch):
    from ptkit import gui
    load(app, pka)
    before = app.working.to_xml()
    monkeypatch.setattr(gui.filedialog, 'asksaveasfilename', lambda **kw: '')
    app.solve_clicked()
    assert app.working.to_xml() == before


def test_license_notice_is_available_without_loading_an_activity(app):
    text = app.show_license()
    content = text.get('1.0', 'end')
    assert 'Sponge' in content and 'MIT License' in content
    assert 'at your own risk' in content
    assert 'WITHOUT WARRANTY' in content
    assert str(text['state']) == 'disabled'
    text.winfo_toplevel().destroy()
