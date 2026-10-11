#!/usr/bin/env python3
"""Read-only: record the mouse's boot-protocol reports for N seconds (usage: measure.py [seconds]).
Prints polling-interval stats and total X/Y counts; slide a known distance to estimate DPI. Report: [buttons, X lo, X hi, Y lo, Y hi, wheel, pan]."""
import glob, os, select, statistics, struct, sys, time
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 5
path = next('/dev/' + os.path.basename(h) for h in sorted(glob.glob('/sys/class/hidraw/hidraw*'))
            if 'HID_ID=0003:000004D9:0000A09F' in open(h + '/device/uevent').read()
            and open(h + '/device/report_descriptor', 'rb').read()[:4] == bytes.fromhex('05010902'))
fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
print(f'recording {path} for {dur}s — go!', flush=True)
t0 = time.time(); ts = []; sx = sy = 0; n = 0
while time.time() - t0 < dur:
    if select.select([fd], [], [], 0.2)[0]:
        d = os.read(fd, 64); t = time.perf_counter()
        if len(d) >= 5:
            x, y = struct.unpack_from('<hh', d, 1); sx += x; sy += y; n += 1
            if x or y: ts.append(t)
iv = [(b - a) * 1000 for a, b in zip(ts, ts[1:]) if (b - a) < 0.05]
print(f'reports {n}, motion reports {len(ts)}, sum X {sx}, sum Y {sy}')
if iv:
    print(f'interval ms: median {statistics.median(iv):.2f}  min {min(iv):.2f}  ->  ~{1000/statistics.median(iv):.0f} Hz')
