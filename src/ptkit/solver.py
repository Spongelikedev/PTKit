"""Create solution activities from their embedded answer topology."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

from lxml import etree

from . import codec
from .assessment import grading_items, reconcile
from .compatibility import repair_legacy_assessment
from .model import NETWORK_TAGS, PTFile, Network
from .xmlio import XmlDoc


class SolveError(ValueError):
    """An activity has no unambiguous embedded solution network."""


def _networks(pt, *, allow_missing_answer=False):
    if not pt.is_activity:
        raise SolveError('Solve PKA requires an activity (.pka); standard .pkt files have no answer network.')
    users = list(pt.root.iter('USER_NETWORK'))
    answers = list(pt.root.iter('ANSWER_NETWORK'))
    if not answers:
        answers = list(pt.root.iter('REFERENCE_NETWORK'))
    if users or answers:
        if len(users) != 1 or len(answers) > 1 or (not answers and not allow_missing_answer):
            raise SolveError('The activity must contain exactly one user network and one answer/reference network.')
        user, answer = users[0], answers[0] if answers else None
    else:
        networks = [el for el in pt.root if el.tag in NETWORK_TAGS]
        if len(networks) != 3 and not (allow_missing_answer and len(networks) in (1, 2)):
            raise SolveError('No unambiguous answer topology was found. This activity cannot be solved from embedded network data.')
        user, answer = networks[0], networks[2] if len(networks) == 3 else None
    if answer is not None and not any(isinstance(el.tag, str) and el.tag.rsplit('}', 1)[-1] == 'DEVICE'
                                     for el in answer.iter()):
        if allow_missing_answer:
            answer = None
        else:
            raise SolveError('The answer topology contains no devices; no solution file was created.')
    if answer is None and not allow_missing_answer:
        raise SolveError('The answer topology contains no devices; no solution file was created.')
    return user, answer


def available(pt) -> bool:
    if pt is None:
        return False
    try:
        _, answer = _networks(pt, allow_missing_answer=True)
        if answer is None and not grading_items(pt):
            return False
    except SolveError:
        return False
    return True


def solution_xml(pt: PTFile) -> bytes:
    """Replace only the user tree, preserving activity/grading/reference bytes.

    Copy the complete answer state, including links, physical state and fields
    outside the configuration model. Named wrappers retain the user wrapper's
    identity. This does not emulate Packet Tracer's grader or activity scripts.
    """
    work = PTFile(pt.decoded, XmlDoc.parse(pt.to_xml()), pt.path)
    user, answer = _networks(work)
    fragment = XmlDoc.parse(work.doc.fragment(answer))
    fragment.root.tag = user.tag
    fragment.root.attrib.clear()
    fragment.root.attrib.update(user.attrib)
    replacement = fragment.serialize()
    marker_name = 'PTKIT_SOLUTION_' + uuid4().hex
    marker = etree.Element(marker_name)
    marker.tail = user.tail
    user.getparent().replace(user, marker)
    xml = work.to_xml()
    token = f'<{marker_name}/>'.encode()
    if xml.count(token) != 1:
        raise SolveError('Could not locate the user network for replacement.')
    xml = xml.replace(token, replacement)
    XmlDoc.parse(xml)
    return xml


@dataclass
class SolveResult:
    path: Path
    devices: int
    size: int
    report_path: Path
    matched: int
    unresolved: int
    reference_copied: bool
    compatibility_repairs: int


def prepare_solution(pt: PTFile) -> tuple[bytes, dict]:
    """Reconcile known checks and report all unverified requirements honestly."""
    _, answer = _networks(pt, allow_missing_answer=True)
    copied = answer is not None
    xml = solution_xml(pt) if copied else pt.to_xml()
    work = PTFile(pt.decoded, XmlDoc.parse(xml))
    user, reference = _networks(work, allow_missing_answer=True)
    compatibility_repairs = repair_legacy_assessment(work, reference)
    checks = reconcile(work, user)
    if not copied and not any(c['status'] == 'matched_locally' for c in checks):
        raise SolveError('No usable answer topology or supported explicit grading requirements were found.')
    # Reparse and independently check the saved representation, not cached views.
    xml = work.to_xml()
    again = PTFile(pt.decoded, XmlDoc.parse(xml))
    user_again, _ = _networks(again, allow_missing_answer=True)
    verification = reconcile(again, user_again, apply=False)
    for check, observed in zip(checks, verification):
        check['status'] = observed['status']
        if 'actual' in observed:
            check['actual'] = observed['actual']
        if 'reason' in observed:
            check['reason'] = observed['reason']
    matched = sum(c['status'] == 'matched_locally' for c in checks)
    consistency = Network(user_again, again.doc, 'user').check()
    return xml, {'reference_copied': copied, 'packet_tracer_score': None,
                 'requires_packet_tracer_validation': True,
                 'matched_locally': matched, 'unresolved': len(checks) - matched,
                 'checks': checks,
                 'local_consistency_issues': consistency,
                 'assessment_compatibility_repairs': compatibility_repairs,
                 'notes': ['Local matching is not a Packet Tracer completion score.',
                           'Unresolved local checks may still pass in Packet Tracer.',
                           'Legacy assessment value repairs derive targets from the unchanged '
                           'reference topology; no check selections or point totals are changed.',
                           'Custom scripts, runtime behavior, connectivity and progress checks '
                           'must be evaluated in Packet Tracer.']}


def write_solution(pt: PTFile, out: str | Path, *, source: str | Path | None = None) -> SolveResult:
    """Write a separate solution copy and verify its decoded XML."""
    out = Path(out)
    if out.suffix.lower() != '.pka':
        raise SolveError('Choose a .pka output file for the solution.')
    original = Path(source or pt.path) if source or pt.path else None
    if original and (out.resolve() == original.resolve()
                     or (out.exists() and original.exists() and out.samefile(original))):
        raise SolveError('Choose a different output file; Solve PKA keeps the source activity unchanged.')
    xml, report = prepare_solution(pt)
    solved = PTFile(pt.decoded, XmlDoc.parse(xml))
    user, _ = _networks(solved, allow_missing_answer=True)
    count = sum(1 for el in user.iter() if isinstance(el.tag, str)
                and el.tag.rsplit('}', 1)[-1] == 'DEVICE')
    # encode_file validates both XML/container bytes before atomic replacement.
    codec.encode_file(out, xml, pt.decoded.format, pt.decoded.zlib_params, verify=True)
    if PTFile.load(out).to_xml() != xml:
        raise SolveError('Solution read-back verification failed.')
    report_path = out.with_suffix('.solve-report.json')
    report['output_file'] = out.name
    report['source_file'] = original.name if original else None
    report['container_format'] = pt.decoded.format.value
    try:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    except OSError as exc:
        raise SolveError(f'Solution was saved to {out}, but its report could not be written: {exc}') from exc
    return SolveResult(out, count, out.stat().st_size, report_path,
                       report['matched_locally'], report['unresolved'], report['reference_copied'],
                       len(report['assessment_compatibility_repairs']))
