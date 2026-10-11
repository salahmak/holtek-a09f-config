#!/usr/bin/python3
"""GTK4/libadwaita configurator for Holtek 04d9:a09f gaming mice (Zelotes C-13, E-Signal LUOM G10, ...).

Uses the protocol implementation in a09fctl.py. Every write is shown (packets + changed bytes) and must be
confirmed, unless confirmation is switched off on the Device page.
"""
import json, os, sys, threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import a09fctl as g

APP_ID = 'io.github.salahmak.HoltekA09FConfig'
MODE_KEYS = list(g.MODES)
MODE_LABELS = [k.replace('-', ' ').title() for k in MODE_KEYS]
ACTION_KEYS = list(g.ACTIONS)
ACTION_LABELS = {
    'left': 'Left click', 'right': 'Right click', 'middle': 'Middle click',
    'back': 'Back', 'forward': 'Forward',
    'play-pause': 'Play / Pause', 'next-track': 'Next track', 'prev-track': 'Previous track',
    'stop': 'Stop', 'mute': 'Mute', 'volume-up': 'Volume up', 'volume-down': 'Volume down',
    'dpi-cycle': 'DPI cycle', 'dpi-up': 'DPI +', 'dpi-down': 'DPI −', 'disabled': 'Disabled',
}
BUTTON_LABELS = {'left': 'Left button', 'right': 'Right button', 'middle': 'Wheel click',
                 'forward': 'Forward (side)', 'back': 'Back (side)', 'dpi': 'DPI button'}
CSS = b"""
.mono { font-family: monospace; font-size: 0.9em; }
.swatch-row { padding: 12px; }
.changed { color: @accent_color; font-weight: bold; }
"""


def rgba_to_bytes(rgba):
    return bytes(round(max(0.0, min(1.0, c)) * 255) for c in (rgba.red, rgba.green, rgba.blue))


def bytes_to_rgba(b):
    r = Gdk.RGBA()
    r.parse('#' + bytes(b[:3]).hex())
    return r


def hexdump_with_marks(old, new):
    """Return (line, markline) pairs, 32 bytes per line, ^^ under changed bytes."""
    out = []
    for i in range(0, len(new), 32):
        line = new[i:i + 32].hex(' ')
        mark = ''.join('^^ ' if old[j] != new[j] else '   ' for j in range(i, min(i + 32, len(new)))).rstrip()
        out.append((line, mark))
    return out


class Device:
    """Serialises all mouse I/O on a worker thread; callbacks run on the GTK main loop."""

    def __init__(self):
        self.lock = threading.Lock()

    def run(self, fn, done, error):
        def work():
            with self.lock:
                try:
                    m = g.Mouse()
                    try:
                        res = fn(m)
                    finally:
                        m.close()
                except Exception as e:  # noqa: BLE001 — surfaced to the UI
                    GLib.idle_add(error, e)
                    return
            GLib.idle_add(done, res)
        threading.Thread(target=work, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='Holtek A09F Mouse')
        self.set_default_size(560, 720)
        self.dev = Device()
        self.cfg = None
        self.confirm_writes = True
        self.busy = False

        self.toasts = Adw.ToastOverlay()
        self.view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.refresh_btn = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Re-read from mouse')
        self.refresh_btn.connect('clicked', lambda *_: self.load())
        header.pack_start(self.refresh_btn)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)
        self.view.add_top_bar(header)

        self.root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.status = Adw.StatusPage(icon_name='input-mouse-symbolic', title='Connecting…')
        retry = Gtk.Button(label='Try again', halign=Gtk.Align.CENTER, css_classes=['pill', 'suggested-action'])
        retry.connect('clicked', lambda *_: self.load())
        self.status.set_child(retry)
        self.root.add_named(self.status, 'status')
        self.root.add_named(self.stack, 'main')
        self.view.set_content(self.root)
        self.toasts.set_child(self.view)
        self.set_content(self.toasts)

        self.build_lighting()
        self.build_colours()
        self.build_buttons()
        self.build_device()
        self.load()

    # ---------------------------------------------------------------- helpers
    def toast(self, text, timeout=3):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=timeout))

    def set_busy(self, busy):
        self.busy = busy
        self.spinner.set_visible(busy)
        self.refresh_btn.set_sensitive(not busy)
        for b in self.apply_buttons:
            b.set_sensitive(not busy and self.cfg is not None)

    def apply_button(self, label, cb):
        b = Gtk.Button(label=label, css_classes=['suggested-action'], valign=Gtk.Align.CENTER)
        b.connect('clicked', cb)
        self.apply_buttons.append(b)
        return b

    def on_error(self, e):
        self.set_busy(False)
        if isinstance(e, g.MouseNotFound) or isinstance(e, PermissionError) or self.cfg is None:
            hint = ('Install udev/99-holtek-a09f.rules (see README), then replug the mouse.'
                    if isinstance(e, PermissionError) else str(e))
            self.status.set_title('Mouse not available')
            self.status.set_description(hint)
            self.root.set_visible_child_name('status')
        else:
            self.toast(f'Error: {e}', 6)

    # ---------------------------------------------------------------- loading
    def load(self):
        self.set_busy(True)
        self.dev.run(g.dump, self.on_loaded, self.on_error)

    def on_loaded(self, cfg):
        self.cfg = cfg
        self.key = cfg['key'].encode()
        self.populating = True
        col = bytes.fromhex(cfg['8c:0']['reply'])[:3]
        md = bytes.fromhex(cfg['8d:0']['reply'])[:3]
        self.colour_btn.set_rgba(bytes_to_rgba(col))
        if md[0] in g.MODE_NAMES:
            self.mode_row.set_selected(MODE_KEYS.index(g.MODE_NAMES[md[0]]))
        self.p1_row.set_value(md[1])
        self.p2_row.set_value(md[2])
        for page, btns in ((0, self.cycle_btns), (1, self.dpi_btns)):
            data = bytes.fromhex(cfg[f'90:{page}']['data'])
            for i, b in enumerate(btns):
                b.set_rgba(bytes_to_rgba(data[i * 3:i * 3 + 3]))
        bmap = bytes.fromhex(cfg['92:1']['data'])
        for name, row in self.button_rows.items():
            s = (g.SLOTS[name] - 1) * 4
            entry = (bmap[s], bmap[s + 2])
            match = [k for k, v in g.ACTIONS.items() if v == entry]
            if match:
                row.set_selected(ACTION_KEYS.index(match[0]))
                row.set_subtitle('')
            else:
                row.set_subtitle(f'Unknown code {bmap[s]:02x} {bmap[s + 2]:02x} — choosing will replace it')
        dpi = bytes.fromhex(cfg['91:0']['data'])[:8]
        self.dpi_row.set_subtitle(' '.join(
            f'{b:02x}' + ('' if b & 0x80 == 0 else '·off') for b in dpi))
        for k, row in self.group_rows.items():
            row.set_subtitle(cfg[k]['reply'][:8])
        self.path_row.set_subtitle(g.find_config_hidraw() or '—')
        self.key_row.set_subtitle(cfg['key'])
        self.populating = False
        self.root.set_visible_child_name('main')
        self.set_busy(False)

    # ---------------------------------------------------------------- lighting
    def build_lighting(self):
        self.apply_buttons = []
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title='Wheel lighting',
                                   description='Changes are stored in the mouse immediately.')
        grp.set_header_suffix(self.apply_button('Apply', self.on_apply_lighting))
        self.mode_row = Adw.ComboRow(title='Effect', model=Gtk.StringList.new(MODE_LABELS))
        self.mode_row.connect('notify::selected', self.on_mode_changed)
        grp.add(self.mode_row)

        self.colour_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                                valign=Gtk.Align.CENTER)
        crow = Adw.ActionRow(title='Colour', subtitle='Used by Standard and single-colour effects')
        crow.add_suffix(self.colour_btn)
        crow.set_activatable_widget(self.colour_btn)
        grp.add(crow)
        page.add(grp)

        adv = Adw.PreferencesGroup(title='Effect parameters',
                                   description='Raw values; meaning depends on the effect (speed, brightness, length). '
                                               'Defaults come from captures of this model.')
        self.p1_row = Adw.SpinRow.new_with_range(0, 255, 1)
        self.p1_row.set_title('Parameter 1')
        self.p2_row = Adw.SpinRow.new_with_range(0, 255, 1)
        self.p2_row.set_title('Parameter 2')
        adv.add(self.p1_row)
        adv.add(self.p2_row)
        page.add(adv)
        self.populating = False
        self.stack.add_titled_with_icon(page, 'lighting', 'Lighting', 'weather-clear-night-symbolic')

    def on_mode_changed(self, *_):
        if self.populating or self.cfg is None:
            return
        key = MODE_KEYS[self.mode_row.get_selected()]
        md = bytes.fromhex(self.cfg['8d:0']['reply'])
        if md[0] == g.MODES[key][0]:          # back to the mouse's current mode: show its stored params
            p1, p2 = md[1], md[2]
        else:
            _, p1, p2 = g.MODES[key]
        self.p1_row.set_value(p1)
        self.p2_row.set_value(p2)

    def on_apply_lighting(self, *_):
        key = MODE_KEYS[self.mode_row.get_selected()]
        rgb = rgba_to_bytes(self.colour_btn.get_rgba())
        mode = (g.MODES[key][0], int(self.p1_row.get_value()), int(self.p2_row.get_value()))
        if (bytes.fromhex(self.cfg['8c:0']['reply'])[:3] == rgb
                and bytes.fromhex(self.cfg['8d:0']['reply'])[:3] == bytes(mode)):
            self.toast('Nothing changed')
            return
        pkts = [g.packet(0x0C, *rgb), g.packet(0x0D, *mode)]
        lines = [f'{p.hex(" ")}\n    wire {g.scramble(p, self.key).hex(" ")}' for p in pkts]
        body = (f'Colour #{rgb.hex()}, effect {MODE_LABELS[MODE_KEYS.index(key)]} '
                f'(params {mode[1]}, {mode[2]}).')

        def write(m):
            for p in pkts:
                m.send(p)
            return m.read(0x8C)[0][:3], m.read(0x8D)[0][:3]

        def done(res):
            col, md = res
            ok = col == rgb and md == bytes(mode)
            self.toast('Lighting applied' if ok else f'Read-back mismatch: {col.hex()} / {md.hex()}')
            self.load()
        self.confirm('Apply lighting?', body, lines, write, done)

    # ---------------------------------------------------------------- colours
    def build_colours(self):
        page = Adw.PreferencesPage()
        self.cycle_btns = self.palette_group(page, 'Cycle palette',
                                             'Colour table used by cycling effects (not by Neon).', 0)
        self.dpi_btns = self.palette_group(page, 'DPI stage colours',
                                           'Flashed when the DPI stage changes. Only stages 1–2 are enabled.', 1)
        self.stack.add_titled_with_icon(page, 'colours', 'Colours', 'applications-graphics-symbolic')

    def palette_group(self, page, title, desc, idx):
        grp = Adw.PreferencesGroup(title=title, description=desc)
        btns = []
        box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                          min_children_per_line=4, homogeneous=True, css_classes=['swatch-row'])
        for i in range(8):
            b = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                      tooltip_text=f'Slot {i + 1}')
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            v.append(b)
            v.append(Gtk.Label(label=str(i + 1), css_classes=['dim-label', 'caption']))
            box.append(v)
            btns.append(b)
        card = Gtk.ListBox(css_classes=['boxed-list'], selection_mode=Gtk.SelectionMode.NONE)
        card.append(box)
        grp.add(card)
        grp.set_header_suffix(self.apply_button('Apply', lambda *_: self.on_apply_palette(idx, btns)))
        page.add(grp)
        return btns

    def on_apply_palette(self, idx, btns):
        old = bytes.fromhex(self.cfg[f'90:{idx}']['data'])[:32]
        new = b''.join(rgba_to_bytes(b.get_rgba()) for b in btns).ljust(32, b'\x00')
        self.write_block_flow(0x10, idx, 0x18, old, new,
                              'Apply cycle palette?' if idx == 0 else 'Apply DPI stage colours?')

    # ---------------------------------------------------------------- buttons
    def build_buttons(self):
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title='Button mapping',
                                   description='Media actions are sent as standard media keys.')
        grp.set_header_suffix(self.apply_button('Apply', self.on_apply_buttons))
        self.button_rows = {}
        model = Gtk.StringList.new([ACTION_LABELS[k] for k in ACTION_KEYS])
        for name in g.SLOTS:
            row = Adw.ComboRow(title=BUTTON_LABELS[name], model=model)
            if name == 'left':
                row.set_sensitive(False)
                row.set_subtitle('Locked so the mouse always keeps a primary click')
            self.button_rows[name] = row
            grp.add(row)
        page.add(grp)
        self.stack.add_titled_with_icon(page, 'buttons', 'Buttons', 'input-mouse-symbolic')

    def on_apply_buttons(self, *_):
        old = bytes.fromhex(self.cfg['92:1']['data'])[:64]
        if old[:4] != bytes.fromhex('0100f000'):
            self.toast('Unexpected button map on the mouse — not writing')
            return
        new = bytearray(old)
        for name, row in self.button_rows.items():
            if name == 'left':
                continue
            t, code = g.ACTIONS[ACTION_KEYS[row.get_selected()]]
            s = (g.SLOTS[name] - 1) * 4
            if (old[s], old[s + 2]) != (t, code):
                new[s:s + 4] = bytes((t, 0, code, 0))
        self.write_block_flow(0x12, 1, 0x40, old, bytes(new), 'Apply button mapping?')

    # ---------------------------------------------------------------- device
    def build_device(self):
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title='Configuration')
        backup = Adw.ButtonRow(title='Back up to file', start_icon_name='document-save-symbolic')
        backup.connect('activated', self.on_backup)
        restore = Adw.ButtonRow(title='Restore from backup…', start_icon_name='document-revert-symbolic')
        restore.connect('activated', self.on_restore)
        grp.add(backup)
        grp.add(restore)
        sw = Adw.SwitchRow(title='Confirm every write',
                           subtitle='Show the exact packets before anything is sent', active=True)
        sw.connect('notify::active', lambda r, *_: setattr(self, 'confirm_writes', r.get_active()))
        grp.add(sw)
        page.add(grp)

        info = Adw.PreferencesGroup(title='Device', description='Read-only. Unknown groups are shown raw.')
        self.path_row = Adw.ActionRow(title='Config interface')
        info.add(self.path_row)
        self.key_row = Adw.ActionRow(title='Scramble key', subtitle='—', subtitle_selectable=True)
        info.add(self.key_row)
        info.add(Adw.ActionRow(title='Polling rate', subtitle='1000 Hz (measured)'))
        self.dpi_row = Adw.ActionRow(title='DPI table (raw)')
        info.add(self.dpi_row)
        self.group_rows = {}
        for k in ('81:0', '82:0', '83:0', '8b:0'):
            r = Adw.ActionRow(title=f'Group {k[:2]}', subtitle_selectable=True)
            r.add_css_class('property')
            self.group_rows[k] = r
            info.add(r)
        page.add(info)
        self.stack.add_titled_with_icon(page, 'device', 'Device', 'preferences-system-symbolic')

    def on_backup(self, *_):
        def done(cfg):
            f = g.save_backup(cfg)
            self.set_busy(False)
            self.toast(f'Saved {os.path.basename(f)}')
        self.set_busy(True)
        self.dev.run(g.dump, done, self.on_error)

    def on_restore(self, *_):
        os.makedirs(g.BACKUP_DIR, exist_ok=True)
        dlg = Gtk.FileDialog(title='Restore backup', initial_folder=Gio.File.new_for_path(g.BACKUP_DIR))
        flt = Gtk.FileFilter(name='Backups')
        flt.add_pattern('*.json')
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        dlg.open(self, None, self.on_restore_chosen)

    def on_restore_chosen(self, dlg, res):
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        try:
            b = json.load(open(f.get_path()))
            col = bytes.fromhex(b['8c:0']['reply'])[:3]
            md = bytes.fromhex(b['8d:0']['reply'])[:3]
            pal = [bytes.fromhex(b[f'90:{i}']['data'])[:32] for i in (0, 1)]
            bmap = bytes.fromhex(b['92:1']['data'])[:64]
            assert len(bmap) == 64 and bmap[:4] == bytes.fromhex('0100f000') and all(len(p) == 32 for p in pal)
        except Exception as e:  # noqa: BLE001
            self.toast(f'Not a valid backup: {e}', 6)
            return
        cur = self.cfg
        lines = [f'{g.packet(0x0C, *col).hex(" ")}   colour #{col.hex()}',
                 f'{g.packet(0x0D, *md).hex(" ")}   effect {g.MODE_NAMES.get(md[0], md[0])}']
        for name, cmd, page, ln, new, key in (('cycle palette', 0x10, 0, 0x18, pal[0], '90:0'),
                                               ('DPI colours', 0x10, 1, 0x18, pal[1], '90:1'),
                                               ('button map', 0x12, 1, 0x40, bmap, '92:1')):
            old = bytes.fromhex(cur[key]['data'])[:len(new)]
            lines.append(f'{g.packet(cmd, page, ln).hex(" ")}   {name}' + ('' if old != new else '  (unchanged, skipped)'))

        def write(m):
            m.send(g.packet(0x0C, *col))
            m.send(g.packet(0x0D, *md))
            ok = True
            for cmd, page, ln, new, key in ((0x10, 0, 0x18, pal[0], '90:0'), (0x10, 1, 0x18, pal[1], '90:1'),
                                            (0x12, 1, 0x40, bmap, '92:1')):
                if bytes.fromhex(cur[key]['data'])[:len(new)] != new:
                    ok &= m.write_block(cmd, page, ln, new) == new
            return ok

        def done(ok):
            self.toast('Backup restored' if ok else 'Restore finished with read-back mismatch', 5)
            self.load()
        self.confirm('Restore backup?', f'{os.path.basename(f.get_path())} — lighting, colours and buttons. '
                     'The DPI table and unknown groups are not touched.', lines, write, done)

    # ---------------------------------------------------------------- writing
    def write_block_flow(self, cmd, page, ln, old, new, heading):
        if old == new:
            self.toast('Nothing changed')
            return
        p = g.packet(cmd, page, ln)
        lines = [f'feature {p.hex(" ")}\n    wire {g.scramble(p, self.key).hex(" ")}']
        for line, mark in hexdump_with_marks(old, new):
            lines.append(f'data    {line}\n        {mark}')
        n = sum(1 for a, b in zip(old, new) if a != b)

        def done(back):
            self.toast('Written and verified' if back == new else 'Read-back mismatch!', 4)
            self.load()
        self.confirm(heading, f'{n} byte{"s" if n != 1 else ""} change (marked ^^).', lines,
                     lambda m: m.write_block(cmd, page, ln, new), done)

    def confirm(self, heading, body, lines, write, done):
        def go():
            self.set_busy(True)
            self.dev.run(write, done, self.on_error)
        if not self.confirm_writes:
            go()
            return
        dlg = Adw.AlertDialog(heading=heading, body=body)
        text = Gtk.Label(label='\n'.join(lines), xalign=0, selectable=True, wrap=True,
                         wrap_mode=Pango.WrapMode.CHAR, css_classes=['mono'])
        scroller = Gtk.ScrolledWindow(child=text, min_content_height=80, max_content_height=260,
                                      propagate_natural_height=True)
        dlg.set_extra_child(scroller)
        dlg.add_response('cancel', 'Cancel')
        dlg.add_response('write', 'Write to mouse')
        dlg.set_response_appearance('write', Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response('cancel')
        dlg.connect('response', lambda d, r: go() if r == 'write' else None)
        dlg.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = Window(self)
        win.present()


if __name__ == '__main__':
    sys.exit(App().run(sys.argv))
