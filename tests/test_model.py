import pytest

from ptkit import IosConfig, PTFile
from ptkit.codec import Format


@pytest.fixture
def pt(pkt_xml):
    return PTFile.from_xml(pkt_xml)


def test_device_inventory(pt):
    net = pt.network()
    assert [d.name for d in net.devices()] == ["Router0", "PC0"]
    r = net.device("Router0")
    assert (r.type, r.model, r.hostname, r.is_ios) == ("Router", "1841", "Router", True)
    assert net.device("Router") is not None  # lookup by hostname


def test_port_names_follow_interface_family(pt):
    r = pt.network().device("Router0")
    names = [(p.name, p.ip) for p in r.ports()]
    # Serial ports come first in the XML, FastEthernet ports first in config.
    assert names == [("Serial0/0/0", "10.0.0.1"), ("Serial0/0/1", None),
                     ("FastEthernet0/0", "192.168.1.1"), ("FastEthernet0/1", None)]
    assert pt.network().device("PC0").port("FastEthernet0").ip == "192.168.1.10"


def test_links_resolve(pt):
    net = pt.network()
    (a, pa), (b, pb) = net.links()[0].endpoints
    assert net.resolve_ref(a).name == "Router0" and pa == "FastEthernet0/0"
    assert net.resolve_ref(b).name == "PC0" and pb == "FastEthernet0"


def test_fixture_is_consistent(pt):
    assert pt.network().check() == []


def test_edit_config_syncs_structured_state(pt):
    r = pt.network().device("Router0")
    ed = r.edit_config()
    with ed as cfg:
        cfg.set_hostname("R1")
        cfg.set_interface_ip("FastEthernet0/1", "172.16.0.1", "255.255.0.0")
        cfg.set_interface_shutdown("FastEthernet0/1", False)
    assert "SYS_NAME -> R1" in ed.changes
    port = r.port("FastEthernet0/1")
    assert (port.ip, port.subnet, port.power) == ("172.16.0.1", "255.255.0.0", True)
    assert r.hostname == "R1"
    assert pt.network().check() == []


def test_check_detects_drift(pt):
    r = pt.network().device("Router0")
    r.port("FastEthernet0/0").set_ip("1.2.3.4", "255.0.0.0")
    issues = pt.network().check()
    assert any("FastEthernet0/0" in i for i in issues)
    r.sync_from_config()
    assert pt.network().check() == []


def test_write_memory_and_save_roundtrip(tmp_path, pt):
    r = pt.network().device("Router0")
    with r.edit_config() as cfg:
        cfg.merge("router ospf 1\n network 192.168.1.0 0.0.0.255 area 0\n")
    r.write_memory()
    out = pt.save(tmp_path / "out.pkt")
    again = PTFile.load(out)
    r2 = again.network().device("Router0")
    assert r2.startup_config == r2.running_config
    assert "router ospf 1" in r2.running_config
    assert again.network().check() == []


def test_new_lines_match_existing_indentation(pt):
    r = pt.network().device("Router0")
    r.write_memory()
    xml = pt.to_xml().decode()
    start = xml.index("<STARTUPCONFIG>")
    block = xml[start:xml.index("</STARTUPCONFIG>", start) + len("</STARTUPCONFIG>")]
    assert block.startswith("<STARTUPCONFIG>\n      <LINE>!</LINE>\n")
    assert block.endswith("<LINE>end</LINE>\n     </STARTUPCONFIG>")


def test_endpoint_ip_and_gateway(pt):
    pc = pt.network().device("PC0")
    pc.set_endpoint_ip("FastEthernet0", "192.168.1.20", "255.255.255.0", "192.168.1.254")
    again = PTFile.from_xml(pt.to_xml())
    pc2 = again.network().device("PC0")
    assert pc2.port(0).ip == "192.168.1.20"
    assert pc2.engine.findtext("GATEWAY") == "192.168.1.254"
    assert pc2.port(0).el.findtext("PORT_DHCP_ENABLE") == "false"


def test_invalid_addresses_rejected(pt):
    r = pt.network().device("Router0")
    with pytest.raises(ValueError):
        with r.edit_config() as cfg:
            cfg.set_interface_ip("FastEthernet0/0", "300.1.1.1", "255.0.0.0")
    with pytest.raises(ValueError):
        pt.network().device("PC0").set_endpoint_ip(0, "10.0.0.1", "255.0.255.0")


def test_unrelated_bytes_untouched(pt, pkt_xml):
    pt.network().device("PC0").name = "Workstation"
    out = pt.to_xml()
    assert out.replace(b"Workstation", b"PC0") == pkt_xml


def test_rename_keeps_physical_workspace_node_in_step(pt):
    pc = pt.network().device("PC0")
    pc.name = "Workstation"
    assert [n.findtext("NAME") for n in pc.physical_nodes()] == ["Workstation"]
    assert pt.network().check() == []
    again = PTFile.from_xml(pt.to_xml())
    assert again.network().device("Workstation").physical_nodes()


def test_rename_rejects_duplicates_and_empty(pt):
    pc = pt.network().device("PC0")
    with pytest.raises(ValueError, match="already named"):
        pc.name = "Router0"
    with pytest.raises(ValueError):
        pc.name = "  "
    assert pc.name == "PC0"


def test_check_flags_orphaned_physical_node(pt):
    pc = pt.network().device("PC0")
    pc.engine.find("NAME").text = "Ghost"  # raw edit bypassing the setter
    assert any("physical-workspace nodes" in i for i in pt.network().check())


def test_save_preserves_legacy_format(tmp_path, pkt_xml):
    from ptkit import codec
    src = tmp_path / "old.pkt"
    src.write_bytes(codec.encode(pkt_xml, Format.LEGACY))
    pt = PTFile.load(src)
    assert pt.decoded.format is Format.LEGACY
    assert pt.to_bytes() == src.read_bytes()
    pt.save(tmp_path / "new.pkt")
    assert codec.decode_file(tmp_path / "new.pkt").format is Format.LEGACY


def test_activity_networks_and_assessment(pka_xml):
    pt = PTFile.from_xml(pka_xml)
    assert pt.is_activity
    assert [n.role for n in pt.networks()] == ["user", "initial", "answer"]
    checklist = pt.activity.checklist()
    assert [(i.parts, i.points) for i in checklist] == [
        (("Network", "Router0", "Host Name"), "2"),
        (("Network", "Router0", "Ports", "FastEthernet0/0", "IP Address"), "3"),
        (("Network", "Router0", "(name hidden by author)"), "1"),
    ]
    everything = pt.activity.assessment_items()  # unlocked: ungraded leaves too
    assert [i.parts[-1] for i in everything if not i.graded] == ["Power"]
    assert pt.activity.instructions() == ["Configure <b>Router0</b>."]
    assert pt.activity.summary()["total_points"] == 6
    assert pt.activity.summary()["graded_items"] == 3


def test_edit_one_pka_network_leaves_others(pka_xml):
    pt = PTFile.from_xml(pka_xml)
    with pt.network("initial").device("Router0").edit_config() as cfg:
        cfg.set_hostname("Lab1")
    again = PTFile.from_xml(pt.to_xml())
    assert [n.device("Router0").hostname for n in again.networks()] == ["Router", "Lab1", "ANSWER-KEY"]


# --- IosConfig unit tests ------------------------------------------------------

BASE = ["!", "hostname R", "!", "interface Gi0/0", " no ip address", " shutdown", "!",
        "line con 0", "!", "end"]


def test_merge_interface_block():
    c = IosConfig(BASE)
    c.merge("interface Gi0/0\n ip address 10.1.1.1 255.255.255.0\n no shutdown\n"
            " description uplink\n")
    st = c.interface("Gi0/0")
    assert (st.ip, st.mask, st.shutdown, st.description) == \
        ("10.1.1.1", "255.255.255.0", False, "uplink")
    assert " no ip address" not in c.lines


def test_merge_new_blocks_go_before_line_section():
    c = IosConfig(BASE)
    c.merge("router ospf 1\n network 10.0.0.0 0.255.255.255 area 0\nip route 0.0.0.0 0.0.0.0 10.1.1.2")
    assert c.lines.index("router ospf 1") < c.lines.index("line con 0")
    assert "ip route 0.0.0.0 0.0.0.0 10.1.1.2" in c.lines
    c.merge("no router ospf 1")
    assert "router ospf 1" not in c.lines
    assert not any("network 10.0.0.0" in l for l in c.lines)


def test_new_interface_added_after_last_interface():
    c = IosConfig(BASE)
    c.set_interface_ip("Gi0/1", "10.2.2.1", "255.255.255.0")
    assert c.lines.index("interface Gi0/1") > c.lines.index("interface Gi0/0")
    assert c.lines.index("interface Gi0/1") < c.lines.index("line con 0")


def test_merge_is_idempotent():
    snippet = "interface Gi0/0\n ip address 10.1.1.1 255.255.255.0\nrouter rip\n network 10.0.0.0\n"
    c = IosConfig(BASE)
    c.merge(snippet)
    once = list(c.lines)
    c.merge(snippet)
    assert c.lines == once


def test_hostname_validation():
    with pytest.raises(ValueError):
        IosConfig(BASE).set_hostname("two words")
