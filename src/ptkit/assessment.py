"""Conservative reconciliation of explicit scalar grading requirements.

Local matches are evidence about stored state, never a Packet Tracer score.
Unknown IDs, ambiguous paths, absent values and runtime checks stay unresolved.
"""
from __future__ import annotations

import ipaddress
import re

from . import ops
from .iosconfig import IosConfig
from .model import Network


def _key(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())


def grading_items(pt):
    items = []
    for node in pt.root.iterfind('COMPARISONS//NODE'):
        label = node.find('NAME')
        if node.findall('NODE') or label is None or label.get('checkType') != '2':
            continue
        ancestors = [a for a in node.iterancestors() if a.tag == 'NODE'][::-1] + [node]
        segments = [(pt.doc.from_tree(a.findtext('NAME')) or '',
                     pt.doc.from_tree(a.findtext('ID')) or '') for a in ancestors]
        items.append({'path': ' > '.join(name or ident for name, ident in segments),
                      'segments': segments, 'id': node.findtext('ID') or '',
                      'expected': pt.doc.from_tree(label.get('nodeValue')),
                      'points': node.findtext('POINTS') or None})
    return items


def _boolean(value):
    if value.lower() in ('true', 'yes', 'on', '1', 'enabled', 'up'):
        return True
    if value.lower() in ('false', 'no', 'off', '0', 'disabled', 'down'):
        return False
    raise ValueError('expected a boolean value')


def reconcile(pt, user, *, apply=True):
    """Apply and independently re-read supported explicit grading values."""
    net = Network(user, pt.doc, 'user')
    devices = net.devices()
    findings, resolved = [], []
    items = grading_items(pt)

    def peer_value(item, keys):
        values = {other['expected'] for other in items
                  if other['segments'][:-1] == item['segments'][:-1]
                  and _key(other['id'] or other['segments'][-1][0]) in keys
                  and other['expected'] is not None}
        return next(iter(values)) if len(values) == 1 else None

    def config_value(dev, port, tag):
        if not dev.is_ios:
            return None
        cfg = IosConfig(dev.running_config)
        if tag == 'SYS_NAME':
            return cfg.hostname() or ''
        if port is None or tag not in ('IP', 'SUBNET', 'POWER'):
            return None
        state = cfg.interface(port.name)
        if state is None:
            return ''
        return {'IP': state.ip or '', 'SUBNET': state.mask or '',
                'POWER': 'false' if state.shutdown else 'true'}[tag]

    for item in items:
        report = {k: v for k, v in item.items() if k != 'segments'}
        report.update(status='unresolved', changed=False)
        findings.append(report)
        expected = item['expected']
        if expected is None:
            report['reason'] = 'No explicit expected value is stored for this check.'
            continue
        names = {value for pair in item['segments'][:-1] for value in pair if value}
        matches = [d for d in devices if d.name in names or d.save_ref_id in names]
        if len(matches) != 1:
            report['reason'] = 'No unique device matches the assessment path.'
            continue
        dev = matches[0]
        scope_names = {_key(value) for pair in item['segments'][:-1] for value in pair if value}
        ports = [p for p in dev.ports() if p.name and _key(p.name) in scope_names]
        if len(ports) > 1:
            report['reason'] = 'Multiple interfaces match the assessment path.'
            continue
        port = ports[0] if ports else None
        key = _key(item['id'] or item['segments'][-1][0])
        # Match only known semantics. Unrecognized IDs are never guessed.
        if key in ('hostname', 'sysname') and port is None:
            tag, element, kind = 'SYS_NAME', dev.engine, 'hostname'
        elif key in ('ip', 'ipaddress', 'ipv4address') and port is not None:
            tag, element, kind = 'IP', port.el, 'ipv4'
        elif key in ('subnet', 'subnetmask', 'mask') and port is not None:
            tag, element, kind = 'SUBNET', port.el, 'mask'
        elif key in ('gateway', 'defaultgateway') and port is None and not dev.is_ios:
            tag, element, kind = 'GATEWAY', dev.engine, 'ipv4'
        elif key in ('dns', 'dnsserver', 'dnsserverip') and port is None and not dev.is_ios:
            tags = [t for t in ('DNS', 'DNS_SERVER', 'DNSSERVER') if dev.engine.find(t) is not None]
            tag, element, kind = tags[0] if len(tags) == 1 else 'DNS', dev.engine, 'ipv4'
        elif key in ('power', 'portstatus'):
            tag, element, kind = 'POWER', port.el if port is not None else dev.engine, 'bool'
        else:
            report['reason'] = 'This requirement needs a type-specific or runtime evaluator.'
            continue
        # A path under Ports that did not resolve must not modify the device.
        interface_scope = scope_names.intersection({'ports', 'interfaces'}) or any(
            re.match(r'^(?:ethernet|fastethernet|gigabitethernet|serial|loopback|vlan|'
                     r'tunnel|portchannel|dot11radio)\d', segment) for segment in scope_names)
        if port is None and interface_scope:
            report['reason'] = 'The assessment interface could not be matched.'
            continue
        try:
            if kind == 'ipv4':
                expected = str(ipaddress.IPv4Address(expected))
            elif kind == 'mask':
                expected = str(ipaddress.IPv4Network('0.0.0.0/' + expected).netmask)
            elif kind == 'bool':
                expected = 'true' if _boolean(expected) else 'false'
            elif kind == 'hostname':
                if not expected or any(c.isspace() for c in expected):
                    raise ValueError('hostname must be a non-empty single word')
            child = element.find(tag)
            if child is None:
                report['reason'] = f'No supported {tag} field exists on the matched device.'
                continue
            current = pt.doc.from_tree(child.text) or ''
            config_actual = config_value(dev, port, tag)
            changed = current != expected or (config_actual is not None and config_actual != expected)
            if changed and apply:
                if kind == 'hostname' and dev.is_ios:
                    ops.apply_snippet(dev, 'hostname ' + expected)
                elif port is not None and tag in ('IP', 'SUBNET') and dev.is_ios:
                    cfg = IosConfig(dev.running_config)
                    state = cfg.interface(port.name)
                    ip = expected if tag == 'IP' else (peer_value(item, {'ip', 'ipaddress', 'ipv4address'})
                                                      or (state.ip if state else None) or port.ip)
                    mask = expected if tag == 'SUBNET' else (peer_value(item, {'subnet', 'subnetmask', 'mask'})
                                                            or (state.mask if state else None) or port.subnet)
                    if not ip or not mask:
                        report['reason'] = 'An address and subnet mask are both needed to update this IOS interface.'
                        continue
                    ops.set_ip(dev, port.name, ip, mask)
                elif port is not None and tag == 'POWER' and dev.is_ios:
                    ops.apply_snippet(dev, f'interface {port.name}\n ' +
                                      ('no shutdown' if expected == 'true' else 'shutdown'))
                else:
                    child.text = pt.doc.to_tree(expected)
                    if port is not None and tag in ('IP', 'SUBNET'):
                        dhcp = port.el.find('PORT_DHCP_ENABLE')
                        if dhcp is not None:
                            dhcp.text = 'false'
            # Re-read after *all* changes: contradictory checks stay unresolved.
            resolved.append((report, dev, port, element, tag, expected, changed))
        except (ValueError, KeyError) as exc:
            report['reason'] = f'Invalid or unsupported expected value: {exc}'
    for report, dev, port, element, tag, expected, changed in resolved:
        actual = pt.doc.from_tree(element.findtext(tag)) or ''
        cli_actual = config_value(dev, port, tag)
        if actual == expected and (cli_actual is None or cli_actual == expected):
            report.update(status='matched_locally', actual=actual, changed=changed and apply)
        else:
            report.update(actual=actual, reason='Stored state does not match this requirement.')
    return findings
