# Shared desktop oneshot handoff

The GNOME, Plasma, Hyprland, Cosmic and Cinnamon oneshot launchers use one Bash
library, `rootfs/usr/lib/skorion-session-handoff`, rather than a fixed two-second
sleep. Every direct desktop exec in those wrappers, including restoration
failure fallbacks, goes through its observation-only barrier. Cinnamon's special
Xorg branch waits before scheduling the delayed return update or asking SDDM to
switch sessions. Desktop-specific commands, arguments and restoration paths
remain in their respective wrappers; Plasma's X11 command is an argument array
(`sx`, `startplasma-x11`), not a single executable pathname containing a space.

## Scope and packaging

The 11 current `branch/manifest-*` variants select GNOME (four), Plasma/KDE
(four), or Hyprland (three) overlays. All inherit the helper from the shared
rootfs; none of their POSTCOPY files overwrite it. The retained Cinnamon and
Cosmic overlays have `branch/-manifest-*` definitions and are not in the current
matrix. Their same-protocol wrappers use the helper too, but this does not
reactivate their builds or establish that their desktop packages work.

This fixes the shared Steam-return-to-desktop oneshot handoff risk. The observed
GNOME 50 graphical-session guard is GNOME-specific; the same error is not
attributed to other desktops. Native persistent login and the `skorion-session desktop` / `desktop-xorg`
routes bypass the marker/return protocol and are unchanged.
Existing `return-to-game-mode` commands keep each desktop's native logout path.
No barrier is inserted into the old session while waiting for that session to
stop itself.

## Safety behavior

The library uses a 30-second monotonic deadline (0.2-second polling) to observe
`graphical-session.target` and relevant `gamescope-session-plus@*.service`
instances, with an explicit Steam-instance check. Complete inactive/dead state
and no unit job are required. Related queued jobs of any type block; unrelated
jobs do not. Every systemctl query has a two-second timeout capped by the
remaining deadline. Query errors, missing/empty/malformed unit records and
failed services fail closed. A failed service can cause an abnormal transition
to return to the game instead of trying the desktop; failure state is not reset.

The desktop marker is consumed before waiting. A removal error prevents the
transition and is reported; timeout preserves the saved return for the next
SDDM Relogin. A marker written during the wait is not removed at the end.
Cinnamon explicitly rejects an unreadable/missing marker, rather than allowing
an ignored `cat` error to masquerade as an Xorg request.

One persistent advisory lock, `skorion-session-oneshot.lock`, serializes all five
oneshot entrypoints before marker/return handling. Contenders cannot restore or
consume state. The lock file is never unlinked. Desktop exec and external
restoration commands close the descriptor; Cinnamon's SDDM/update commands also
run without it. A missing shared library is an explicit fatal error.

The existing `XDG_CONF_DIR` convention is kept aligned with the OS selection
hooks, including paths relative to HOME. This change does not switch only the
read side to `XDG_CONFIG_HOME`.

The helper observes state and never stops units, kills user sessions or changes
running services. Its advisory lock does not coordinate os-session-select,
which does not take that lock. A simultaneous new request or activation after
the final query is not atomically prevented. Quiescent systemd state does not
prove GPU/DRM readiness or replace correct old-session teardown.

## Verification

Run from the repository root:

    bash -n rootfs/usr/lib/skorion-session-handoff
    python3 -m unittest discover -s tests -p 'test_session_handoff.py' -v

Tests use sandbox copies with mock systemctl, desktop commands and restoration;
GNU timeout, flock, files and concurrency are real. Packaging tests inspect the
current manifest/POSTCOPY composition without building images or running hooks.
They do not validate real GNOME, Plasma, Hyprland, Cosmic, Cinnamon, systemd,
SDDM or graphics drivers. Core tests and per-desktop behavior are distinguished
from the non-active overlays' compatibility tests.

Before release, validate the three active desktop families on SkorionOS:
normal game → desktop, old-service teardown slower than two seconds, target
stopping before the service, desktop logout back to game, failed/timeout
handoff, Plasma X11 where supported, and another same-user graphical session.
Capture unit state and jobs around transitions. No reproduction of the original
reported failure is claimed without its logs.
