#!/usr/bin/env python3
"""Focused build-delta regression tests, requiring only Python and GNU tools.

Real: input xz indexing/decompression, GNU tar/xz output, archive restore,
content hashes, metadata fingerprint, hardlinks, symlinks and user xattrs.
Mocked: btrfs/loop mounts, disk capacity/allocation and rsync (the fake batch is
itself a tar archive, NOT a real rsync wire-format compatibility test).
No root, installed packages, network, or multi-GiB allocations are required.
Run: python3 -m unittest discover -s tests -p 'test_build_delta.py' -v
"""
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "build-delta.sh"
GIB = 1024**3
REAL_RSYNC = shutil.which("rsync")
SHIM = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
p = pathlib.Path
name = p(sys.argv[0]).name
args = sys.argv[1:]
root = p(os.environ['DELTA_TEST_ROOT'])
fail = os.environ.get('DELTA_TEST_FAIL', '')
with (root / 'events').open('a') as f:
    f.write(json.dumps([name, *args]) + '\n')
mount_state = root / 'mounted.json'
if name == 'mktemp':
    count_file = root / 'mktemp-count'
    count = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(count))
    if fail == 'mktemp-' + str(count): sys.exit(1)
    args = [a.replace('/tmp/delta-', str(root / 'delta-')) for a in args]
    result = subprocess.run(['/usr/bin/mktemp', *args], capture_output=True, text=True)
    if result.returncode == 0:
        with (root / 'temp-paths').open('a') as f: f.write(result.stdout)
    print(result.stdout, end='')
    sys.exit(result.returncode)
elif name == 'df':
    count_file = root / 'df-count'
    count = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(count))
    low_at = int(os.environ.get('DELTA_TEST_LOW_DF_AT', '0'))
    available = 1024 if count == low_at else 120 * 1024**3
    print('Avail\n' + str(available))
elif name == 'stat' and args[:2] == ['-c', '%d']:
    print(2 if os.environ.get('DELTA_TEST_SPLIT_FS') and p(args[2]).name == 'output' else 1)
elif name == 'fallocate':
    if fail == name: sys.exit(1)
    p(args[-1]).write_bytes(b'fake loop backing')
elif name == 'mkfs.btrfs':
    if fail == name: sys.exit(1)
elif name == 'mount':
    if fail == name: sys.exit(1)
    mount_state.write_text(json.dumps({'image': args[-2], 'dir': args[-1]}))
elif name == 'mountpoint':
    if fail == name: sys.exit(1)
    sys.exit(0 if mount_state.exists() and json.loads(mount_state.read_text())['dir'] == args[-1] else 1)
elif name == 'umount':
    if fail == name: sys.exit(1)
    if fail == 'umount-once' and not (root / 'umount-failed').exists():
        (root / 'umount-failed').touch()
        sys.exit(1)
    state = json.loads(mount_state.read_text())
    for child in p(state['dir']).iterdir():
        if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
        else: child.unlink()
    mount_state.unlink()
elif name == 'btrfs':
    if fail == name: sys.exit(1)
    if args[0] == 'receive':
        sys.exit(subprocess.call(['/usr/bin/tar', '--xattrs', '--acls', '-xf', '-', '-C', args[-1]]))
elif name == 'rsync':
    if fail == name: sys.exit(23)
    batch = next((x.split('=', 1)[1] for x in args if x.startswith('--write-batch=')), None)
    if batch:
        subprocess.run(['/usr/bin/tar', '--xattrs', '--acls', '-cf', batch, '-C', args[-2], '.'], check=True)
        p(batch + '.sh').write_text('mock rsync helper\n')
    elif '--dry-run' in args and os.environ.get('DELTA_TEST_NO_CHANGES') != '1':
        if os.environ.get('DELTA_TEST_ATTRS_ONLY') == '1':
            print('.f...p..... permissions')
            sys.exit(0)
        print('*deleting   obsolete')
        print('>f.st...... changed')
        print('>f+++++++++ added')
        print('cL+++++++++ link -> added')
        print('hf+++++++++ hardlink => added')
        print('.f...p..... permissions')
elif name == 'tar' and args[:2] == ['cf', '-']:
    if fail == name:
        sys.stdout.buffer.write(b'incomplete tar')
        sys.exit(2)
    if any('delta.tar' in a for a in args): raise AssertionError('uncompressed delta.tar')
    if os.environ.get('DELTA_TEST_FORMAT') == 'rsync-batch' and mount_state.exists():
        raise AssertionError('batch tar started before unmount')
    if os.environ.get('DELTA_TEST_FORMAT') == 'tar' and not mount_state.exists():
        raise AssertionError('target unmounted before tar read')
    os.execv('/usr/bin/tar', ['tar', *args])
elif name == 'xz' and '-7' in args:
    if fail == name:
        sys.stdin.buffer.read()
        sys.stdout.buffer.write(b'incomplete xz')
        sys.exit(1)
    os.execv('/usr/bin/xz', ['xz', *args])
else:
    os.execv('/usr/bin/' + name, [name, *args])
'''


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def fingerprint(path):
    return run('bash', '-o', 'pipefail', '-c',
               "find . -not -path './proc/*' -not -path './sys/*' -not -path './dev/*' "
               "-not -path './tmp/*' -not -path './run/*' -not -type s "
               "-printf '%P\\t%s\\t%m\\t%U\\t%G\\t%y\\n' | LC_ALL=C sort | sha256sum",
               cwd=path).stdout.decode().split()[0]


class BuildDeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='build-delta-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        shim = self.bin / 'shim'
        shim.write_text(SHIM)
        shim.chmod(0o755)
        for tool in ('mktemp', 'df', 'stat', 'fallocate', 'mkfs.btrfs', 'mount',
                     'mountpoint', 'umount', 'btrfs', 'rsync', 'tar', 'xz'):
            (self.bin / tool).symlink_to(shim)
        self.target_name = 'skorionos-51_abcd-test'
        self.base_name = 'skorionos-50_1234-test'
        fixtures = self.root / 'fixtures'
        fixtures.mkdir()
        self.target = fixtures / self.target_name
        self.base = fixtures / self.base_name
        self.target.mkdir()
        self.base.mkdir()
        unchanged = random.Random(17).randbytes(128 * 1024)
        for path in (self.target, self.base):
            (path / 'unchanged').write_bytes(unchanged)
            (path / 'permissions').write_text('same bytes\n')
            (path / 'changed').write_text('old\n')
        (self.base / 'obsolete').write_text('remove me\n')
        (self.target / 'changed').write_text('new contents\n')
        (self.target / 'added').write_text('added contents\n')
        (self.target / 'permissions').chmod(0o751)
        (self.target / 'link').symlink_to('added')
        os.link(self.target / 'added', self.target / 'hardlink')
        os.setxattr(self.target / 'added', 'user.delta-test', b'preserve me')
        self.images = []
        for name in (self.target_name, self.base_name):
            image = self.root / (name + '.skosys')
            # These are xz-compressed tar fixtures, not btrfs send streams.
            tar_bytes = run('/usr/bin/tar', '--xattrs', '--acls', '-cf', '-', '-C', str(fixtures), name).stdout
            image.write_bytes(run('/usr/bin/xz', '-c', input=tar_bytes).stdout)
            self.images.append(image)
        self.image_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.images]
        self.output = self.root / 'output'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], DELTA_TEST_ROOT=str(self.root))

    def generate(self, fmt='rsync-batch', fail='', ratio=None, auto=False, **env):
        args = ['bash', str(SCRIPT), '--target-img', self.images[0].name,
                '--base-img', self.images[1].name, '--output-dir', 'output', '--delta-format', fmt]
        if not auto:
            args += ['--work-size', '32M']
        if ratio is not None:
            args += ['--max-ratio', str(ratio)]
        self.env.update(DELTA_TEST_FAIL=fail, DELTA_TEST_FORMAT=fmt, **env)
        self.result = subprocess.run(args, cwd=self.root, env=self.env, capture_output=True, text=True)
        return self.result

    def assert_inputs_intact(self):
        self.assertEqual(self.image_hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.images])

    def assert_clean(self, preserve_mount=False):
        paths = (self.root / 'temp-paths').read_text().splitlines() if (self.root / 'temp-paths').exists() else []
        for path in paths:
            if preserve_mount and (Path(path).name.startswith('delta-work-') or Path(path).name.startswith('delta-img-')):
                self.assertTrue(Path(path).exists(), path)
            else:
                self.assertFalse(Path(path).exists(), path)
        self.assertFalse((self.output / 'delta.tar').exists())
        self.assert_inputs_intact()

    def assert_failed(self, preserve_mount=False):
        self.assertNotEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)
        self.assertEqual(list(self.output.iterdir()), [], self.result.stdout + self.result.stderr)
        self.assert_clean(preserve_mount)

    def unpack(self):
        artifact = next(self.output.glob('*.skdelta'))
        run('/usr/bin/xz', '-t', str(artifact))
        self.unpack_dir = self.root / 'unpacked'
        self.unpack_dir.mkdir()
        tar_bytes = run('/usr/bin/xz', '-dc', str(artifact)).stdout
        run('/usr/bin/tar', '--xattrs', '--acls', '--numeric-owner', '-xf', '-', '-C', str(self.unpack_dir), input=tar_bytes)
        entry = json.loads((self.output / 'delta-entry.json').read_text())
        self.assertEqual(entry['checksum'], 'sha256:' + hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(entry['size'], artifact.stat().st_size)
        self.assertEqual(entry['full_size'], self.images[0].stat().st_size)
        self.assertEqual(entry['target_meta_hash'], fingerprint(self.target))
        self.assertEqual(json.loads((self.unpack_dir / '.delta-meta.json').read_text())['target_meta_hash'], entry['target_meta_hash'])
        self.assertEqual((self.output / 'delta-status.txt').read_text(), 'OK\n')
        self.assertEqual((self.output / 'delta-sha256sum.txt').read_text(), entry['checksum'][7:] + '  ' + artifact.name + '\n')
        return entry

    def assert_restored(self, restored):
        self.assertEqual(fingerprint(restored), fingerprint(self.target))
        for path in self.target.iterdir():
            dest = restored / path.name
            if path.is_symlink(): self.assertEqual(os.readlink(path), os.readlink(dest))
            elif path.is_file():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(dest.read_bytes()).hexdigest())
        self.assertEqual(os.stat(restored / 'added').st_ino, os.stat(restored / 'hardlink').st_ino)
        self.assertEqual(os.getxattr(restored / 'added', 'user.delta-test'), b'preserve me')

    def test_batch_pipeline_real_archive_mock_rsync_roundtrip(self):
        self.assertEqual(self.generate(ratio=1000).returncode, 0, self.result.stderr)
        entry = self.unpack()
        self.assertEqual(entry['format'], 'rsync-batch')
        self.assertEqual(sorted(p.name for p in self.unpack_dir.iterdir()), ['.delta-filelist', '.delta-meta.json', 'batch'])
        restored = self.root / 'restored'
        restored.mkdir()
        # Mock rsync batch replay. This checks lifecycle/archive wrapping, NOT rsync.
        run('/usr/bin/tar', '--xattrs', '--acls', '-xf', str(self.unpack_dir / 'batch'), '-C', str(restored))
        self.assert_restored(restored)
        self.assert_clean()

    @unittest.skipUnless(REAL_RSYNC, "rsync is not installed; real batch replay needs rsync")
    def test_real_rsync_batch_roundtrip_when_available(self):
        # Keep only the btrfs/loop-capacity simulation; use the installed rsync
        # for both producer and deployment flags, without installing anything.
        (self.bin / 'rsync').unlink()
        self.assertEqual(self.generate(ratio=1000).returncode, 0, self.result.stderr)
        self.unpack()
        restored = self.root / 'restored'
        shutil.copytree(self.base, restored, symlinks=True)
        run(REAL_RSYNC, '-aAXH', '--numeric-ids', '--no-inc-recursive', '--delete',
            '--read-batch=' + str(self.unpack_dir / 'batch'), str(restored) + '/')
        self.assert_restored(restored)
        self.assert_clean()

    def test_tar_real_archive_roundtrip_default_ratio(self):
        self.assertEqual(self.generate(fmt='tar').returncode, 0, self.result.stderr)
        entry = self.unpack()
        self.assertEqual(entry['format'], 'tar')
        restored = self.root / 'restored'
        shutil.copytree(self.base, restored, symlinks=True)
        for deletion in (self.unpack_dir / '.delta-deletions').read_text().splitlines():
            (restored / deletion).unlink()
        tar_bytes = run('/usr/bin/xz', '-dc', str(next(self.output.glob('*.skdelta')))).stdout
        run('/usr/bin/tar', '--xattrs', '--acls', '--numeric-owner', '-xf', '-', '-C', str(restored), input=tar_bytes)
        for line in (restored / '.delta-attrs').read_text().splitlines():
            name, mode, uid, gid = line.split('\t')
            (restored / name).chmod(int(mode, 8))
            self.assertEqual((int(uid), int(gid)), (os.getuid(), os.getgid()))
        for control in restored.glob('.delta-*'): control.unlink()
        self.assert_restored(restored)
        self.assert_clean()

    def test_default_seventy_percent_skip(self):
        self.assertEqual(self.generate().returncode, 0, self.result.stderr)
        self.assertEqual([p.name for p in self.output.iterdir()], ['delta-status.txt'])
        self.assertEqual((self.output / 'delta-status.txt').read_text(), 'SKIP\n')
        self.assert_clean()

    def test_tar_no_changes_skip(self):
        self.assertEqual(self.generate(fmt='tar', DELTA_TEST_NO_CHANGES='1').returncode, 0, self.result.stderr)
        self.assertEqual((self.output / 'delta-status.txt').read_text(), 'SKIP\n')
        self.assert_clean()

    def test_tar_attributes_only_empty_file_list(self):
        self.assertEqual(self.generate(fmt='tar', DELTA_TEST_ATTRS_ONLY='1').returncode, 0, self.result.stderr)
        self.unpack()
        self.assertEqual(sorted(p.name for p in self.unpack_dir.iterdir()),
                         ['.delta-attrs', '.delta-deletions', '.delta-filelist', '.delta-meta.json'])
        self.assertEqual((self.unpack_dir / '.delta-deletions').read_text(), '')
        self.assertIn('permissions\t751\t', (self.unpack_dir / '.delta-attrs').read_text())
        self.assert_clean()

    def test_cleanup_retries_unmount_without_publishing(self):
        self.generate(fail='umount-once', ratio=1000)
        self.assert_failed()
        self.assertFalse((self.root / 'mounted.json').exists())

    def test_mountpoint_false_negative_never_removes_work_tree(self):
        self.generate(fail='mountpoint', ratio=1000)
        self.assert_failed(preserve_mount=True)
        state = json.loads((self.root / 'mounted.json').read_text())
        self.assertEqual((Path(state['dir']) / self.target_name / 'changed').read_text(), 'new contents\n')

    def test_failures_cleanup_and_remove_stale_success(self):
        for fmt in ('rsync-batch', 'tar'):
            for fail in ('rsync', 'tar', 'xz'):
                with self.subTest(fmt=fmt, fail=fail):
                    self.output.mkdir(exist_ok=True)
                    for name in ('delta-status.txt', 'delta-entry.json', 'delta-sha256sum.txt',
                                 self.target_name + '.from_50_1234.skdelta'):
                        (self.output / name).write_text('stale success')
                    self.generate(fmt=fmt, fail=fail, ratio=1000)
                    self.assert_failed()

    def test_unmount_failure_preserves_backing_and_contents(self):
        for fmt in ('rsync-batch', 'tar'):
            with self.subTest(fmt=fmt):
                # Each failure keeps its own work resources; remove after asserting.
                self.generate(fmt=fmt, fail='umount', ratio=1000)
                self.assert_failed(preserve_mount=True)
                state = json.loads((self.root / 'mounted.json').read_text())
                self.assertEqual((Path(state['dir']) / self.target_name / 'changed').read_text(), 'new contents\n')
                self.assertTrue(Path(state['image']).exists())
                shutil.rmtree(state['dir'])
                Path(state['image']).unlink()
                (self.root / 'mounted.json').unlink()
                (self.root / 'temp-paths').unlink()

    def test_early_failure_cleanup(self):
        for fail in ('mktemp-1', 'mktemp-2', 'mktemp-3', 'fallocate', 'mkfs.btrfs', 'mount', 'btrfs'):
            with self.subTest(fail=fail):
                (self.root / 'mktemp-count').unlink(missing_ok=True)
                self.generate(fail=fail)
                self.assert_failed()

    def test_budget_rejects_initial_postallocate_postrestore_postbatch(self):
        # Explicit work-size: output checks before allocation, after, after restore,
        # and after batch release. Each rejection leaves no success/partial artifacts.
        for check in range(1, 5):
            with self.subTest(check=check):
                (self.root / 'df-count').unlink(missing_ok=True)
                self.generate(ratio=1000, DELTA_TEST_LOW_DF_AT=str(check))
                self.assert_failed()
                self.assertIn('insufficient output disk space', self.result.stderr)

    def test_auto_budget_same_and_split_filesystems(self):
        raw_size = int(run('/usr/bin/xz', '--robot', '--list', str(self.images[0])).stdout.decode().split('totals\t')[1].split('\t')[3])
        for split in (False, True):
            with self.subTest(split=split):
                (self.root / 'events').unlink(missing_ok=True)
                self.assertEqual(self.generate(auto=True, ratio=1000, DELTA_TEST_SPLIT_FS='1' if split else '').returncode, 0, self.result.stderr)
                reserve = 5 * GIB if split else raw_size + raw_size // 10 + 5 * GIB
                expected = str((120 * GIB - reserve) // GIB) + 'G'
                allocations = [json.loads(line) for line in (self.root / 'events').read_text().splitlines() if json.loads(line)[0] == 'fallocate']
                self.assertEqual(allocations[0][2], expected)
                self.assert_clean()

    def test_auto_insufficient_work_budget_cleanup(self):
        self.generate(auto=True, DELTA_TEST_LOW_DF_AT='2')
        self.assert_failed()
        self.assertIn('insufficient disk space for a 15GiB work filesystem', self.result.stderr)


if __name__ == '__main__':
    unittest.main()
