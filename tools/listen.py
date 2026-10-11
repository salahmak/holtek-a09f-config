#!/usr/bin/env python3
"""Print every input report from the mouse's non-boot interfaces (read-only).
Useful for seeing what a remapped button actually sends. Usage: listen.py [seconds]"""
import glob, os, select, sys, time

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60
fds = {}
for h in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
    if 'HID_ID=0003:000004D9:0000A09F' in open(h + '/device/uevent').read():
        if open(h + '/device/report_descriptor', 'rb').read()[:4] != bytes.fromhex('05010902'):  # skip boot mouse
            p = '/dev/' + os.path.basename(h)
            fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = p
if not fds:
    sys.exit('mouse not found')
print(f'listening {dur:.0f}s on {", ".join(fds.values())}', flush=True)
t0 = time.time()
while time.time() - t0 < dur:
    for fd in select.select(list(fds), [], [], 0.5)[0]:
        data = os.read(fd, 64)
        print(f'{time.time() - t0:7.3f} {fds[fd]} {data.hex(" ")}', flush=True)
