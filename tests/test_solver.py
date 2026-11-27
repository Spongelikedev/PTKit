"""Solution files copy reference state without changing the authoring data."""
import pytest
from lxml import etree
from conftest import make_pka_xml
from ptkit import PTFile, codec
from ptkit.solver import SolveError, available, solution_xml, write_solution
from ptkit.xmlio import XmlDoc


@pytest.mark.parametrize('fmt', list(codec.Format))
@pytest.mark.parametrize('password', ['', 'secret'])
@pytest.mark.parametrize('pt4', [False, True])
def test_solution_preserves_everything_except_user_tree(tmp_path, fmt, password, pt4):
    xml = make_pka_xml(password=password, pt4=pt4)
    source = tmp_path / 'start.pka'
    raw = codec.encode(xml, fmt)
    source.write_bytes(raw)
    pt = PTFile.load(source)
    assert available(pt)
    original = pt.to_xml()
    roots = [el for el in pt.root if el.tag in ('PACKETTRACER', 'PACKETTRACER5')]
    old_user, answer = pt.doc.fragment(roots[1]), pt.doc.fragment(roots[2])
    out = tmp_path / 'solution.pka'
    result = write_solution(pt, out)
    solved = PTFile.load(out)
    # The second serialized state is the learner network; preserve the first.
    solved_user = [c for c in solved.root if c.tag in ('PACKETTRACER', 'PACKETTRACER5')][1]
    assert solved.to_xml().replace(solved.doc.fragment(solved_user), old_user, 1) == xml
    assert solved.network('user').device('Router0').hostname == 'SECRET-HOSTNAME'
    assert result.matched == 1 and result.unresolved == 2
    assert result.report_path.is_file()
    assert result.devices == 2
    assert source.read_bytes() == raw and pt.to_xml() == original
    assert solved.decoded.format == fmt
    assert solved.is_protected == bool(password)


def test_named_wrappers_preserve_user_identity_and_activity_metadata(tmp_path, pkt_xml):
    activity = etree.Element('PACKETTRACER5_ACTIVITY')
    body = XmlDoc.parse(pkt_xml).fragment(XmlDoc.parse(pkt_xml).root)
    for role in ('USER_NETWORK', 'INITIAL_NETWORK', 'ANSWER_NETWORK'):
        node = etree.SubElement(activity, role, role=role)
        node.append(etree.fromstring(body.replace(b'\x03', b'')))
    activity.find('ANSWER_NETWORK').find('.//SYS_NAME').text = 'Target'
    etree.SubElement(activity, 'ACTIVITY', PASS='secret')
    pt = PTFile.from_xml(etree.tostring(activity))
    solved = PTFile.from_xml(solution_xml(pt))
    assert solved.root.find('USER_NETWORK').get('role') == 'USER_NETWORK'
    assert solved.root.find('USER_NETWORK').findtext('.//SYS_NAME') == 'Target'
    assert solved.doc.fragment(solved.root.find('INITIAL_NETWORK')) == pt.doc.fragment(pt.root.find('INITIAL_NETWORK'))
    assert solved.doc.fragment(solved.root.find('ANSWER_NETWORK')) == pt.doc.fragment(pt.root.find('ANSWER_NETWORK'))
    assert solved.root.find('ACTIVITY').get('PASS') == 'secret'


def test_refuse_pkt_and_missing_answer_without_writing(tmp_path, pkt_xml):
    for xml in (pkt_xml, b'<PACKETTRACER5_ACTIVITY><USER_NETWORK/></PACKETTRACER5_ACTIVITY>',
                b'<PACKETTRACER5_ACTIVITY><USER_NETWORK/><ANSWER_NETWORK/></PACKETTRACER5_ACTIVITY>'):
        pt = PTFile.from_xml(xml)
        assert not available(pt)
        with pytest.raises(SolveError):
            write_solution(pt, tmp_path / 'never.pka')
        assert not (tmp_path / 'never.pka').exists()


def test_refuse_overwriting_source_and_wrong_extension(tmp_path):
    source = tmp_path / 'original.pka'
    source.write_bytes(codec.encode(make_pka_xml(password='secret')))
    pt = PTFile.load(source)
    original = source.read_bytes()
    with pytest.raises(SolveError, match='different output'):
        write_solution(pt, source)
    with pytest.raises(SolveError, match='.pka'):
        write_solution(pt, tmp_path / 'out.pkt')
    assert source.read_bytes() == original


@pytest.mark.parametrize('pt4', [False, True])
@pytest.mark.parametrize('password', ['', 'secret'])
def test_solve_targets_second_serialized_network_and_preserves_initial(tmp_path, pt4, password):
    # Deliberately inspect physical XML slots, independently of the role mapping.
    # Packet Tracer loads slot 1; the old implementation edited slot 0 instead.
    pt = PTFile.from_xml(make_pka_xml(pt4=pt4, password=password))
    roots = [n for n in pt.root if n.tag in ('PACKETTRACER', 'PACKETTRACER5')]
    initial = pt.doc.fragment(roots[0])
    roots[2].find('.//ENGINE/NAME[.="PC0"]').text = 'Added-PC'
    roots[2].find('.//LINK').set('regression', 'answer-cable')
    pc = roots[1].xpath('.//DEVICE[ENGINE/NAME="PC0"]')[0]
    pc.getparent().remove(pc)
    roots[1].find('.//LINKS').clear()
    answer = pt.doc.fragment(roots[2])
    before = pt.to_xml()
    out = tmp_path / 'solved.pka'
    write_solution(pt, out)
    saved = PTFile.load(out)
    slots = [n for n in saved.root if n.tag in ('PACKETTRACER', 'PACKETTRACER5')]
    assert saved.doc.fragment(slots[0]) == initial
    assert saved.doc.fragment(slots[2]) == answer
    assert slots[1].find('.//LINK[@regression="answer-cable"]') is not None
    assert slots[1].find('.//ENGINE/NAME[.="Added-PC"]') is not None
    assert saved.network().el is slots[1]
    assert saved.network('user').el is slots[1]
    assert len(list(slots[1].iter('DEVICE'))) == 2
    assert pt.to_xml() == before
