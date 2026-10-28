import json

import pytest

from ptkit import PTFile, codec
from ptkit.cli import main


@pytest.fixture
def pkt(tmp_path, pkt_xml):
    p = tmp_path / "lab.pkt"
    p.write_bytes(codec.encode(pkt_xml))
    return p


def test_decode_encode_cycle(tmp_path, pkt, pkt_xml):
    xml = tmp_path / "lab.xml"
    assert main(["decode", str(pkt), "-o", str(xml)]) == 0
    assert xml.read_bytes() == pkt_xml
    out = tmp_path / "re.pkt"
    assert main(["encode", str(xml), "-o", str(out)]) == 0
    assert out.read_bytes() == pkt.read_bytes()


def test_encode_refuses_malformed_xml(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_bytes(b"<A><B></A>")
    assert main(["encode", str(bad), "-o", str(tmp_path / "x.pkt")]) == 2
    assert not (tmp_path / "x.pkt").exists()


def test_verify_and_check(pkt, capsys):
    assert main(["verify", str(pkt)]) == 0
    assert "container_roundtrip=identical" in capsys.readouterr().out
    assert main(["check", str(pkt)]) == 0


def test_info_json(pkt, capsys):
    assert main(["info", str(pkt), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["networks"][0]["devices"] == 2


def test_set_ip_requires_output(pkt):
    with pytest.raises(SystemExit):
        main(["set-ip", str(pkt), "Router0", "FastEthernet0/1", "10.9.9.1", "255.255.255.0"])


def test_set_ip_and_apply(tmp_path, pkt):
    out = tmp_path / "out.pkt"
    assert main(["set-ip", str(pkt), "Router0", "FastEthernet0/1", "10.9.9.1",
                 "255.255.255.0", "--no-shutdown", "-o", str(out)]) == 0
    snippet = tmp_path / "s.txt"
    snippet.write_text("hostname Core\nrouter rip\n network 10.0.0.0\n")
    assert main(["apply", str(out), "Router0", str(snippet), "--write-memory", "--in-place"]) == 0
    r = PTFile.load(out).network().device("Router0")
    assert r.hostname == "Core"
    assert r.port("FastEthernet0/1").ip == "10.9.9.1"
    assert r.port("FastEthernet0/1").power
    assert "router rip" in r.startup_config


def test_batch_plan(tmp_path, pkt):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"operations": [
        {"device": "Router0", "op": "hostname", "name": "Edge"},
        {"device": "Router0", "op": "set_ip", "port": "FastEthernet0/1",
         "ip": "172.20.0.1", "mask": "255.255.255.0", "no_shutdown": True},
        {"device": "PC0", "op": "set_ip", "port": 0, "ip": "192.168.1.99",
         "mask": "255.255.255.0", "gateway": "192.168.1.1"},
        {"device": "Router0", "op": "write_memory"},
    ]}))
    out = tmp_path / "b.pkt"
    assert main(["batch", str(pkt), str(plan), "-o", str(out)]) == 0
    net = PTFile.load(out).network()
    assert net.device("Router0").hostname == "Edge"
    assert net.device("PC0").port(0).ip == "192.168.1.99"
    assert net.check() == []


def test_unknown_device_is_clean_error(pkt, capsys):
    assert main(["show-config", str(pkt), "Nope"]) == 2
    assert "no device 'Nope'" in capsys.readouterr().err
