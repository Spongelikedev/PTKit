"""Explicit grading constraints are applied without modifying the grader."""
import json
from lxml import etree
from conftest import make_pka_xml
from ptkit import PTFile
from ptkit.solver import prepare_solution, write_solution, available


def activity(requirements, *, answer=True):
    pt = PTFile.from_xml(make_pka_xml(password='internal'))
    old = pt.root.find('COMPARISONS')
    comparisons = etree.Element('COMPARISONS')
    top = etree.SubElement(comparisons, 'NODE')
    etree.SubElement(top, 'NAME', checkType='1').text = 'Network'
    etree.SubElement(top, 'ID').text = 'Network'
    for device, port, ident, expected in requirements:
        dev = etree.SubElement(top, 'NODE')
        etree.SubElement(dev, 'NAME', checkType='1').text = device
        etree.SubElement(dev, 'ID').text = device
        parent = dev
        if port:
            parent = etree.SubElement(dev, 'NODE')
            etree.SubElement(parent, 'NAME', checkType='1').text = port
            etree.SubElement(parent, 'ID').text = port
        leaf = etree.SubElement(parent, 'NODE')
        attrs = {'checkType': '2'}
        if expected is not None:
            attrs['nodeValue'] = expected
        etree.SubElement(leaf, 'NAME', **attrs).text = ident
        etree.SubElement(leaf, 'ID').text = ident
        etree.SubElement(leaf, 'POINTS').text = '1'
    pt.root.replace(old, comparisons)
    if not answer:
        for node in list(pt.root.findall('PACKETTRACER5'))[1:]:
            pt.root.remove(node)
    return PTFile.from_xml(pt.to_xml())


def test_reconcile_hostname_ip_mask_power_and_gateway(tmp_path):
    pt = activity([('Router0', None, 'HostName', 'ExpectedRouter'),
                   ('Router0', 'FastEthernet0/0', 'IpAddress', '172.16.1.1'),
                   ('Router0', 'FastEthernet0/0', 'SubnetMask', '255.255.0.0'),
                   ('Router0', 'FastEthernet0/0', 'Power', 'false'),
                   ('PC0', None, 'DefaultGateway', '172.16.1.1')])
    before = pt.to_xml()
    comparison_bytes = pt.doc.fragment(pt.root.find('COMPARISONS'))
    result = write_solution(pt, tmp_path / 'solution.pka')
    solved = PTFile.load(result.path)
    router = solved.network('user').device('Router0')
    assert router.hostname == 'ExpectedRouter'
    assert router.port('FastEthernet0/0').ip == '172.16.1.1'
    assert router.port('FastEthernet0/0').subnet == '255.255.0.0'
    assert not router.port('FastEthernet0/0').power
    assert solved.network('user').device('PC0').engine.findtext('GATEWAY') == '172.16.1.1'
    assert solved.network('user').check() == []
    assert solved.doc.fragment(solved.root.find('COMPARISONS')) == comparison_bytes
    assert pt.to_xml() == before
    report = json.loads(result.report_path.read_text(encoding='utf-8'))
    assert report['matched_locally'] == 5 and report['unresolved'] == 0
    assert report['packet_tracer_score'] is None
    assert report['requires_packet_tracer_validation']


def test_supported_checks_work_without_answer_topology():
    pt = activity([('Router0', None, 'HostName', 'AssessmentOnly')], answer=False)
    assert available(pt)
    xml, report = prepare_solution(pt)
    assert PTFile.from_xml(xml).network('net0').device('Router0').hostname == 'AssessmentOnly'
    assert report['reference_copied'] is False
    assert report['matched_locally'] == 1


def test_invalid_unknown_missing_and_ambiguous_requirements_are_unresolved():
    pt = activity([('Router0', 'FastEthernet0/0', 'IpAddress', 'not-an-ip'),
                   ('Router0', None, 'CustomRuntimeCheck', "{'object': '<TCPClient>'}"),
                   ('MissingDevice', None, 'HostName', 'Missing'),
                   ('Router0', None, 'Power', None)])
    _, report = prepare_solution(pt)
    assert report['matched_locally'] == 0
    assert report['unresolved'] == 4
    assert all(c.get('reason') for c in report['checks'])


def test_contradictory_checks_do_not_claim_all_matched():
    pt = activity([('Router0', None, 'HostName', 'First'),
                   ('Router0', None, 'HostName', 'Second')])
    xml, report = prepare_solution(pt)
    assert report['matched_locally'] == 1 and report['unresolved'] == 1
    assert PTFile.from_xml(xml).network('user').device('Router0').hostname == 'Second'


def test_check_value_without_correct_interface_never_changes_device_power():
    pt = activity([('Router0', 'FastEthernet99/99', 'Power', 'false')])
    # Keep Ports in the scope so a missing interface cannot fall back to engine POWER.
    comparisons = pt.root.find('COMPARISONS')
    router = comparisons.find('NODE/NODE')
    ports = etree.Element('NODE')
    etree.SubElement(ports, 'NAME', checkType='1').text = 'Ports'
    etree.SubElement(ports, 'ID').text = 'Ports'
    interface = router.findall('NODE')[0]
    router.remove(interface)
    ports.append(interface)
    router.append(ports)
    xml, report = prepare_solution(pt)
    assert PTFile.from_xml(xml).network('user').device('Router0').engine.findtext('POWER') == 'true'
    assert report['unresolved'] == 1
