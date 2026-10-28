import pytest

from ptkit import PTFile, ops


@pytest.fixture
def router(pkt_xml):
    pt = PTFile.from_xml(pkt_xml)
    return pt, pt.network().device("Router0")


def test_abbreviations_expand_like_ios(router):
    _, r = router
    got = ops.expand_interface_names(
        r, "host R9\nint s0/0/1\n ip add 10.1.1.1 255.255.255.252\n desc uplink\n no shut\n"
           "interface fa0/0.10\ninterface lo0\nno int lo5\ninterface Vlan1\n shut")
    assert got.splitlines() == [
        "hostname R9", "interface Serial0/0/1", " ip address 10.1.1.1 255.255.255.252",
        " description uplink", " no shutdown", "interface FastEthernet0/0.10",
        "interface Loopback0", "no interface Loopback5", "interface Vlan1", " shutdown"]


def test_abbreviated_snippet_merges_and_syncs(router):
    pt, r = router
    changes = ops.apply_snippet(r, "int s0/0/1\n ip add 10.1.1.1 255.255.255.252\n no shut")
    assert "Serial0/0/1 IP -> 10.1.1.1 255.255.255.252" in changes
    assert r.running_config.count("interface Serial0/0/1") == 1
    assert pt.network().check() == []


def test_unknown_physical_interface_rejected_without_changes(router):
    _, r = router
    before = r.running_config
    with pytest.raises(ValueError, match="has no interface GigabitEthernet0/0/1"):
        ops.apply_snippet(r, "interface GigabitEthernet0/0/1\n no shutdown")
    assert r.running_config == before


def test_virtual_interfaces_allowed(router):
    pt, r = router
    ops.apply_snippet(r, "interface Loopback0\n ip address 1.1.1.1 255.255.255.255")
    assert "interface Loopback0" in r.running_config
    assert pt.network().check() == []


def test_interface_range_rejected(router):
    with pytest.raises(ValueError, match="interface range"):
        ops.apply_snippet(router[1], "interface range fa0/0 - 1\n shutdown")


def test_end_device_snippet_rejected(pkt_xml):
    pc = PTFile.from_xml(pkt_xml).network().device("PC0")
    with pytest.raises(ValueError, match="no IOS CLI"):
        ops.apply_snippet(pc, "hostname x")


@pytest.mark.parametrize("text,msg", [
    ("not json", "invalid JSON"),
    ('{"operations": []}', "non-empty"),
    ('[{"device": "R", "op": "nope"}]', "unknown op"),
    ('[{"device": "R", "op": "set_ip", "ip": "1.1.1.1"}]', "missing port, mask"),
    ('[{"op": "write_memory"}]', "missing device"),
])
def test_plan_validation(text, msg):
    with pytest.raises(ValueError, match=msg):
        ops.parse_plan(text)


def test_bare_list_plan_accepted():
    assert ops.parse_plan('[{"device": "R", "op": "write_memory"}]')["operations"]


def test_example_plan_is_valid_and_applies(pkt_xml):
    pt = PTFile.from_xml(pkt_xml)
    plan = ops.parse_plan(ops.example_plan(pt.network()))
    net, changes = ops.apply_plan(pt, plan)
    assert changes and net.check() == []
