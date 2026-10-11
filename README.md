# holtek-a09f-config

Linux configurator (CLI + GTK4 GUI) for gaming mice built on the Holtek **04d9:a09f** controller.
Set the wheel lighting, colour tables and button mapping without the original Windows software.

The same mouse has been sold under many names:

| Brand / model | Status |
|---|---|
| **Zelotes C-13** (2017) | tested: everything below was developed on this mouse |
| E-Signal LUOM G10 | same hardware (the C-13 itself reports "E-Signal USB Gaming Mouse" over USB) |
| Phoenix Void RGB | protocol matches the published captures |
| Ardor Gaming Fury, MSI M99, Fater MCR-9000B | same chip; firmware differences likely (see [Limitations](#limitations)) |

Check yours with `lsusb`:

```
ID 04d9:a09f Holtek Semiconductor, Inc. E-Signal LUOM G10 Mechanical Gaming Mouse
```

## What it can do

- **Read and back up** the full on-board configuration, read-only.
- **Lighting:** effect (Off, Standard, Breathing, Neon, Wave, Key reaction, Trailing, Drag,
  Slide, Yo-yo, Marbles, Flying star), colour, raw effect parameters.
- **Colour tables:** the 8-colour cycle palette and the 8 DPI-stage colours.
- **Polling rate:** 1000 / 500 Hz (verified), 250 / 125 Hz (inferred).
- **Button mapping** for right, wheel click, forward, back and DPI: mouse buttons, media
  keys (play/pause, next/previous, volume, mute, stop), DPI cycle/up/down, or disabled.
- **Restore** lighting, colours and buttons from a backup.
- **Safety:** every write shows the exact packets (changed bytes marked) and asks before
  sending. Every write is read back and verified.
- The per-unit scramble key is **detected automatically**.
- Pure Python standard library for the CLI (raw `hidraw` ioctls, no hidapi). The GUI uses
  the GTK4/libadwaita bindings that GNOME systems already have.

## Install

```sh
git clone https://github.com/salahmak/holtek-a09f-config.git
cd holtek-a09f-config

# allow your desktop user to talk to the mouse (no sudo needed afterwards)
sudo cp udev/99-holtek-a09f.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

The rule uses `TAG+="uaccess"`, which gives access only to the user logged in at the machine.

GUI requirements: Python ≥ 3.9 with PyGObject, GTK ≥ 4.10 and libadwaita ≥ 1.6. On most
GNOME distributions these are already installed; otherwise:

| Distro | Package |
|---|---|
| Arch | `python-gobject libadwaita` |
| Fedora | `python3-gobject libadwaita` |
| Debian/Ubuntu | `python3-gi gir1.2-adw-1` |

Optional app-menu launcher:

```sh
mkdir -p ~/.local/bin ~/.local/share/applications
ln -s "$PWD/a09f_gui.py" ~/.local/bin/a09f-gui
cp holtek-a09f-config.desktop ~/.local/share/applications/
```

## Usage

### GUI

```sh
./a09f_gui.py
```

It has four tabs (Lighting, Colours, Buttons, Device). **Apply** opens a dialog with the
exact bytes that will be written. Backups and restore are on the Device tab.

### CLI

```sh
./a09fctl.py show                       # decode the current config (read-only)
./a09fctl.py backup                     # save everything to ~/.local/share/holtek-a09f-config/backups/
./a09fctl.py color ff8000               # static orange
./a09fctl.py mode neon --p1 3 --p2 255  # effect + raw parameters
./a09fctl.py palette dpi ff00ff ffff00 ff0000 0000ff 00ff00 8000ff ffffff ffffff
./a09fctl.py polling 500                # polling rate in Hz
./a09fctl.py button dpi play-pause      # DPI button sends Play/Pause
./a09fctl.py button forward forward     # back to the factory function
./a09fctl.py restore-buttons ~/.local/share/holtek-a09f-config/backups/config-….json
```

Every write command prints the packets and asks `[y/N]`; add `--yes` to skip the prompt.
The left button can only be remapped with `--force`.

**Make a backup first** (`./a09fctl.py backup`). There is no factory reset.

## How it works (short version)

The mouse exposes a vendor HID interface with an 8-byte feature report for commands and
32-byte input/output reports for data blocks. Command packets carry a simple checksum and are
scrambled with a 6-character key that is **different on each unit/batch** (`C178FF` on the
C-13 used for development). Data blocks such as palettes and the button map are plain bytes.
The full write-up is in **[docs/PROTOCOL.md](docs/PROTOCOL.md)**.

## Limitations

- **Writes are permanent.** On this firmware every change is stored in the mouse's
  memory immediately and survives a replug. There is no "try without saving" mode and no
  known factory-reset command; your backup is the way back.
- **DPI cannot be configured.** The DPI table can be read, and bit 7 enabling/disabling a
  stage is confirmed, but changing a stage's value had no measurable effect on cursor speed.
  The DPI table is read-only in this tool.
- **Effect parameters are raw.** What `p1`/`p2` mean (speed, brightness, length) depends on
  the effect and isn't fully mapped. Defaults come from vendor captures of this model. The
  mouse shipped with Neon at `01 03`, which does not match the captured `03 ff`.
- **Effect numbering differs between firmwares.** On the Ardor Gaming Fury firmware the
  same numbers mean different effects (e.g. Standard is `08`, not `01`). Only the C-13 / E-Signal table
  has been verified.
- **The physical button layout is assumed.** Slot → physical button was mapped on the
  Zelotes C-13. Other shells may have different buttons in different slots.
- **Not supported yet:** macros, the second button-map page, profiles, lift-off
  distance and anything else in the unknown settings groups `02`, `03`, `0b`.
  250 and 125 Hz polling are inferred from the vendor encoding but not measured yet.
- **Neon** uses its own built-in colours, not the cycle palette. Which effects do use the
  palette hasn't been mapped yet.
- **Linux only** (`hidraw`). Only one tool should talk to the mouse at a time.

## What's left to do

Contributions welcome, especially from owners of the other rebrands.

- [ ] **DPI:** find how the stage byte maps to sensor DPI, or which other group sets it
      (`03` = `02 0f d5 04` is a suspect).
- [x] Polling rate (group `01`), identified with help from the 2021 C-13 vendor software.
- [ ] Measure 250 / 125 Hz polling, and confirm group `02` = active profile.
- [ ] **Unknown groups** `03`, `0b`: probably lift-off distance,
      angle snapping or debounce. They need a capture of the vendor software changing each
      setting individually.
- [ ] Map **effect parameters** (speed/brightness ranges per effect) and which effects use the
      cycle palette.
- [ ] **Button types** `0b`, `0c`, the wheel codes `f7`/`f8`, keyboard keys and **macros**.
- [ ] The **second button page** (`12 02`) — a "shift"/profile layer?
- [ ] Confirm effect numbering and button slots on the Ardor, MSI and Fater variants.
- [ ] Packaging (AUR / Flatpak) and possibly an upstream OpenRGB/libratbag driver.

**Helping with another rebrand.** Run `./a09fctl.py show` and open an issue with the output,
your brand/model and `lsusb -v -d 04d9:a09f | grep bcdDevice`. If you still have the Windows
software, USB captures (Wireshark + USBPcap) of single setting changes are gold.
`tools/listen.py` shows what each button sends. `tools/measure.py` measures polling rate and
counts per distance.

## Credits

- USB captures from the vendor software, shared in OpenRGB issues
  [#3721](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/3721) and
  [#1393](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/1393).
- Packet replays for the Phoenix Void / E-Signal in juampe's OpenRGB branch
  ([juampe/OpenRGB](https://github.com/juampe/OpenRGB), `holtek-void-support`).
- The scramble algorithm, first published in
  [Abnegation/Gaming-Fury-Mouse-for-Linux](https://github.com/Abnegation/Gaming-Fury-Mouse-for-Linux).

## Disclaimer

Not affiliated with Holtek, Zelotes, E-Signal or any other brand. This was reverse-engineered
on one mouse. Writes go to the device's on-board memory; use at your own risk and keep a backup.
