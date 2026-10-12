#!/usr/bin/env python3
"""Offline image policy tests. Overlay lookup is modeled, NOT a kernel mount test.

Real: Bash manifest evaluation and legacy hook execution with a mock mount.
The active systemd-initramfs overlay is checked in the generated fstab source.
No package installation, network daemon, privileged mount, or host /etc changes.
Run: python3 -m unittest discover -s tests -p 'test_iwd_image.py' -v
"""
import configparser
import fnmatch
from pathlib import Path
import re
import shlex
import subprocess
import unittest

REPO = Path(__file__).resolve().parents[1]
DEFAULT = REPO / 'rootfs/etc/iwd/main.conf'
HOOK = REPO / 'rootfs/etc/initcpio/hooks/frzr-etc'


def manifest_values(variant):
    names = ('PACKAGES', 'SUB_PACKAGES', 'PACKAGES_TO_DELETE',
             'SUB_PACKAGES_TO_DELETE', 'SERVICES', 'FILES_TO_DELETE', 'VERSION')
    # Variant files source base-* in their current directory, as the builder does.
    script = 'source "$1"; source "$2"; '
    script += '; '.join('printf "%s\\0" "${' + n + '}"' for n in names)
    output = subprocess.check_output(['bash', '-c', script, 'test',
                                     str(REPO / 'manifest'), str(variant)],
                                    cwd=REPO / 'branch')
    return dict(zip(names, output.decode().split('\0')))


def overlay_lookup(lower, upper, relative, whiteouts=(), opaque=()):
    """Model ONLY upper-file/whiteout/opaque-directory precedence used below."""
    if relative in whiteouts:
        return None
    if relative in upper:
        return upper[relative]
    if any(relative.startswith(directory.rstrip('/') + '/') for directory in opaque):
        return None
    return lower.get(relative)


class IwdImageTests(unittest.TestCase):
    def test_every_active_and_legacy_variant_keeps_both_backends(self):
        variants = sorted((REPO / 'branch').glob('*manifest-*'))
        self.assertEqual(len(variants), 13)
        for variant in variants:
            with self.subTest(variant=variant.name):
                values = manifest_values(variant)
                packages = (values['PACKAGES'] + ' ' + values['SUB_PACKAGES']).split()
                removed = (values['PACKAGES_TO_DELETE'] + ' ' + values['SUB_PACKAGES_TO_DELETE']).split()
                for package in ('iwd', 'wpa_supplicant', 'networkmanager'):
                    self.assertIn(package, packages)
                    self.assertNotIn(package, removed)
                self.assertIn('NetworkManager', values['SERVICES'].split())
                self.assertFalse({'iwd', 'iwd.service', 'wpa_supplicant',
                                  'wpa_supplicant.service'} & set(values['SERVICES'].split()))
                self.assertEqual(values['VERSION'], '56-5')
                for path in ('/etc/iwd/main.conf', '/usr/lib/iwd/iwd',
                             '/usr/lib/systemd/system/iwd.service',
                             '/usr/share/dbus-1/system-services/net.connman.iwd.service',
                             '/usr/share/dbus-1/system.d/iwd-dbus.conf'):
                    for pattern in values['FILES_TO_DELETE'].split():
                        for candidate in (Path(path), *Path(path).parents):
                            self.assertFalse(fnmatch.fnmatch(str(candidate), pattern),
                                             (path, pattern))

    def test_iwd_vendor_link_cannot_replace_predictable_names(self):
        path = '/usr/lib/systemd/network/80-iwd.link'
        values = manifest_values(REPO / 'branch/manifest-kde')
        self.assertIn(path, values['FILES_TO_DELETE'].split())
        config = (REPO / 'rootfs/etc/pacman.conf').read_text()
        exclusions = re.findall(r'^NoExtract\s*=\s*(.*)$', config, re.MULTILINE)
        self.assertIn(path.lstrip('/'), ' '.join(exclusions).split())
        # The builder copies this configuration before all-install invokes pacman.
        builder = (REPO / 'build-image.sh').read_text()
        self.assertIn('pacstrap -K -C rootfs/etc/pacman.conf', builder)
        self.assertIn('rootfs/. ${BUILD_PATH}/', builder)

    def test_active_systemd_initramfs_overlay_uses_persistent_upper(self):
        hooks = subprocess.check_output(
            ['bash', '-c', 'source "$1"; printf "%s\\n" "${HOOKS[@]}"',
             'test', str(REPO / 'rootfs/etc/mkinitcpio.conf')], text=True).split()
        self.assertIn('systemd', hooks)
        self.assertNotIn('frzr-etc', hooks)
        source = (REPO / 'all-install.sh').read_text()
        fstab = re.search(r'echo "\n(LABEL=frzr_efi.*?)" > /etc/fstab', source,
                          re.DOTALL)
        self.assertIsNotNone(fstab)
        line = next(line for line in fstab[1].splitlines() if line.startswith('overlay '))
        fields = line.split()
        self.assertEqual(fields[:3], ['overlay', '/etc', 'overlay'])
        options = fields[3].split(',')
        for option in ('lowerdir=/sysroot/etc', 'upperdir=/sysroot/frzr_root/etc',
                       'workdir=/sysroot/frzr_root/.etc', 'x-initrd.mount'):
            self.assertIn(option, options)
        self.assertNotIn('upperdir=/etc', options)

    def test_default_is_narrow_driver_quirk_only(self):
        config = configparser.ConfigParser()
        config.read(DEFAULT)
        self.assertEqual(config.sections(), ['DriverQuirks'])
        self.assertEqual(dict(config['DriverQuirks']), {'defaultinterface': 'iwlwifi'})
        # No EnableNetworkConfiguration: NetworkManager keeps DHCP/IP ownership.

    def test_legacy_initramfs_hook_targets_new_root_and_persistent_upper(self):
        # mount is a shell function: no command can mount or modify the host.
        script = 'mount() { printf "%s\\n" "$*"; }; source "$1"; run_latehook'
        lines = subprocess.check_output(['bash', '-c', script, 'test', str(HOOK)], text=True).splitlines()
        overlay = next(shlex.split(line) for line in lines if line.startswith('-t overlay '))
        self.assertEqual(overlay[-1], '/new_root/etc')
        options = overlay[overlay.index('-o') + 1].split(',')
        self.assertIn('lowerdir=/new_root/etc', options)
        self.assertIn('upperdir=/new_root/frzr_root/etc', options)
        self.assertIn('workdir=/new_root/frzr_root/.etc', options)
        self.assertNotIn('upperdir=/etc', options)

    def test_upgrade_without_custom_config_exposes_image_default(self):
        lower = {'iwd/main.conf': DEFAULT.read_bytes()}
        self.assertEqual(overlay_lookup(lower, {}, 'iwd/main.conf'), DEFAULT.read_bytes())

    def test_custom_configuration_is_preserved_byte_for_byte(self):
        custom = b'# owner configuration\n[General]\nCountry=DE\n[DriverQuirks]\nDefaultInterface=other_driver\n'
        upper = {'iwd/main.conf': custom}
        self.assertEqual(overlay_lookup({'iwd/main.conf': DEFAULT.read_bytes()}, upper,
                                       'iwd/main.conf'), custom)
        self.assertEqual(upper, {'iwd/main.conf': custom})

    def test_repeated_deployment_does_not_mutate_persistent_config(self):
        upper = {'iwd/main.conf': b'[General]\nCountry=JP\n'}
        before = upper.copy()
        for _ in range(3):
            self.assertEqual(overlay_lookup({'iwd/main.conf': DEFAULT.read_bytes()}, upper,
                                           'iwd/main.conf'), before['iwd/main.conf'])
        self.assertEqual(upper, before)

    def test_persistent_whiteout_hides_image_default(self):
        self.assertIsNone(overlay_lookup({'iwd/main.conf': DEFAULT.read_bytes()}, {},
                                        'iwd/main.conf', whiteouts={'iwd/main.conf'}))

    def test_persistent_opaque_directory_hides_image_default(self):
        self.assertIsNone(overlay_lookup({'iwd/main.conf': DEFAULT.read_bytes()}, {},
                                        'iwd/main.conf', opaque={'iwd'}))

    def test_existing_iwd_backend_selection_is_not_migrated_to_wpa(self):
        backend = b'[device]\nwifi.backend=iwd\n'
        upper = {'NetworkManager/conf.d/wifi_backend.conf': backend}
        self.assertEqual(overlay_lookup({'iwd/main.conf': DEFAULT.read_bytes()}, upper,
                                       'NetworkManager/conf.d/wifi_backend.conf'), backend)
        # This change supplies the requested daemon rather than rewriting the owner preference.
        self.assertFalse((REPO / 'rootfs/usr/lib/frzr.d/iwd.migration').exists())


if __name__ == '__main__':
    unittest.main()
