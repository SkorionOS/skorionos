# iwd image support

## Scope

The common manifest explicitly installs both `iwd` and `wpa_supplicant` alongside
NetworkManager. All 11 active desktop variants inherit it; the two legacy
Cinnamon/Cosmic manifests are also checked. No backend is enabled independently
in `SERVICES`. NetworkManager continues to select and start its backend.
The existing NetworkManager backend preference is not changed.

`rootfs/etc/iwd/main.conf` supplies only:

```ini
[DriverQuirks]
DefaultInterface=iwlwifi
```

This prevents iwd from destroying/recreating the default interface on the tested
Intel AX210/iwlwifi path, preserving predictable names used by existing
NetworkManager profiles. It deliberately does not use `*` or add untested drivers.
It does not turn on iwd's IP/DHCP handling. No ARP sysctl changes are included.

The iwd package also ships `80-iwd.link` with `Type=wlan` and
`NamePolicy=keep kernel`. This vendor rule would change Wi-Fi naming on cold boot
even without starting iwd. The build's pacman configuration excludes that exact
file with `NoExtract`, before package hooks can generate an initramfs. Final
image cleanup also removes that vendor path if it was already present. This
preserves the existing systemd predictable-naming policy for all Wi-Fi drivers;
no administrator `/etc/systemd/network` rules are replaced.

## Existing installations and configuration ownership

The image's `/etc` is the lower layer. The active initramfs uses `systemd`
(`rootfs/etc/mkinitcpio.conf`); `all-install.sh` generates the `/etc/fstab` overlay
with `lowerdir=/sysroot/etc`, `upperdir=/sysroot/frzr_root/etc`, and
`workdir=/sysroot/frzr_root/.etc`. The older `frzr-etc` hook is still in the tree
but is not in the configured HOOKS list. Therefore a new default becomes visible
automatically when no upper
`iwd/main.conf` exists; no deployment migration is necessary.

An existing user `main.conf` wins, byte for byte, including a user-selected
`DefaultInterface`. A whiteout of the file or an opaque upper `iwd` directory can
also hide the new default. These are preserved intentionally. An administrator
with such an override must merge the desired quirk themselves; upgrades do not
silently rewrite their configuration. Previously saved `wifi.backend=iwd` remains
selected and is now backed by the installed daemon. No forced wpa migration runs.
No host service is restarted by this image change.

The deployed frzr 3.5.3 runs migration functions on the host with
`post_install MOUNT_PATH SUBVOL NAME`, and ignores their failures. Avoiding a
migration also avoids modifying the live host `/etc` or confusing the new image's
`SUBVOL/etc` with the persistent overlay.

## Dependency and activation evidence

Checked 2026-10-12 against official Arch package metadata:

- iwd 3.12-2 (built 2026-09-17) depends on `ell`, `glibc`, `libgcc`, and readline.
  Normal `pacman -S` dependency resolution in `all-install.sh` includes these;
  no separate hardcoded ell entry is needed.
- The package ships `/usr/lib/iwd/iwd`, `/usr/lib/systemd/system/iwd.service`,
  `/usr/share/dbus-1/system-services/net.connman.iwd.service` and
  `/usr/share/dbus-1/system.d/iwd-dbus.conf`. Image cleanup does not remove these.
- The actual official iwd 3.12-2 package was downloaded and its `80-iwd.link`
  inspected to confirm the cold-boot naming rule above.
- Package metadata confirms activation files exist, not that a freshly built
  image has successfully activated them. Archive date remains 2026/10/01;
  version remains 56-5. The build's actual selected packages still require image
  build verification (custom repositories and testing repositories have priority).

Sources:
- https://archlinux.org/packages/extra/x86_64/iwd/
- https://archlinux.org/packages/extra/x86_64/iwd/files/
- https://man.archlinux.org/man/iwd.config.5.en
- https://github.com/honjow/frzr/blob/3.5.3/__frzr-deploy#L2003-L2012

## Verification

Run `python3 -m unittest discover -s tests -p 'test_iwd_image.py' -v`.

The focused tests execute real Bash manifest evaluation for all 13 variants and
check the active systemd initramfs hook list and generated fstab source, and
execute the legacy initramfs hook with a mocked mount command. Overlay
precedence, whiteouts, opaque directories and repeated deployment are an explicit
Python model, not real kernel overlay mounts. They verify the intended policy
without privileged mounts or changing host configuration. No deployment
migration was added; repeated-deployment tests cover the no-migration behavior.

This patch has not built an ISO/system image, booted that image, installed packages,
started D-Bus/iwd, or tested cold boot, suspend/resume, roaming or other Wi-Fi
hardware. AX210 hardware observations supplied for the change validate only that
case, not general device coverage. Before release, verify the produced image's
package list, binary dependencies and activation files. Inspect both the final
rootfs and generated initramfs to confirm `80-iwd.link` is absent; the build-time
exclusion is not a substitute for checking the actual artifact. Then cold boot
and test both backends, retained profile names, DHCP/connectivity and
suspend/resume.
