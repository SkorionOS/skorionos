import configparser
import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION = os.path.join(REPO_ROOT, "rootfs", "usr", "lib", "frzr.d", "dns-resolver.migration")
NM_CONF = os.path.join(REPO_ROOT, "rootfs", "etc", "NetworkManager", "conf.d", "20-systemd-resolved.conf")
ALL_INSTALL = os.path.join(REPO_ROOT, "all-install.sh")
MANIFEST = os.path.join(REPO_ROOT, "manifest")
RESOLV_TARGET = "/usr/lib/systemd/resolv.conf"


def run_migration(mount_root):
	return subprocess.run(
		["bash", "-c", 'source "$1"; post_install "$2"', "mig", MIGRATION, mount_root],
		capture_output=True,
		text=True,
	)


class MigrationTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.mount_root = os.path.join(self.tmp.name, "frzr_root")
		self.etc = os.path.join(self.mount_root, "etc")
		os.makedirs(self.etc)

	def resolv(self):
		return os.path.join(self.etc, "resolv.conf")

	def assert_correct_link(self):
		path = self.resolv()
		self.assertTrue(os.path.islink(path))
		self.assertEqual(os.readlink(path), RESOLV_TARGET)

	def test_replaces_ordinary_file(self):
		with open(self.resolv(), "w") as f:
			f.write("nameserver 192.0.2.1\n")
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assert_correct_link()

	def test_replaces_existing_link(self):
		os.symlink("/var/run/NetworkManager/resolv.conf", self.resolv())
		result = run_migration(self.mount_root)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assert_correct_link()

	def test_idempotent(self):
		with open(self.resolv(), "w") as f:
			f.write("nameserver 192.0.2.1\n")
		first = run_migration(self.mount_root)
		self.assertEqual(first.returncode, 0, first.stderr)
		second = run_migration(self.mount_root)
		self.assertEqual(second.returncode, 0, second.stderr)
		self.assert_correct_link()

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


class OrderingAndConfigTests(unittest.TestCase):
	def test_fresh_link_after_download(self):
		with open(ALL_INSTALL, "r", encoding="utf-8") as f:
			text = f.read()
		download = text.find("postinstall_download")
		link = text.find("ln -sfnT /usr/lib/systemd/resolv.conf /etc/resolv.conf")
		self.assertNotEqual(download, -1)
		self.assertNotEqual(link, -1)
		self.assertGreater(link, download)

	def test_manifest_includes_systemd_resolved(self):
		with open(MANIFEST, "r", encoding="utf-8") as f:
			text = f.read()
		self.assertIn("systemd-resolved", text)

	def test_nm_config(self):
		parser = configparser.ConfigParser()
		parser.read(NM_CONF)
		self.assertEqual(parser.get("main", "dns"), "systemd-resolved")
		self.assertEqual(parser.get("main", "rc-manager"), "symlink")


if __name__ == "__main__":
	unittest.main()
