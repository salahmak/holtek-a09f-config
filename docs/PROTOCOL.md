# Holtek 04d9:a09f configuration protocol

Reverse-engineered notes for the Holtek-based mouse sold as Zelotes C-13, E-Signal LUOM G10,
Ardor Gaming Fury, MSI M99, Phoenix Void RGB and Fater MCR-9000B. Everything marked
**verified** was confirmed on a Zelotes C-13 / E-Signal unit (bcdDevice 2.39) by writing
and reading back. Everything else is labelled as inferred or unknown.

## USB layout

| Interface | Purpose | Report descriptor |
|---|---|---|
| 0 | Boot mouse | 5 buttons, 16-bit X/Y, wheel, AC pan (no report ID) |
| 1 | Extra keys | Keyboard (ID 1), System Control (ID 2), Consumer (ID 3, 16-bit usage), vendor FF01 (ID 6) |
| 2 | **Configuration** | Usage page `0xFF00`, no report IDs: 32-byte Input, 32-byte Output, **8-byte Feature** |

On the C-13/G10 the output report goes to interrupt endpoint `0x04` (32 bytes) and the
input report comes from `0x83`. The Ardor Gaming Fury (fw 3.06) uses OUT `0x03` with a
64-byte packet size and has no input report. Writing through hidraw hides that difference.

Remapped media buttons arrive on interface 1 as Consumer reports, e.g. `03 cd 00` for Play/Pause.

## Command packets (8-byte feature reports)

Plain packet:

```
[cmd, d1, d2, d3, d4, d5, d6, chk]      chk = (0xFF - (cmd + d1 + ... + d6)) & 0xFF
```

On the wire every packet is **scrambled** with a 6-character ASCII key that differs between
units/batches (seen so far: `FB1255` Ardor, `64A3E2` MSI M99, `FB1413` Phoenix Void, `C178FF` Zelotes C-13):

```python
ADD = [0x25, -0x0A, -0x1C, 0x76, 0x47, 0x54, -0x1A, 0x76]
def scramble(p, k):
    q = [p[5], p[4]^k[0], p[7]^k[1], p[6]^k[2], p[1]^k[3], p[0]^k[4], p[3]^k[5], p[2]]
    return bytes(((((q[i] << 3) & 0xFF) | (q[(i+1) % 8] >> 5)) + ADD[i]) & 0xFF for i in range(8))
```

That is a byte permutation, an XOR with the key, a 64-bit rotate left by 3 and a per-byte
add. The inverse is in `a09fctl.py` (`unscramble`). The mouse silently ignores packets
scrambled with the wrong key, because they fail its checksum.

**Finding the key.** Right after plug-in, a GET_FEATURE returns `scramble(00 × 8, key)`, so
the key can be read straight off it. Later the buffer holds a status packet
`[00, s1, s2, s3, 00, 00, 00, 00]`. Key characters 0, 1, 2 and 4 are then still exact, and
characters 3 and 5 are found by probing with a read command, at most 256 tries. Key
character 4 is the one XORed into the command byte, so every probe the mouse can decode
is the same read command. With hex-character keys, no wrong candidate can pass the
checksum. `a09fctl.py` does this and caches the result in `~/.cache/holtek-a09f-config/key`.

### Reading

`cmd | 0x80` with zero arguments reads group `cmd`. The reply sits in the feature buffer and
comes back on the next GET_FEATURE: `[cmd|0x80, values…, 00]`. Replies carry `00` instead of
a checksum. Groups with data also stream 32-byte **input reports** right after the request.

### Writing

- **Simple settings:** one feature report.
- **Data blocks:** a feature report `[cmd, page, length]` followed by 32-byte **output reports**.
- **Persistence:** on this firmware every write is persisted immediately and survives a
  replug. The vendor software ends a session with `08 aa cc ee`, which looks like a
  save/commit, but writes stick without it. `a09fctl` never sends `08`.
- **Feedback:** most writes make the wheel flash the colour of the current DPI stage
  (yellow on stage 2) and then apply. Palette writes apply silently.

## Groups

| Cmd | Read reply / data | Write | Meaning | Status |
|---|---|---|---|---|
| `0C` | `R G B` | `0c R G B` | Wheel colour, used by Standard and single-colour effects | **verified** |
| `0D` | `mode p1 p2` | `0d mode p1 p2` | Lighting effect + 2 parameters | **verified** (Standard, Neon) |
| `10` page 0 | `00 18` + 32 B | `10 00 18` + 32 B | Cycle palette, 8 × RGB (Neon does *not* use it) | **verified** |
| `10` page 1 | `01 18` + 32 B | `10 01 18` + 32 B | DPI-stage colours, 8 × RGB | read verified; write same path |
| `11` | `05 08` + 32 B | `11 05 08` + 32 B | DPI table, 1 byte per stage, bit 7 = stage disabled | stage enable **verified**; value encoding **unknown** |
| `12` page 1 | `01 40` + 64 B | `12 01 40` + 2 × 32 B | Button map (pages 0 and 1 read identically) | **verified** |
| `12` page 2 | `02 40` + 64 B | `12 02 40` | Second map, all zero here; the vendor software writes it too | unknown |
| `01` | `01` | `01 div` | **Polling rate** = 1000 Hz / div (`01` 1000, `02` 500, `04` 250, `08` 125) | **verified** for 1000/500 (measured); 250/125 inferred |
| `02` | `01` | `02 n` | Active profile, 1-based? (the 2021 C-13 software sends `02 [profile+1]`) | hypothesis |
| `03` | `02 0f d5 04` | `03 …` | ? Vendor reads it and writes it back unchanged every time | unknown |
| `0B` | `02` | `0b xx` | ? Not the polling rate: the mouse polls at 1000 Hz with `02` | unknown |
| `08` | — | `08 aa cc ee` | Vendor "save/commit" | not needed, never sent |
| `00` | `02 39` | — | **Firmware version** (2.39 = USB bcdDevice) | **verified** (read) |
| `04` | `30` | ? | unknown (the 2021 software uses `04` to select the DPI stage) | unknown |
| `05` | `00` | ? | unknown | unknown |
| `06` | `0a 0a 19 19` | ? | unknown; changing `19 19` → `32 32` had no measurable effect on DPI | unknown |
| `07` | `00 03 05 05` | ? | unknown | unknown |
| `09` | ⚠ | — | **Reading `89` stalls the config interface** (every later feature request fails with EPIPE) until the mouse is replugged. Don't send it. | observed |

Groups `0a`–`0f` (other than the ones above) have not been probed.

Page arguments only use bit 0 (`90 02` reads the same as `90 00`). `91` ignores its argument.

### Lighting effects (`0d mode p1 p2`, C-13 / E-Signal firmware)

| Mode | Effect | Default p1 p2 (from vendor captures) |
|---|---|---|
| `00` | Off | `00 00` |
| `01` | Standard (static, uses `0c` colour) | `ff 00` — **verified** |
| `02` | Breathing | `03 00` |
| `03` | Neon (built-in colour cycle) | `03 ff` — **verified** (unit shipped with `01 03`) |
| `04` | Wave | `03 ff` |
| `05` | Key reaction | `ff 00` |
| `06` | Trailing | `02 00` |
| `07` | Drag | `02 00` |
| `08` | Slide | `ff 00` (`ff 00` and `01 00` both showed nothing on the C-13) |
| `09` | Yo-yo | `ff 10` |
| `0a` | Marbles | `ff 01` |
| `0b` | Flying star | `ff 00` |

⚠ The Ardor Gaming Fury firmware numbers these differently: there, `08` is Standard and `01`
is "ripple breathing". Check yours before trusting this table.

### Button map (`12`)

64 bytes, 16 entries of 4 bytes: `[type, 00, code, 00]`.

| Type | Meaning | Codes |
|---|---|---|
| `00` | disabled / unused | — |
| `01` | mouse function | `f0` left, `f1` right, `f2` middle, `f3` back, `f4` forward, `f7`/`f8` (wheel up/down?) |
| `03` | consumer (media) key | HID consumer usage: `cd` play/pause, `b5` next, `b6` previous, `e9`/`ea` volume, `e2` mute |
| `04` | wheel (factory maps) | `01`, `02` |
| `07` | DPI | `01` DPI+, `02` DPI−, `03` DPI cycle (**verified**) |
| `0b`, `0c` | seen in factory maps | unknown |

Slots on the C-13: 1 left, 2 right, 3 wheel click, 4 forward, 5 back, 6 DPI, 15–16 wheel.

### DPI table (`11`)

As found on the C-13: `07 08 c6 c8 cb 80 80 80`: two enabled stages (`07`, `08`); stages
with bit 7 set are skipped when cycling. The Phoenix Void factory table is
`08 10 18 30 60 7e 7e 78`. On the C-13, changing `07 08` to `08 10` made no measurable
difference in cursor speed, even after a replug (measured about 1050–1280 counts/inch either way),
and changing group `06` had no effect either. The most likely explanation is that the sensor
DPI on this unit is fixed (≈1100–1300 CPI measured on every stage) or is set by a command that
appears in no available capture. A capture of the vendor software changing only DPI would settle it.

## Related: the 2021 Zelotes C-13 software

The vendor software for the newer (2021) C-13 targets a different controller (USB `1017:9116`) and
uses 64-byte output reports, but the same 8-byte command format and checksum, **without**
scrambling. Its `Monitor.exe` sends `02 [profile+1]` (profile), `03 [profile+1] [8|4|2|1]`
(polling rate as a divider), and `04 [profile+1] [value+1]`. The divider encoding is what
identified group `01` above. Its `.gmp` profiles are INI files XOR-ed with `0x32`.

Its DPI handling, for reference (it does **not** apply to the 04d9:a09f, see above):

- **Stored:** 5 levels per axis, with CPI = level × 90.
- **Encoded:** a 100-entry table converts each level to a sensor byte (≈39 CPI per step, and bit 7
  selects a ×2 high range: level 100 → `0xf3`).
- **Sent:** inside a 128-byte control page (`0c xx 80` + 2 × 64 bytes). `04 [profile+1] [stage+1]`
  selects the active stage.

## Sources

- Raw USB captures from the vendor software: OpenRGB issues
  [#3721](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/3721) (Ardor Gaming Fury) and
  [#1393](https://gitlab.com/CalcProgrammer1/OpenRGB/-/issues/1393) (MSI M99).
- Packet replays for the Phoenix Void / E-Signal: the unmerged OpenRGB driver by juampe
  ([juampe/OpenRGB](https://github.com/juampe/OpenRGB), branch `holtek-void-support`).
- The scramble function was first published in
  [Abnegation/Gaming-Fury-Mouse-for-Linux](https://github.com/Abnegation/Gaming-Fury-Mouse-for-Linux)
  (ported from the vendor driver). It was checked against all of the captures above, and the
  per-unit keys were recovered from them.
