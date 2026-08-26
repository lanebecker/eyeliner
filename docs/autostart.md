# Autostart: running the appliance on boot

The app draws through SDL, and **how SDL reaches the display depends on your
Pi's session model.** There is no single "correct" autostart unit — the right
one depends on whether you boot to a Wayland desktop, an X11 desktop, or a bare
console. Run the detection step, find your case, follow that section.

> Full multi-environment support (a backend **detection script**, the
> console/KMSDRM kiosk path, and a `pi-setup-guide.md` §12 rewrite) is tracked
> in **#451**. This guide is Phase 1: it documents the working desktop path and
> preserves the legacy X11 path so no configuration is left stranded.

## 1. Detect your backend

From the Pi — in a **graphical session** for a desktop setup, or at the
**console** for a headless one — initialise pygame and read the driver SDL
selects:

```bash
<APP_DIR>/venv/bin/python -c "import pygame; pygame.display.init(); print('SDL driver:', pygame.display.get_driver()); pygame.display.quit()"
```

And note your session model:

```bash
systemctl get-default        # graphical.target = desktop; multi-user.target = console
echo "WAYLAND_DISPLAY=$WAYLAND_DISPLAY | XDG_SESSION_TYPE=$XDG_SESSION_TYPE"
```

| SDL driver | Session | Your case |
|-----------|---------|-----------|
| `wayland` | Wayland desktop (labwc / wayfire — current Pi OS default) | **Case A — user service** |
| `x11`     | X11 desktop | **Case A — user service** (or the legacy **Case C**) |
| `kmsdrm`  | console, no desktop | **Case B — system service** (planned, #451) |

> If you ran the probe over **SSH** and got `wayland`, that's expected: libwayland
> defaults to the `wayland-0` socket even with `WAYLAND_DISPLAY` unset, so SSH
> reaches the running desktop's compositor.

## Case A — desktop session (Wayland or X11): user service

The supported path for the current Raspberry Pi OS default (labwc / Wayland).
The app runs as a **user** service *inside* your logged-in graphical session and
is launched by the compositor's autostart. **Requires autologin to the desktop**
(otherwise the session, and the app, never start).

The unit template is [`deploy/user-session.service.in`](../deploy/user-session.service.in).
Substitute `@APP_DIR@` (e.g. `/home/pi/eyeliner`) and `@WAYLAND_DISPLAY@`
(labwc default `wayland-0`; confirm with `ls /run/user/1000/ | grep wayland`).

**Install the user unit** (example values for the `pi` user and `~/eyeliner`):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/eyeliner.service <<'EOF'
[Unit]
Description=eyeliner vinyl now-playing display
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=/home/pi/eyeliner
Environment=WAYLAND_DISPLAY=wayland-0
ExecStart=/home/pi/eyeliner/venv/bin/python /home/pi/eyeliner/main.py
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=15
TimeoutStopSec=30
EOF
export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user daemon-reload
```

**Hook it into the compositor's autostart** (labwc), preserving the desktop
default so the panel/wallpaper survive:

```bash
mkdir -p ~/.config/labwc
[ -f ~/.config/labwc/autostart ] || { cp /etc/xdg/labwc/autostart ~/.config/labwc/autostart 2>/dev/null || touch ~/.config/labwc/autostart; }
grep -q 'eyeliner.service' ~/.config/labwc/autostart || echo 'systemctl --user start eyeliner.service &' >> ~/.config/labwc/autostart
```

**Test live, then reboot-test:**

```bash
export XDG_RUNTIME_DIR=/run/user/1000
systemctl --user start eyeliner.service
systemctl --user status eyeliner.service --no-pager | head -5   # want: active (running), display fullscreen
sudo reboot                                                     # screen should come up unattended
```

**Rollback** (fully undoes autostart):

```bash
systemctl --user stop eyeliner.service
sed -i '/eyeliner.service/d' ~/.config/labwc/autostart
rm ~/.config/systemd/user/eyeliner.service && systemctl --user daemon-reload
```

**Logs:** `systemctl --user status eyeliner.service` shows recent output. The
user journal may not persist across reboots by default; enable persistent
logging if you want history.

## Case B — console kiosk (KMSDRM): system service

**Planned in Phase 2 (#451).** Boot to console (`multi-user.target`), no
compositor; SDL renders straight to the framebuffer via
`SDL_VIDEODRIVER=kmsdrm`, run as a **system** service. This is the leanest
appliance (no desktop overhead) but has no desktop to fall back to. Until this
lands, a headless Pi can use Case A by enabling the desktop + autologin.

## Case C — legacy X11 system service

The original ratified path — [`deploy/vinyl-now-playing.service.in`](../deploy/vinyl-now-playing.service.in),
`scripts/render_system_service.py`, and `pi-setup-guide.md` §12 — targets an
**X11 desktop** and discovers `DISPLAY`/`XAUTHORITY` from a running **Xwayland**
process. Use it only on a genuine X11 setup.

> ⚠️ On a **native Wayland** session (no Xwayland — the current Pi OS default),
> that path's auth discovery hard-fails ("No Xwayland process found"). Use
> **Case A** there. This supersedes the "system-service only / do not use a user
> service" guidance in §12 **for non-X11 setups**; the full §12 reconciliation is
> Phase 2 of #451.

## Safety: the clock gate is universal

Whichever case you pick, the date-dependent writes (Last Played + the scrobble
timestamp) are gated **in the app** by [`src/util/clock.py`](../src/util/clock.py)
(STAB-2): it refuses to write from an unset/epoch, grossly stale, or far-future
clock, so no launch method can stamp a catastrophic date over your collection.

The systemd `time-sync.target` ordering in the system-service cases (B/C) is
**defense-in-depth** on top of that. The user-service Case A does not carry that
explicit ordering, but the residual it would cover (a stale-but-plausible clock
on an **offline** cold boot) is mooted in practice: recognition needs the
network, so an offline boot never reaches a date write in the first place.
Adding an optional user-unit clock pre-flight is tracked in #451.
