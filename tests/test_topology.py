"""Recursive inspection across container schemas, without changing XML."""
import pytest
from ptkit import PTFile, XmlDoc, codec


def test_standard_pkt(pkt_xml):
    pt = PTFile.from_xml(pkt_xml)
    data = pt.extract_topologies()['network']
    router = data['Router0']
    assert router['interfaces']['FastEthernet0/0']['ip'] == '192.168.1.1'
    assert router['configurations']['runningconfig']['hostname'] == 'Router'
    assert data['PC0']['parameters']['engine']['gateway'] == '192.168.1.1'
    assert pt.to_xml() == pkt_xml


@pytest.mark.parametrize('fmt', list(codec.Format))
def test_container_variants(fmt):
    xml = b'''<PACKETTRACER5_ACTIVITY><STATES>
    <USER_NETWORK><WRAPPER><DEVICE name="Router0"><INTERFACES>
    <INTERFACE name="GigabitEthernet0/0"><IP>192.168.1.1</IP>
    <SUBNET>255.255.255.0</SUBNET><OPTION/><OPTION>auto</OPTION></INTERFACE>
    </INTERFACES><ROUTING><ROUTE><DEST>0.0.0.0</DEST><MASK>0.0.0.0</MASK>
    <NEXT_HOP>192.168.1.254</NEXT_HOP></ROUTE></ROUTING></DEVICE></WRAPPER></USER_NETWORK>
    <INITIAL_NETWORK><DEVICE><HOSTNAME>Router0</HOSTNAME><IP>10.0.0.1</IP></DEVICE></INITIAL_NETWORK>
    <ANSWER_NETWORK><DEVICE><NAME>Router0</NAME><IP>10.1.0.1</IP></DEVICE></ANSWER_NETWORK>
    <BASELINE_STATE><DEVICE/><DEVICE/></BASELINE_STATE>
    </STATES></PACKETTRACER5_ACTIVITY>'''
    pt = PTFile.from_bytes(codec.encode(xml, fmt))
    data = pt.extract_topologies()
    assert set(data) == {'user', 'initial', 'answer', 'baseline_state'}
    iface = data['user']['Router0']['interfaces']['GigabitEthernet0/0']
    assert iface['ip'] == '192.168.1.1'
    assert iface['option'] == [None, 'auto']
    assert data['initial']['Router0']['parameters']['ip'] == '10.0.0.1'
    assert data['answer']['Router0']['parameters']['ip'] == '10.1.0.1'
    assert len(data['baseline_state']) == 2
    assert data['user']['Router0']['parameters']['routing']['route']['next_hop'] == '192.168.1.254'
    assert pt.to_xml() == xml


def test_namespaces_comments_and_duplicate_devices():
    doc = XmlDoc.parse(b'''<P xmlns="urn:pt"><!--comment--><ANSWER_NETWORK>
    <DEVICE><NAME>R0</NAME><HOSTNAME>r</HOSTNAME></DEVICE>
    <DEVICE><NAME>R0</NAME></DEVICE></ANSWER_NETWORK></P>''')
    assert set(doc.extract_topologies()['answer']) == {'R0', 'R0#2'}


def test_empty_standard_file():
    assert PTFile.from_xml(b'<PACKETTRACER5/>').extract_topologies() == {}


def test_original_activity_roles(pka_xml):
    data = PTFile.from_xml(pka_xml).extract_topologies()
    assert list(data) == ['user', 'initial', 'answer']


def test_locked_inspection_includes_all_states():
    from conftest import make_pka_xml
    data = PTFile.from_xml(make_pka_xml(password='secret')).extract_topologies()
    assert list(data) == ['user', 'initial', 'answer']
    assert 'ANSWER-KEY' in repr(data)
