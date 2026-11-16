import configparser
import os
import subprocess
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(REPO_ROOT, "rootfs", "usr", "lib", "frzr.d", "dns-resolver.migration")
NM_CONF = os.path.join(REPO_ROOT, "rootfs", "etc", "NetworkManager", "conf.d", "20-systemd-resolved.conf")
ALL_INSTALL = os.path.join(REPO_ROOT, "all-install.sh")
MANIFEST = os.path.join(REPO_ROOT, "manifest")
HELPER = os.path.join(REPO_ROOT, "rootfs", "usr", "lib", "skorionos", "dns-resolver-setup")
UNIT = os.path.join(REPO_ROOT, "rootfs", "usr", "lib", "systemd", "system", "skorion-dns-resolver.service")
RESOLV_TARGET = "/usr/lib/systemd/resolv.conf"
BOOT_UNIT = "/usr/lib/systemd/system/skorion-dns-resolver.service"


def run_migration(mount_root):
	return subprocess.run(
		["bash", "-c", 'source "$1"; post_install "$2"', "mig", MIGRATION, mount_root],
		capture_output=True,
		text=True,
	)


def run_helper(configure_args, path_with_fake_systemctl=None):
	script = textwrap.dedent(
		'''
		if [ -n "$2" ]; then export PATH="$2:$PATH"; fi
		source "$1"
		configure_resolver "$3" "$4"
		'''
	)
	env = dict(os.environ)
	return subprocess.run(
		["bash", "-c", script, "helper", HELPER, path_with_fake_systemctl or "", configure_args[0], configure_args[1]],
		capture_output=True,
		text=True,
		env=env,
	)


class FakeSystemctl:
	def __init__(self, base, enabled=True, active=True, reenable_failure=False):
		self.dir = os.path.join(base, "bin")
		os.makedirs(self.dir, exist_ok=True)
		self.enabled = enabled
		self.active = active
		self.log = os.path.join(base, "systemctl.log")
		path = os.path.join(self.dir, "systemctl")
		with open(path, "w") as f:
			f.write("#!/bin/bash\n")
			f.write(f"printf '%s\\n' \"$*\" >> {self.log!r}\n")
			f.write(
				"verb=; for a in \"$@\"; do case \"$a\" in is-enabled|is-active|reenable|disable) verb=\"$a\";; esac; done\n"
			)
			f.write(f"if [ \"$verb\" = is-enabled ]; then exit {'0' if enabled else '1'}; fi\n")
			f.write(f"if [ \"$verb\" = is-active ]; then exit {'0' if active else '1'}; fi\n")
			f.write(f"if [ \"$verb\" = reenable ]; then exit {'1' if reenable_failure else '0'}; fi\n")
			f.write("if [ \"$verb\" = disable ]; then exit 0; fi\n")
			f.write("exit 1\n")
		os.chmod(path, 0o755)

	def calls(self):
		if not os.path.exists(self.log):
			return []
		with open(self.log) as f:
			return [line.rstrip("\n") for line in f if line.strip()]


class MigrationTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.mount_root = os.path.join(self.tmp.name, "frzr_root")
		self.etc = os.path.join(self.mount_root, "etc")
		os.makedirs(self.etc)

	def resolv(self):
		return os.path.join(self.etc, "resolv.conf")

	def unit_link(self):
		return os.path.join(self.etc, "systemd", "system", "multi-user.target.wants", "skorion-dns-resolver.service")

	def assert_boot_unit_linked(self):
		link = self.unit_link()
		self.assertTrue(os.path.islink(link), link)
		self.assertEqual(os.readlink(link), BOOT_UNIT)

	def assert_no_dns_mutation(self, path):
		self.assertFalse(os.path.exists(path + ".skorionos-backup"))

	def test_registers_boot_unit_without_dns_mutation_for_regular_file(self):
		with open(self.resolv(), "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assert_boot_unit_linked()
		self.assertFalse(os.path.islink(self.resolv()))

	def test_registers_boot_unit_without_dns_mutation_for_symlink(self):
		os.symlink("/var/run/NetworkManager/resolv.conf", self.resolv())
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assert_boot_unit_linked()
		self.assertEqual(os.readlink(self.resolv()), "/var/run/NetworkManager/resolv.conf")

	def test_idempotent(self):
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		first_inode = os.lstat(self.unit_link()).st_ino
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assert_boot_unit_linked()
		self.assertEqual(os.lstat(self.unit_link()).st_ino, first_inode)
		self.assert_no_dns_mutation(self.resolv())

	def test_missing_mountroot_rejected(self):
		missing = os.path.join(self.tmp.name, "does-not-exist")
		result = run_migration(missing)
		self.assertNotEqual(result.returncode, 0)

	def test_empty_arg_rejected(self):
		result = run_migration("")
		self.assertNotEqual(result.returncode, 0)

	def test_missing_etc_rejected(self):
		bare = os.path.join(self.tmp.name, "bare")
		os.makedirs(bare)
		result = run_migration(bare)
		self.assertNotEqual(result.returncode, 0)


class LiveResolverPreservationTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.mount_root = os.path.join(self.tmp.name, "frzr_root")
		self.etc = os.path.join(self.mount_root, "etc")
		os.makedirs(self.etc)

	def resolv(self):
		return os.path.join(self.etc, "resolv.conf")

	def test_regular_file_untouched(self):
		path = self.resolv()
		original = b"nameserver 192.0.2.1\nsearch example.test\n"
		with open(path, "wb") as f:
			f.write(original)
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertFalse(os.path.islink(path))
		with open(path, "rb") as f:
			self.assertEqual(f.read(), original)

	def test_existing_symlink_untouched(self):
		path = self.resolv()
		target = "/var/run/NetworkManager/resolv.conf"
		os.symlink(target, path)
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(os.path.islink(path))
		self.assertEqual(os.readlink(path), target)


class HelperTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.resolver = os.path.join(self.tmp.name, "resolv.conf")
		self.target = os.path.join(self.tmp.name, "resolv-target.conf")
		with open(self.target, "w") as f:
			f.write("nameserver 192.0.2.53\n")

	def fake(self, enabled=True, active=True, reenable_failure=False):
		return FakeSystemctl(self.tmp.name, enabled=enabled, active=active, reenable_failure=reenable_failure).dir

	def backup(self):
		return self.resolver + ".skorionos-backup"

	def test_disabled_preserves_original(self):
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_helper((self.resolver, self.target), self.fake(enabled=False, active=True))
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertFalse(os.path.islink(self.resolver))
		self.assertFalse(os.path.exists(self.backup()))

	def test_inactive_preserves_original(self):
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_helper((self.resolver, self.target), self.fake(enabled=True, active=False))
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertFalse(os.path.islink(self.resolver))
		self.assertFalse(os.path.exists(self.backup()))

	def test_unavailable_target_preserves_original(self):
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_helper((self.resolver, os.path.join(self.tmp.name, "missing")), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertFalse(os.path.islink(self.resolver))
		self.assertFalse(os.path.exists(self.backup()))

	def test_switches_after_saving_file(self):
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(os.path.islink(self.resolver))
		self.assertEqual(os.readlink(self.resolver), self.target)
		self.assertTrue(os.path.isfile(self.backup()))
		with open(self.backup()) as f:
			self.assertEqual(f.read(), "nameserver 192.0.2.1\n")

	def test_switches_after_saving_dangling_link(self):
		os.symlink("/nonexistent/dangling.target", self.resolver)
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(os.path.islink(self.resolver))
		self.assertEqual(os.readlink(self.resolver), self.target)
		self.assertTrue(os.path.islink(self.backup()))
		self.assertEqual(os.readlink(self.backup()), "/nonexistent/dangling.target")

	def test_existing_backup_not_overwritten(self):
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		with open(self.backup(), "w") as f:
			f.write("pre-existing-backup\n")
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(os.path.islink(self.resolver))
		with open(self.backup()) as f:
			self.assertEqual(f.read(), "pre-existing-backup\n")

	def test_repeated_run_keeps_inode(self):
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		inode = os.lstat(self.resolver).st_ino
		backup_inode = os.lstat(self.backup()).st_ino
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(os.lstat(self.resolver).st_ino, inode)
		self.assertEqual(os.lstat(self.backup()).st_ino, backup_inode)

	def test_backup_failure_does_not_switch(self):
		original = b"nameserver 192.0.2.1\n"
		with open(self.resolver, "wb") as f:
			f.write(original)
		fake_dir = self.fake()
		cp = os.path.join(fake_dir, "cp")
		with open(cp, "w") as f:
			f.write("#!/bin/bash\nexit 1\n")
		os.chmod(cp, 0o755)
		result = run_helper((self.resolver, self.target), fake_dir)
		self.assertNotEqual(result.returncode, 0)
		self.assertFalse(os.path.islink(self.resolver))
		with open(self.resolver, "rb") as f:
			self.assertEqual(f.read(), original)
		self.assertFalse(os.path.exists(self.backup()))

	def test_directory_rejected(self):
		os.makedirs(self.resolver)
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertNotEqual(result.returncode, 0)
		self.assertTrue(os.path.isdir(self.resolver))
		self.assertFalse(os.path.exists(self.backup()))

	def _make_config_files(self):
		nm_conf = os.path.join(self.tmp.name, "NetworkManager", "conf.d", "20-systemd-resolved.conf")
		resolved_conf = os.path.join(self.tmp.name, "systemd", "resolved.conf.d", "20-skorionos.conf")
		os.makedirs(os.path.dirname(nm_conf))
		os.makedirs(os.path.dirname(resolved_conf))
		nm_bytes = b"[main]\ndns=systemd-resolved\n"
		resolved_bytes = b"[Resolve]\nDNS=192.0.2.53\n"
		old = 1000000000
		for path, data in ((nm_conf, nm_bytes), (resolved_conf, resolved_bytes)):
			with open(path, "wb") as f:
				f.write(data)
			os.utime(path, (old, old))
		return nm_conf, resolved_conf, nm_bytes, resolved_bytes, old

	def test_already_correct_link_reenables_then_disables(self):
		os.symlink(self.target, self.resolver)
		inode = os.lstat(self.resolver).st_ino
		fake = FakeSystemctl(self.tmp.name, enabled=True, active=True)
		result = run_helper((self.resolver, self.target), fake.dir)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertTrue(os.path.islink(self.resolver))
		self.assertEqual(os.readlink(self.resolver), self.target)
		self.assertEqual(os.lstat(self.resolver).st_ino, inode)
		argv = [c.split() for c in fake.calls()]
		verbs = [a[0] for a in argv]
		self.assertIn("reenable", verbs)
		idx_reenable = verbs.index("reenable")
		idx_disable = verbs.index("disable")
		self.assertGreater(idx_disable, idx_reenable)
		self.assertEqual(
			argv[idx_reenable],
			["reenable", "--no-reload", "systemd-resolved.service"],
		)
		self.assertEqual(
			argv[idx_disable],
			["disable", "--no-reload", "skorion-dns-resolver.service"],
		)
		self.assertFalse(os.path.exists(self.backup()))

	def test_reenable_failure_preserves_original(self):
		original = b"nameserver 192.0.2.1\n"
		with open(self.resolver, "wb") as f:
			f.write(original)
		fake = FakeSystemctl(self.tmp.name, enabled=True, active=True, reenable_failure=True)
		result = run_helper((self.resolver, self.target), fake.dir)
		self.assertNotEqual(result.returncode, 0)
		self.assertFalse(os.path.islink(self.resolver))
		with open(self.resolver, "rb") as f:
			self.assertEqual(f.read(), original)
		self.assertFalse(os.path.exists(self.backup()))
		verbs = [c.split()[0] for c in fake.calls()]
		self.assertNotIn("disable", verbs)

	def test_config_files_copied_up_on_success(self):
		nm_conf, resolved_conf, nm_bytes, resolved_bytes, old = self._make_config_files()
		with open(self.resolver, "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_helper((self.resolver, self.target), self.fake())
		self.assertEqual(result.returncode, 0, result.stderr)
		for path, expected in ((nm_conf, nm_bytes), (resolved_conf, resolved_bytes)):
			with open(path, "rb") as f:
				self.assertEqual(f.read(), expected)
			self.assertGreater(os.stat(path).st_mtime, old)

	def test_touch_failure_leaves_resolver_and_unit(self):
		original = b"nameserver 192.0.2.1\n"
		with open(self.resolver, "wb") as f:
			f.write(original)
		self._make_config_files()
		fake = FakeSystemctl(self.tmp.name, enabled=True, active=True)
		touch = os.path.join(fake.dir, "touch")
		with open(touch, "w") as f:
			f.write("#!/bin/bash\nexit 1\n")
		os.chmod(touch, 0o755)
		result = run_helper((self.resolver, self.target), fake.dir)
		self.assertNotEqual(result.returncode, 0)
		self.assertFalse(os.path.islink(self.resolver))
		with open(self.resolver, "rb") as f:
			self.assertEqual(f.read(), original)
		self.assertFalse(os.path.exists(self.backup()))
		verbs = [c.split()[0] for c in fake.calls()]
		self.assertNotIn("disable", verbs)


class OrderingAndConfigTests(unittest.TestCase):
	def test_fresh_link_after_download(self):
		with open(ALL_INSTALL, "r", encoding="utf-8") as f:
			text = f.read()
		download = text.find("postinstall_download")
		link = text.find("ln -sfnT /usr/lib/systemd/resolv.conf /etc/resolv.conf")
		self.assertNotEqual(download, -1)
		self.assertNotEqual(link, -1)
		self.assertGreater(link, download)

	def test_manifest_includes_resolver_boot_unit(self):
		with open(MANIFEST, "r", encoding="utf-8") as f:
			text = f.read()
		self.assertIn("skorion-dns-resolver", text)
		self.assertIn("systemd-resolved", text)

	def test_nm_config(self):
		parser = configparser.ConfigParser()
		parser.read(NM_CONF)
		self.assertEqual(parser.get("main", "dns"), "systemd-resolved")
		self.assertEqual(parser.get("main", "rc-manager"), "symlink")

	def test_unit_ordering(self):
		parser = configparser.ConfigParser(strict=False)
		parser.optionxform = str
		parser.read(UNIT)
		after = parser.get("Unit", "After").split()
		before = parser.get("Unit", "Before").split()
		self.assertIn("systemd-resolved.service", after)
		self.assertIn("systemd-user-sessions.service", before)
		self.assertIn("NetworkManager.service", before)
		self.assertEqual(parser.get("Service", "Type"), "oneshot")
		self.assertEqual(parser.get("Service", "ExecStart"), "/usr/lib/skorionos/dns-resolver-setup")
		self.assertEqual(parser.get("Install", "WantedBy"), "multi-user.target")


if __name__ == "__main__":
	unittest.main()
