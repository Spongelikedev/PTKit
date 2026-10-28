"""Reference selection and executable batch conversion."""
from ptkit import PTFile, ops
from ptkit.reference import batch_plan, preferred_state


def test_prefer_reference_then_baseline():
    assert preferred_state({'user': {}, 'initial': {}, 'answer': {}}) == 'answer'
    assert preferred_state({'network': {}, 'reference_topology': {}}) == 'reference_topology'
    assert preferred_state({'user': {}, 'baseline_state': {}}) == 'baseline_state'
    assert preferred_state({}) == ''


def test_batch_restores_baseline_addresses_hostname_and_routes(pkt_xml):
    pt = PTFile.from_xml(pkt_xml)
    router = pt.network().device('Router0')
    ops.apply_snippet(router, 'router ospf 1\n network 192.168.1.0 0.0.0.255 area 0')
    baseline = pt.extract_topologies()['network']
    ops.apply_snippet(router, 'hostname Changed\ninterface FastEthernet0/0\n ip address 10.0.0.1 255.255.255.0')
    pt.network().device('PC0').set_endpoint_ip(0, '10.0.0.10', '255.255.255.0', '10.0.0.1')
    plan, notes = batch_plan(baseline, pt.network(), 'network')
    assert not notes
    assert plan['reference_parameters'] == baseline
    ops.parse_plan(__import__('json').dumps(plan))
    ops.apply_plan(pt, plan)
    assert router.hostname == 'Router'
    assert router.port('FastEthernet0/0').ip == '192.168.1.1'
    assert pt.network().device('PC0').port(0).ip == '192.168.1.10'
    assert any('router ospf 1' in op.get('config', '') for op in plan['operations'])


def test_unknown_devices_are_reported(pkt_xml):
    pt = PTFile.from_xml(pkt_xml)
    baseline = pt.extract_topologies()['network']
    baseline['MissingDevice'] = baseline['Router0']
    plan, notes = batch_plan(baseline, pt.network(), 'answer')
    assert 'MissingDevice' in notes[0]
    assert all(op['device'] != 'MissingDevice' for op in plan['operations'])


def test_structured_fallback(pkt_xml):
    pt = PTFile.from_xml(pkt_xml)
    data = {'Router0': {'parameters': {'engine': {'sys_name': 'TargetRouter'}},
                       'interfaces': {'FastEthernet0/0': {'ip': '172.16.1.1',
                                                        'subnet': '255.255.255.0'}}}}
    plan, _ = batch_plan(data, pt.network(), 'initial')
    ops.apply_plan(pt, plan)
    router = pt.network().device('Router0')
    assert router.hostname == 'TargetRouter'
    assert router.port('FastEthernet0/0').ip == '172.16.1.1'
