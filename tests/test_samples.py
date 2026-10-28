"""Round-trip every sample file shipped with a local Packet Tracer install.

Skipped when no install is found. Set PTKIT_SAMPLES to point at another
directory of .pkt/.pka files. By default a subset of samples is checked; set
PTKIT_ALL_SAMPLES=1 to check them all (takes a few minutes).
"""

import os

import pytest

from conftest import sample_files
from ptkit import PTFile

FILES = sample_files()
if FILES and not os.environ.get("PTKIT_ALL_SAMPLES"):
    FILES = FILES[::10]

pytestmark = [
    pytest.mark.samples,
    pytest.mark.skipif(not FILES, reason="no Packet Tracer sample files found"),
]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_sample_roundtrip_is_byte_identical(path):
    raw = path.read_bytes()
    pt = PTFile.from_bytes(raw)
    assert pt.to_xml() == pt.decoded.xml
    assert pt.to_bytes() == raw


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_sample_is_internally_consistent(path):
    pt = PTFile.from_bytes(path.read_bytes())
    for net in pt.networks():
        assert net.check() == []


@pytest.mark.parametrize("path", FILES[:6], ids=lambda p: p.name)
def test_sample_edit_then_reload(tmp_path, path):
    pt = PTFile.from_bytes(path.read_bytes())
    net = pt.network()
    routers = [d for d in net.devices() if d.is_ios and _has_named_ports(d)]
    if not routers:
        pytest.skip("no IOS device with interfaces")
    dev = routers[0]
    port = next(p for p in dev.ports() if p.name)
    with dev.edit_config() as cfg:
        cfg.set_hostname("PtkitTest")
        cfg.set_interface_ip(port.name, "198.51.100.1", "255.255.255.0")
    out = pt.save(tmp_path / path.name)
    again = PTFile.load(out).network()
    dev2 = again.devices()[dev.index]
    assert "hostname PtkitTest" in dev2.running_config
    assert dev2.port(port.name).ip == "198.51.100.1"
    assert again.check() == []


def _has_named_ports(dev):
    return any(p.name for p in dev.ports())
