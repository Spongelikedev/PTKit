"""Distribution boundaries must exclude real activity data and workstation paths."""
import json
from pathlib import Path
import zipfile

from tools.package_release import package
from tools.privacy_audit import audit_source, scan_blob
from tools.release_files import ROOT_FILES, source_files
from ptkit import PTFile, codec, xmlbundle
from ptkit.solver import write_solution


def source_tree(tmp_path):
    project = Path(__file__).resolve().parents[1]
    for name in ROOT_FILES:
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((project / name).read_bytes())
    app = tmp_path / 'src' / 'ptkit'
    app.mkdir(parents=True)
    (app / '__init__.py').write_text('__version__ = "0.2.0"', encoding='utf-8')
    (tmp_path / 'tests').mkdir()
    return tmp_path


def test_private_workspace_outputs_never_enter_source_bundle(tmp_path):
    root = source_tree(tmp_path)
    for name in ['solutions/private.pka', '.venv/pyvenv.cfg', 'build/debug.log',
                 '.test-tmp-private/results.json', 'PROGRESS.md', 'local.env']:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('private-test-operator secret activity data', encoding='utf-8')
    assert audit_source(root, ['private-test-operator'])['passed']
    output = root / 'release'
    package(root, output)
    with zipfile.ZipFile(output / 'PTKit-source.zip') as archive:
        names = archive.namelist()
        assert 'README.md' in names and '.gitignore' in names
        assert archive.read('LICENSE').startswith(b'MIT License')
        assert b'Sponge and contributors' in archive.read('LICENSE')
        assert 'RESPONSIBLE_USE.md' in names
        assert '.github/workflows/windows-build.yml' in names
        assert not any(name.startswith(('solutions/', '.venv/', 'build/', '.test-tmp')) for name in names)
        assert all(b'private-test-operator' not in archive.read(name) for name in names)
        assert all(not info.extra for info in archive.infolist())


def test_audit_checks_unicode_and_compressed_payload_bytes_without_false_substrings():
    token = 'private-test-operator'
    assert scan_blob('code', token.encode('utf-16-le'), [token])
    assert scan_blob('code', token.encode('utf-8'), [token])
    assert not scan_blob('translation', (token + 'suffix').encode(), [token])


def test_absolute_profile_path_is_rejected_even_for_a_different_builder(tmp_path):
    root = source_tree(tmp_path)
    # Assemble a fictitious profile path so the test source itself contains no
    # hardcoded user-profile path in the distributable repository.
    path = 'C:' + chr(92) + 'Users' + chr(92) + 'demo-operator' + chr(92) + 'private'
    (root / 'src/ptkit/private.py').write_text(path, encoding='utf-8')
    report = audit_source(root, [])
    assert not report['passed']
    assert any(f['reason'] == 'absolute user-profile path found' for f in report['findings'])


def test_export_manifest_and_solution_report_do_not_record_computer_paths(tmp_path, pka_xml):
    source = tmp_path / 'personal-folder' / 'activity.pka'
    source.parent.mkdir()
    source.write_bytes(codec.encode(pka_xml))
    pt = PTFile.load(source)
    bundle = tmp_path / 'bundle'
    xmlbundle.export_bundle(pt, bundle)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    assert manifest['source'] == 'activity.pka'
    assert str(source.parent) not in (bundle / 'manifest.json').read_text()
    result = write_solution(pt, tmp_path / 'solution.pka')
    report = json.loads(result.report_path.read_text(encoding='utf-8'))
    assert report['source_file'] == 'activity.pka'
    assert report['output_file'] == 'solution.pka'
    assert str(source.parent) not in result.report_path.read_text(encoding='utf-8')
