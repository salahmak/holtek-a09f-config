#!/usr/bin/env python3
"""a09fctl — configure Holtek 04d9:a09f gaming mice (Zelotes C-13, E-Signal LUOM G10, ...) on Linux.

Protocol summary (details in docs/PROTOCOL.md):
  * 8-byte feature reports on the vendor interface (usage page 0xFF00, no report ID):
    plain packet [cmd, d1..d6, chk], chk = (0xFF - sum(b0..b6)) & 0xFF,
    scrambled with a per-unit 6-character key. The key is detected automatically.
  * cmd | 0x80 reads a group; the reply arrives on the next GET_FEATURE, larger groups
    additionally stream 32-byte input reports.
  * Data blocks (palettes, button map) follow a [cmd, page, length] feature report as
    32-byte output reports.
  * Writes are stored in the mouse immediately; there is no separate save step.

Only commands verified on real hardware are writable: 0x0C colour, 0x0D effect,
0x10 colour tables, 0x12 button map. Every write prints its packets and asks for
confirmation unless --yes is given.
"""
import argparse, datetime, fcntl, glob, json, os, sys, time

VID, PID = 0x04D9, 0xA09F
ADD = [0x25, -0x0A, -0x1C, 0x76, 0x47, 0x54, -0x1A, 0x76]
KEY_CHARS = b'0123456789ABCDEF'
BACKUP_DIR = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')),
                          'holtek-a09f-config', 'backups')
KEY_CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
                         'holtek-a09f-config', 'key')

# Effect table (Zelotes C-13 / E-Signal firmware) with default (p1, p2) from vendor captures.
MODES = {
    'off':          (0x00, 0x00, 0x00),
    'standard':     (0x01, 0xFF, 0x00),
    'breathing':    (0x02, 0x03, 0x00),
    'neon':         (0x03, 0x03, 0xFF),
    'wave':         (0x04, 0x03, 0xFF),
    'key-reaction': (0x05, 0xFF, 0x00),
    'trailing':     (0x06, 0x02, 0x00),
    'drag':         (0x07, 0x02, 0x00),
    'slide':        (0x08, 0xFF, 0x00),
    'yo-yo':        (0x09, 0xFF, 0x10),
    'marbles':      (0x0A, 0xFF, 0x01),
    'flying-star':  (0x0B, 0xFF, 0x00),
}
MODE_NAMES = {v[0]: k for k, v in MODES.items()}
READ_GROUPS = [(0x81, 0), (0x82, 0), (0x83, 0), (0x8B, 0), (0x8C, 0), (0x8D, 0),
               (0x90, 0), (0x90, 1), (0x91, 0), (0x92, 0), (0x92, 1), (0x92, 2)]
DATA_LEN = {0x90: 32, 0x91: 32, 0x92: 64}   # read groups that also stream input reports

BUTTON_TYPES = {0x01: 'mouse', 0x03: 'consumer', 0x04: 'wheel', 0x07: 'dpi'}
MOUSE_CODES = {0xF0: 'left', 0xF1: 'right', 0xF2: 'middle', 0xF3: 'back', 0xF4: 'forward'}
CONSUMER_CODES = {0xCD: 'play/pause', 0xB5: 'next track', 0xB6: 'previous track',
                  0xB7: 'stop', 0xE2: 'mute', 0xE9: 'volume up', 0xEA: 'volume down'}
DPI_CODES = {0x01: 'dpi+', 0x02: 'dpi-', 0x03: 'dpi cycle'}
# Assignable actions -> (type, code)
ACTIONS = {
    'left': (0x01, 0xF0), 'right': (0x01, 0xF1), 'middle': (0x01, 0xF2),
    'back': (0x01, 0xF3), 'forward': (0x01, 0xF4),
    'play-pause': (0x03, 0xCD), 'next-track': (0x03, 0xB5), 'prev-track': (0x03, 0xB6),
    'stop': (0x03, 0xB7), 'mute': (0x03, 0xE2), 'volume-up': (0x03, 0xE9), 'volume-down': (0x03, 0xEA),
    'dpi-cycle': (0x07, 0x03), 'dpi-up': (0x07, 0x01), 'dpi-down': (0x07, 0x02),
    'disabled': (0x00, 0x00),
}
# Physical button -> slot in the button map (1-based)
SLOTS = {'left': 1, 'right': 2, 'middle': 3, 'forward': 4, 'back': 5, 'dpi': 6}


# ------------------------------------------------------------------ packet layer
def scramble(p, k):
    q = [p[5], p[4] ^ k[0], p[7] ^ k[1], p[6] ^ k[2], p[1] ^ k[3], p[0] ^ k[4], p[3] ^ k[5], p[2]]
    return bytes(((((q[i] << 3) & 0xFF) | (q[(i + 1) % 8] >> 5)) + ADD[i]) & 0xFF for i in range(8))


def _descramble_q(c):
    v = bytes((c[i] - ADD[i]) & 0xFF for i in range(8))
    x = int.from_bytes(v, 'big')
    return (((x >> 3) | (x << 61)) & (2**64 - 1)).to_bytes(8, 'big')


def unscramble(c, k):
    q = _descramble_q(c)
    p = [0] * 8
    p[5], p[4], p[7], p[6] = q[0], q[1] ^ k[0], q[2] ^ k[1], q[3] ^ k[2]
    p[1], p[0], p[3], p[2] = q[4] ^ k[3], q[5] ^ k[4], q[6] ^ k[5], q[7]
    return bytes(p)


def key_from_idle(c):
    """Freshly plugged in, the mouse's feature buffer is scramble(00*8, key): the key falls out."""
    q = _descramble_q(c)
    key = q[1:7]
    if q[0] == 0 and q[7] == 0 and all(ch in KEY_CHARS for ch in key):
        return key
    return None


def key_candidates(c):
    """Later the buffer holds a status packet [00, s1, s2, s3, 00, 00, 00, 00]. Bytes 0 and 4..7 are
    still zero, so key chars 0, 1, 2 and 4 are exact; chars 3 and 5 (which only touch s1 and s3)
    are enumerated. Key char 4 protects the command byte, so every candidate decodes as the same
    read command on the mouse, and only the right key passes its checksum."""
    q = _descramble_q(c)
    if q[0] != 0 or not all(ch in KEY_CHARS for ch in (q[1], q[2], q[3], q[5])):
        return []
    return [bytes((q[1], q[2], q[3], k3, q[5], k5)) for k3 in KEY_CHARS for k5 in KEY_CHARS]


def packet(*b):
    b = list(b) + [0] * (7 - len(b))
    return bytes(b + [(0xFF - sum(b)) & 0xFF])


# ------------------------------------------------------------------ device layer
class MouseNotFound(Exception):
    pass


def _hidioc(nr, size):
    return (3 << 30) | (size << 16) | (ord('H') << 8) | nr   # _IOC(_IOC_WRITE|_IOC_READ, 'H', nr, size)


HIDIOCSFEATURE9, HIDIOCGFEATURE9 = _hidioc(0x06, 9), _hidioc(0x07, 9)


def find_config_hidraw():
    for h in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        try:
            ue = open(h + '/device/uevent').read()
            desc = open(h + '/device/report_descriptor', 'rb').read()
        except OSError:
            continue
        if f'HID_ID=0003:{VID:08X}:{PID:08X}' in ue and desc[:3] == b'\x06\x00\xff':
            return '/dev/' + os.path.basename(h)
    return None


class Mouse:
    """Talks to the vendor interface through raw hidraw ioctls (no hidapi needed)."""

    def __init__(self, key=None):
        path = find_config_hidraw()
        if not path:
            raise MouseNotFound('no Holtek 04d9:a09f config interface found — is the mouse plugged in?')
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            self.key = key or self._detect_key()
        except Exception:
            os.close(self.fd)
            raise

    def _get_feature(self):
        buf = bytearray(9)
        fcntl.ioctl(self.fd, HIDIOCGFEATURE9, buf)
        return bytes(buf[1:])

    def _probe(self, key):
        """Send a read (0x83) scrambled with `key`; the mouse only echoes it if the key is right."""
        fcntl.ioctl(self.fd, HIDIOCSFEATURE9, bytearray(b'\x00' + scramble(packet(0x83), key)))
        time.sleep(0.01)
        return unscramble(self._get_feature(), key)[0] == 0x83

    def _detect_key(self):
        cached = None
        try:
            cached = open(KEY_CACHE, 'rb').read().strip()
        except OSError:
            pass
        idle = self._get_feature()
        exact = key_from_idle(idle)
        for key in [k for k in (cached, exact) if k] + key_candidates(idle):
            if len(key) == 6 and self._probe(key):
                if key != cached:
                    os.makedirs(os.path.dirname(KEY_CACHE), exist_ok=True)
                    with open(KEY_CACHE, 'wb') as fh:
                        fh.write(key)
                return key
        raise IOError('could not detect the scramble key; replug the mouse and retry, or pass --key')

    def _read_input(self):
        try:
            return os.read(self.fd, 64)
        except BlockingIOError:
            return b''

    def send(self, p):
        fcntl.ioctl(self.fd, HIDIOCSFEATURE9, bytearray(b'\x00' + scramble(p, self.key)))
        time.sleep(0.02)

    def wire(self, p):
        return scramble(p, self.key)

    def read(self, cmd, arg=0):
        while self._read_input():
            pass
        self.send(packet(cmd, arg))
        time.sleep(0.03)
        reply = unscramble(self._get_feature(), self.key)
        if reply[0] != cmd:
            raise IOError(f'bad reply to {cmd:02x}: {reply.hex(" ")}')
        data, want, t0 = b'', DATA_LEN.get(cmd, 0), time.time()
        while len(data) < want and time.time() - t0 < 0.5:
            x = self._read_input()
            if x:
                data += x
            else:
                time.sleep(0.005)
        return reply[1:7], data

    def write_block(self, cmd, page, length, data):
        """Feature [cmd, page, length] followed by 32-byte output reports; returns read-back data."""
        self.send(packet(cmd, page, length))
        for i in range(0, len(data), 32):
            os.write(self.fd, b'\x00' + data[i:i + 32])
            time.sleep(0.02)
        time.sleep(0.1)
        return self.read(cmd | 0x80, page)[1][:len(data)]

    def close(self):
        os.close(self.fd)


def dump(m):
    out = {'key': m.key.decode()}
    for cmd, arg in READ_GROUPS:
        reply, data = m.read(cmd, arg)
        out[f'{cmd:02x}:{arg}'] = {'reply': reply.hex(), 'data': data.hex()}
    return out


def save_backup(cfg):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    f = os.path.join(BACKUP_DIR, f'config-{datetime.datetime.now():%Y%m%d-%H%M%S}.json')
    with open(f, 'w') as fh:
        json.dump(cfg, fh, indent=1)
    return f


# ------------------------------------------------------------------ CLI
def describe(cfg):
    c = bytes.fromhex(cfg['8c:0']['reply'])
    md = bytes.fromhex(cfg['8d:0']['reply'])
    print(f'key           {cfg["key"]}')
    print(f'colour        #{c[0]:02x}{c[1]:02x}{c[2]:02x}')
    print(f'effect        {MODE_NAMES.get(md[0], hex(md[0]))}  (params {md[1]:#04x} {md[2]:#04x})')

    def rgb_list(h):
        b = bytes.fromhex(h)[:24]
        return ' '.join('#' + b[i:i + 3].hex() for i in range(0, 24, 3))
    print('cycle palette', rgb_list(cfg['90:0']['data']))
    print('DPI colours  ', rgb_list(cfg['90:1']['data']))
    print('DPI table     (raw)', bytes.fromhex(cfg['91:0']['data'])[:8].hex(' '), ' reply', cfg['91:0']['reply'])
    print('buttons:')
    b = bytes.fromhex(cfg['92:1']['data'])
    phys = {v: k for k, v in SLOTS.items()}
    for i in range(0, len(b), 4):
        t, code = b[i], b[i + 2]
        if t == 0:
            continue
        name = {1: MOUSE_CODES, 3: CONSUMER_CODES, 7: DPI_CODES}.get(t, {}).get(code, f'{code:#04x}')
        print(f'  slot {i // 4 + 1:2d} {phys.get(i // 4 + 1, ""):8s}: {BUTTON_TYPES.get(t, f"type {t:#04x}")} {name}')
    for k in ('81:0', '82:0', '83:0', '8b:0'):
        print(f'group {k[:2]}      {cfg[k]["reply"]}  (unknown)')


def ask(yes):
    if not yes and input('Write this to the mouse (persistent)? [y/N] ').strip().lower() != 'y':
        sys.exit('aborted')


def confirm_and_write_block(m, cmd, page, length, old, new, yes):
    p = packet(cmd, page, length)
    print(f'  feature {p.hex(" ")}   (wire {m.wire(p).hex(" ")})')
    for i in range(0, len(new), 32):
        mark = ''.join('^^ ' if a != b else '   ' for a, b in zip(old[i:i + 32], new[i:i + 32]))
        print(f'  data    {new[i:i + 32].hex(" ")}\n          {mark.rstrip()}')
    if old == new:
        sys.exit('nothing to change')
    ask(yes)
    back = m.write_block(cmd, page, length, new)
    print('VERIFIED' if back == new else f'MISMATCH, read back {back.hex(" ")}')


def confirm_and_write(m, pkts, yes):
    for p in pkts:
        print(f'  {p.hex(" ")}   (wire {m.wire(p).hex(" ")})')
    ask(yes)
    for p in pkts:
        m.send(p)
    print('done; read back colour', m.read(0x8C)[0][:3].hex(' '), '| effect', m.read(0x8D)[0][:3].hex(' '))


def main():
    ap = argparse.ArgumentParser(description='Configure Holtek 04d9:a09f gaming mice (Zelotes C-13, E-Signal LUOM G10, ...).')
    ap.add_argument('--key', help='6-character scramble key (normally auto-detected)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('show', help='read and decode current config (read-only)')
    sub.add_parser('backup', help=f'save full raw config as JSON in {BACKUP_DIR} (read-only)')
    c = sub.add_parser('color', help='set a static colour, e.g. color ff8000')
    c.add_argument('rgb')
    c.add_argument('--yes', action='store_true')
    mo = sub.add_parser('mode', help='set the lighting effect')
    mo.add_argument('name', choices=MODES)
    mo.add_argument('--p1', type=lambda s: int(s, 0), help='effect parameter 1 (speed/brightness/length)')
    mo.add_argument('--p2', type=lambda s: int(s, 0), help='effect parameter 2')
    mo.add_argument('--yes', action='store_true')
    pa = sub.add_parser('palette', help='set the 8-colour cycle palette or the DPI stage colours')
    pa.add_argument('table', choices=('cycle', 'dpi'))
    pa.add_argument('colors', nargs=8, metavar='RRGGBB')
    pa.add_argument('--yes', action='store_true')
    bt = sub.add_parser('button', help='remap a button, e.g. button dpi play-pause')
    bt.add_argument('button', choices=SLOTS)
    bt.add_argument('action', choices=ACTIONS)
    bt.add_argument('--force', action='store_true', help='allow changing the left button')
    bt.add_argument('--yes', action='store_true')
    for name, what in (('restore-lighting', 'colour + effect'), ('restore-buttons', 'button map')):
        r = sub.add_parser(name, help=f'restore {what} from a backup JSON')
        r.add_argument('file')
        r.add_argument('--yes', action='store_true')
    a = ap.parse_args()

    key = None
    if a.key:
        key = a.key.upper().encode()
        if len(key) != 6:
            sys.exit('--key must be 6 characters')
    try:
        m = Mouse(key)
    except (MouseNotFound, PermissionError, IOError) as e:
        hint = '  (install udev/99-holtek-a09f.rules, see README)' if isinstance(e, PermissionError) else ''
        sys.exit(f'{e}{hint}')
    try:
        if a.cmd == 'show':
            describe(dump(m))
        elif a.cmd == 'backup':
            print('saved', save_backup(dump(m)))
        elif a.cmd == 'color':
            rgb = bytes.fromhex(a.rgb.lstrip('#'))
            if len(rgb) != 3:
                sys.exit('colour must be RRGGBB')
            pk = [packet(0x0C, *rgb)]
            if m.read(0x8D)[0][0] != MODES['standard'][0]:
                pk.append(packet(0x0D, *MODES['standard']))
            confirm_and_write(m, pk, a.yes)
        elif a.cmd == 'mode':
            v, p1, p2 = MODES[a.name]
            p1 = p1 if a.p1 is None else a.p1
            p2 = p2 if a.p2 is None else a.p2
            if not (0 <= p1 <= 255 and 0 <= p2 <= 255):
                sys.exit('parameters must be 0..255')
            confirm_and_write(m, [packet(0x0D, v, p1, p2)], a.yes)
        elif a.cmd == 'palette':
            page = 0 if a.table == 'cycle' else 1
            new = b''.join(bytes.fromhex(c.lstrip('#')) for c in a.colors)
            if len(new) != 24:
                sys.exit('colours must be RRGGBB')
            old = m.read(0x90, page)[1][:32]
            confirm_and_write_block(m, 0x10, page, 0x18, old, new.ljust(32, b'\x00'), a.yes)
        elif a.cmd == 'button':
            if a.button == 'left' and not a.force:
                sys.exit('refusing to remap the left button without --force')
            old = m.read(0x92, 1)[1][:64]
            if len(old) != 64 or old[:4] != bytes.fromhex('0100f000'):
                sys.exit(f'unexpected button map on the mouse, not writing: {old.hex()}')
            new = bytearray(old)
            t, code = ACTIONS[a.action]
            s = (SLOTS[a.button] - 1) * 4
            new[s:s + 4] = bytes((t, 0, code, 0))
            confirm_and_write_block(m, 0x12, 1, 0x40, old, bytes(new), a.yes)
        elif a.cmd == 'restore-lighting':
            cfg = json.load(open(a.file))
            col = bytes.fromhex(cfg['8c:0']['reply'])[:3]
            md = bytes.fromhex(cfg['8d:0']['reply'])[:3]
            confirm_and_write(m, [packet(0x0C, *col), packet(0x0D, *md)], a.yes)
        elif a.cmd == 'restore-buttons':
            new = bytes.fromhex(json.load(open(a.file))['92:1']['data'])[:64]
            if len(new) != 64 or new[:4] != bytes.fromhex('0100f000'):
                sys.exit('backup has no valid button map')
            confirm_and_write_block(m, 0x12, 1, 0x40, m.read(0x92, 1)[1][:64], new, a.yes)
    finally:
        m.close()


if __name__ == '__main__':
    main()
