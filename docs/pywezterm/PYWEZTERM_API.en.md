# pywezterm API

Python bindings for the wezterm terminal engine. Five classes: `Terminal` (terminal model), `Pty` (pseudo-terminal),
`Surface` (incremental rendering surface), `Mux` (multi-pane multiplexing), `ConsoleInput` (Windows console input).

```python
import pywezterm
pywezterm.version()          # '0.1.0'
```

| Class / Function | Responsibility |
|---|---|
| `Terminal` | Pure software terminal: feed bytes → parse VT → query state / snapshot / encode input |
| `Pty`      | Real subprocess + pseudo-terminal (ConPTY / openpty) |
| `Surface`  | Grid → incremental ANSI byte stream |
| `Mux`      | Multiple panes (each with Pty + Terminal) → composite single-frame incremental output |
| `ConsoleInput` | Capture host console key/mouse/resize events (Windows only) |

---

## 1. General Conventions

**Coordinates**: All 0-based, `(x=column, y=row)`.

**Cell tuple** (cells returned by `snapshot` / `scrollback` / `logical_lines`):

```python
(col, ch, fg, bg, bold, italic, underline, reverse, strike, width)
# Example: (2, 'c', 'p1', 'default', False, False, False, False, False, 1)
```
- `ch == ""` indicates continuation cell for wide characters (wide characters occupy 2 cells, only the first cell contains the character)
- `width` is display width: CJK/emoji = 2, others = 1

**Color strings**: `"default"` | `"p0"`…`"p15"` (ANSI palette) | `"#rrggbb"`

**Modifier keys** (`mods` parameter, bitwise OR):

```python
SHIFT, ALT, CTRL = 2, 4, 8
t.key_down("c", CTRL)              # -> b'\x03'
t.key_down("a", SHIFT | CTRL)      # -> b'\x01'
```

**Key names**:
`Up Down Left Right Home End Insert Delete PageUp PageDown Backspace Tab Enter Esc Space`,
`F1`…`F24`, or any single character (e.g., `"a"`, `"Z"`).

**Mouse**: `kind ∈ {"press","release","move"}`,
`button ∈ {"left","middle","right","wheel_up","wheel_down","none"}`.

**Byte destination**: `key_down` / `key_up` / `mouse` directly return encoded bytes for this call;
`send_paste` and terminal-generated responses (DSR, DECACK, etc.) remain in internal buffer,
retrieve with `drain_written()`.

---

## 2. Terminal

### 2.1 Offline parsing: feed → read

```python
t = pywezterm.Terminal(cols=80, rows=24, scrollback=10000)

t.feed(b"hello \x1b[31mred\x1b[0m\r\n")   # Feed raw VT bytes

t.text()          # 'hello red'          Visible area plain text, lines joined with \n, trailing whitespace stripped
t.cursor()        # (row, col, visible)  0-based
t.snapshot()      # [[cell, ...], ...]   Each row is a cell list (with styles)
t.snapshot_lines()# [(wrapped, cells)]   wrapped=True indicates this line is continued by next line
t.scrollback()    # History area cell grid (excluding visible area)
t.scrollback_count()

t.resize(120, 30) # Change columns/rows
t.reset()         # RIS: clear screen + clear scrollback + reset
t.clear_scrollback()
```

### 2.2 Incremental reading (for rendering/sync)

```python
base = t.current_seqno()
t.feed(b"more output\r\n")
dirty = t.changed_stable_rows(base)   # Only these stable rows changed
# Only need to redraw dirty rows

t.logical_lines()
# [(first_stable, last_stable, cells), ...]  Reassembles wraps across physical lines into logical lines
```

### 2.3 Input encoding (no write to pty, returns bytes)

```python
t.feed(b"\x1b[?1h")                    # Apply cursor key mode
t.key_down("Up", 0)                    # -> b'\x1bOA' (normal mode would be b'\x1b[A')
t.key_up("Up", 0)                      # -> b'' (xterm mode has no key-up sequence, normal)
t.key_down("c", CTRL)                  # -> b'\x03'

t.feed(b"\x1b[?1000h\x1b[?1006h")      # Enable mouse reporting + SGR
t.mouse(5, 3)                          # -> b'\x1b[<0;6;4M'  (x,y 0-based → sequence 1-based)
t.mouse(5, 3, kind="release")          # Ending 'm'
t.mouse(5, 3, button="wheel_up")

t.feed(b"\x1b[?2004h")                  # Enable bracketed paste
t.send_paste("hi")                     # Automatically wrapped with 200~/201~
t.drain_written()                      # -> b'\x1b[200~hi\x1b[201~', write back to pty
```

### 2.4 Selection (coordinates = stable row + column, spans scrollback, doesn't change with view scrolling)

```python
t.selection_set(anchor_row, anchor_col, end_row, end_col)   # Region (order can be reversed)
t.selection_select_word(row, col)      # Double-click to select word; no selection if on whitespace
t.selection_select_line(row, col)      # Triple-click to select line (including trailing \n)
t.selection_text()                     # 'abc\ndef'
t.selection_active()                   # bool
t.selection_clear()
```

### 2.5 Mode / metadata queries

```python
t.get_keyboard_encoding()   # 'xterm' | 'csi-u' | 'win32' | 'kitty'
t.is_alt_screen_active()    # DECSET 1049
t.is_mouse_grabbed()        # DECSET 1000/1002/1003
t.get_mouse_encoding()      # (mode, sgr)  mode ∈ {0,1000,1002,1003}
t.bracketed_paste_enabled() # DECSET 2004
t.get_title()               # str (OSC 0/2)
t.get_current_dir()         # str | None (OSC 7)
t.get_progress()            # ('none'|'percentage'|'error'|'indeterminate', int|None)
t.get_semantic_zones()      # [(y0, x0, y1, x1, 'prompt'|'input'|'output'), ...]  OSC 133
t.mode_restore_seq()        # str: DECSET sequence that can be fed directly to restore current mode
t.focus_changed(True)       # Report focus (DECSET 1004)
```

### 2.6 Full-screen output

```python
t.render_ansi(include_cursor=True)   # str: Full-screen ANSI (CUP + SGR + \x1b[K)
t.render_scrollback(keep_ansi=False)  # str: History area text / text with SGR
t.render_svg(compression_level=0)     # str: 0=as-is, >=1 compressed
t.render_image(scale=1.0, fmt="png")  # bytes: png | jpg | jpeg | bmp (8x17 pixels/cell × scale)
```

### 2.7 Callbacks

```python
t.set_clipboard_callback(lambda sel, data: ...)   # OSC 52, sel ∈ {'clipboard','primary'}, data can be None
t.set_download_callback(lambda name, data: ...)   # OSC 8 / hyperlink download, data: bytes
t.set_device_control_callback(lambda info: ...)   # DCS, info is str
t.set_notification_callback(lambda info: ...)     # Bell/alarm etc., info is str
t.make_all_lines_dirty()                          # Mark all lines changed (full redraw when selection highlight invalid)
```
Callback exceptions are caught and printed, won't interrupt terminal.

### 2.8 Quick reference

```
Terminal(cols=80, rows=24, scrollback=10000)
feed(data) · resize(cols, rows) · reset() · clear_scrollback() · focus_changed(b)
text() · cursor() · snapshot() · snapshot_lines() · scrollback() · scrollback_count()
logical_lines() · current_seqno() · changed_stable_rows(since) · make_all_lines_dirty()
scroll(delta) · scroll_to_bottom()
key_down(key, mods)->bytes · key_up(key, mods)->bytes
mouse(x, y, kind='press', button='left', mods=0)->bytes · send_paste(text) · drain_written()->bytes
selection_set(r0,c0,r1,c1) · selection_select_word(r,c) · selection_select_line(r,c)
selection_text() · selection_active() · selection_clear()
get_keyboard_encoding() · is_alt_screen_active() · is_mouse_grabbed() · get_mouse_encoding()
bracketed_paste_enabled() · get_title() · get_current_dir() · get_progress()
get_semantic_zones() · mode_restore_seq()
render_ansi(include_cursor) · render_scrollback(keep_ansi) · render_svg(level) · render_image(scale, fmt)
set_clipboard_callback(cb) · set_download_callback(cb) · set_device_control_callback(cb) · set_notification_callback(cb)
```

---

## 3. Pty

### 3.1 Manual closed loop (read → feed terminal → write back response)

```python
p = pywezterm.Pty(cols=80, rows=24)
t = pywezterm.Terminal(cols=80, rows=24)
p.spawn([r"C:\Windows\System32\cmd.exe", "/c", "echo hi"])

while True:
    chunk = p.read(4096, timeout=0.2)   # bytes; timeout/EOF -> b''
    if chunk:
        t.feed(chunk)
        resp = t.drain_written()        # Child process DSR etc. queries must be written back, otherwise it will hang
        if resp:
            p.write(resp)
    elif p.try_wait() is not None:      # Exited, continue draining to EOF
        ...
```

### 3.2 Common operations

```python
pid, handle = p.spawn(["/bin/sh", "-c", "sleep 10"],
                      cwd="/tmp",
                      env={"PATH": "/usr/bin"})    # env is override, rest inherited from current process
# Windows and child program parses command line itself (e.g., cmd.exe /c), preserve original quote semantics:
p.spawn([r"C:\Windows\System32\cmd.exe", "/c", "echo \"a b\""],
        raw_cmdline=r'cmd.exe /c echo "a b"')
# Windows: put the child into a job object at creation time (PROC_THREAD_ATTRIBUTE_JOB_LIST).
# Assigning the job after creation instead leaves a window in which whatever the child
# forks first escapes the job.  The caller creates and owns the job handle.
p.spawn([r"C:\Windows\System32\cmd.exe"], job_handle=job)

p.resize(100, 30);  p.get_size()      # (cols, rows)
p.write(b"dir\r\n")
p.child_pid();     p.child_handle()   # Windows process handle
p.hpcon()                             # Windows ConPTY handle
p.try_wait()                          # Exit code | None (running)
p.kill()
p.buffered_bytes()                    # Bytes pending in read buffer
p.close()                             # Idempotent; after close read() always b'', get_size()==(0,0)
```

### 3.3 Quick reference

```
Pty(cols=80, rows=24)
spawn(argv, cwd=None, env=None, raw_cmdline=None, job_handle=None) -> (pid, handle)
read(n=65536, timeout=None) -> bytes · write(data) · resize(cols, rows) · get_size() -> (cols, rows)
try_wait() -> int|None · kill() · close() · buffered_bytes() -> int
child_pid() -> int|None · child_handle() -> int|None · hpcon() -> int|None
```

---

## 4. Surface (grid → incremental ANSI bytes)

```python
s = pywezterm.Surface(cols=80, rows=24)

s.set_cell(0, 0, "Hi", fg="p1", bg="#000000", bold=True)   # Optional styles all default
seq, frame = s.get_changes_bytes(0)      # First frame: full
# ... draw full screen ...
seq, frame = s.get_changes_bytes(seq)    # Afterwards only contains changes; no change then frame == b''

s.repaint_bytes()                        # Force full = get_changes_bytes(0)
s.resize(100, 30)                        # Size change → next frame full
s.clear()
s.dimensions()                           # (cols, rows)
s.current_seqno()
```

Output is TrueColor ANSI; can write directly to real terminal. `set_cell`'s `text` can be multi-character (e.g., `"Hello"`).

```
Surface(cols=80, rows=24)
set_cell(x, y, text, fg='default', bg='default', bold=False, italic=False,
         underline=False, reverse=False, strike=False)
get_changes_bytes(since_seqno) -> (seq, bytes) · repaint_bytes() -> (seq, bytes)
resize(cols, rows) · clear() · dimensions() · current_seqno()
```

---

## 5. Mux (multi-pane host main loop)

**Convention**: Layout only supports 2 panes, left-right split; `set_output_callback` must be set **before**
`add_pane` (already created panes won't change callback); `render()` requires at least one pane.

```python
m = pywezterm.Mux(cols=80, rows=24)

# ---- Main loop: set callback first, then create pane ----
def on_output():                            # Called when any pane has new output (no args)
    frame, row, col, visible = m.render()   # frame: incremental ANSI bytes
    sys.stdout.buffer.write(frame); sys.stdout.buffer.flush()
    # row/col are focus cursor 0-based full-screen coordinates; CUP in frame is 1-based

m.set_output_callback(on_output)            # or None to clear

a = m.add_pane(["/bin/sh"])                 # pane_id (0-based); second onwards each half
b = m.add_pane([r"C:\Windows\System32\cmd.exe"])
m.pane_rects()                              # [(x,y,w,h), ...]

# ---- Input routing: focus version + explicit pane version ----
m.set_focus(b)
m.key_down("c", CTRL)                    # Send to focus pane, return encoded bytes (already sent to pty)
m.pane_key_down(a, "Enter", 0)
m.pane_write(a, b"ls\r\n")               # Raw bytes
m.pane_send_paste(a, "text")
m.mouse(x, y)                            # Full-screen coordinates → hit pane → convert to pane internal coordinates
m.pane_at(x, y)                          # pane_id | None (separator/status bar → None)
m.scroll(10); m.pane_scroll(a, 10); m.pane_scroll_to_bottom(a)

# ---- Layout ----
m.set_sep(True)                          # Draw separator line between two panes
m.set_split_col(50)                      # Specify split column; None = midpoint
m.set_status_rows(1)                     # Reserve status bar rows at bottom
m.set_status("STATUS_BAR_X")             # Status bar text
m.resize(120, 40); m.force_repaint()     # Force next frame full
m.pane_resize(a, 60, 40)                 # Single pane size

# ---- Queries ----
m.pane_text(a)                           # Visible area plain text
m.pane_cursor(a)                         # (row, col, visible), pane internal 0-based
m.pane_try_wait(a)                       # Exit code | None
m.pane_is_mouse_grabbed(a)
m.pane_take_output(a)                    # Take and clear subprocess raw output (for recording)
m.pane_output_len(a)

# ---- Selection (full-screen coordinates) ----
m.pane_selection_set(a, x0, y0, x1, y1)
m.pane_selection_select_word(a, x, y)
m.pane_selection_select_line(a, x, y)
m.pane_selection_text(a); m.pane_selection_active(a); m.pane_selection_clear(a)
m.set_focus_selection_callback(lambda sel, data: ...)   # OSC 52 (applies to currently existing panes)

m.close_pane(a)                          # Close single (idempotent)
m.close()                                # Close all subprocesses
```

```
Mux(cols=80, rows=24)
add_pane(argv, cwd=None, env=None) -> pane_id · close_pane(id) · close()
pane_rects() · pane_count() · dimensions() · focused() · set_focus(id) · pane_at(x, y)
render() -> (bytes, row, col, visible) · resize(cols, rows) · force_repaint()
set_sep(sep=True) · set_split_col(col|None) · set_status_rows(n) · set_status(text)
key_down(key, mods) · key_up(key, mods) · mouse(x, y, kind='press', button='left', mods=0)
scroll(delta) · scroll_to_bottom() · send_paste(text) · set_output_callback(cb|None)
pane_write(id, data) · pane_key_down(id, key, mods) · pane_key_up(id, key, mods)
pane_mouse(id, x, y, ...) · pane_send_paste(id, text)
pane_text(id) · pane_cursor(id) · pane_is_mouse_grabbed(id) · pane_try_wait(id)
pane_resize(id, cols, rows) · pane_scroll(id, delta) · pane_scroll_to_bottom(id)
pane_take_output(id) · pane_output_len(id)
pane_selection_set(id, x0, y0, x1, y1) · pane_selection_select_word(id, x, y)
pane_selection_select_line(id, x, y) · pane_selection_text(id)
pane_selection_active(id) · pane_selection_clear(id) · set_focus_selection_callback(cb)
```

---

## 6. ConsoleInput (Windows)

Construction takes over console input/output mode and code page, restored on `restore()` (or object destruction).
Event reading is non-blocking: first `wait_input(ms)` to wait, then `read_inputs()` to get all.

```python
ci = pywezterm.ConsoleInput()      # mux = pywezterm.Mux(...) etc. host object
try:
    while True:
        if not ci.wait_input(100):
            continue
        for ev in ci.read_inputs():
            if ev[0] == "key":
                _, key, mods, down = ev           # ('key', 'Up', 0, True)
                if down:
                    mux.key_down(key, mods)
            elif ev[0] == "mouse":
                _, x, y, kind, button, mods = ev  # ('mouse', 12, 4, 'press', 'left', 0)
                mux.mouse(x, y, kind, button, mods)
            elif ev[0] == "resize":
                cols, rows = ci.size()            # Get size immediately after ('resize',)
                mux.resize(cols, rows)
finally:
    ci.restore()
```

```
ConsoleInput()
wait_input(ms) -> bool · read_inputs() -> list[tuple] · size() -> (cols, rows) · restore()
```

---

## 7. Module functions

```python
pywezterm.version()                       # '0.1.0'
pywezterm.cursor_seq(row, col, visible)   # '\x1b[r+1;c+1H' + '\x1b[?25h' / '\x1b[?25l'
pywezterm.clipboard_read()  -> str        # Windows; returns '' if no content
pywezterm.clipboard_write(text)           # Windows; empty string is no-op
```

---

## 8. Common recipes

**Terminal emulation without subprocess** (parse logs/test escape sequences)
```python
t = pywezterm.Terminal(120, 40)
t.feed(data)
text, snap, zones = t.text(), t.snapshot(), t.get_semantic_zones()
```

**Subprocess interactive driver**
```python
p, t = pywezterm.Pty(80, 24), pywezterm.Terminal(80, 24)
p.spawn(argv)
def pump():
    b = p.read(65536, timeout=0.1)
    if b:
        t.feed(b); p.write(t.drain_written())
def send(keys):                 # Type
    for k, mods in keys: p.write(t.key_down(k, mods)); p.write(t.key_up(k, mods))
```

**Screenshot / export**
```python
open("shot.png", "wb").write(t.render_image(scale=2, fmt="png"))
open("shot.svg", "w", encoding="utf-8").write(t.render_svg(1))
ansi = t.render_ansi(include_cursor=True)   # Can write directly to real terminal
```

**Full-screen differential rendering**
```python
base = t.current_seqno()
...
for row in t.changed_stable_rows(base):
    ...   # Only redraw these rows
```
