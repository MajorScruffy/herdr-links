#!/usr/bin/env python3
"""herdr-links: a small panel that lists the links from an agent pane's chat session.

Subcommands:
  panel   Run the panel UI. The target agent pane comes from HERDR_LINKS_TARGET.
  toggle  Open the panel above the focused pane, or close it if it is already open.
  dump    Print the links for a pane (--pane ID) to stdout, for debugging.

Sources, in order of preference:
  1. Kiro CLI transcripts under ~/.kiro/sessions, matched to the pane through the
     session lease files in ~/.kiro/session-leases (lease pid/parentPid is one of
     the pane's foreground processes).
  2. The pane's terminal scrollback (`herdr pane read`) for any other agent.

Written for the macOS system Python (3.9), standard library only.
"""

import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "herdr-links"
KIRO_HOME = os.path.expanduser(os.environ.get("KIRO_HOME", "~/.kiro"))
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr-links")
PANEL_ROWS = int(os.environ.get("HERDR_LINKS_ROWS", "8"))

URL_RE = re.compile(r"https?://[^\s<>\"'`\[\](){}|\\^]+")
MD_LINK_RE = re.compile(r"\[([^\]\n]{1,160})\]\((https?://[^\s)]+)\)")
TRAILING = ".,;:!?*_~'\""


# --------------------------------------------------------------------------- herdr


def herdr(*args, timeout=10):
    proc = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "herdr %s failed" % args[0])
    out = proc.stdout.strip()
    if out.startswith("{"):
        return json.loads(out)
    return out


def pane_exists(pane_id):
    try:
        herdr("pane", "get", pane_id)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- links


def clean_url(url):
    url = url.rstrip(TRAILING)
    return url


def extract_links(text):
    """Return [(url, label_or_None)] in order of appearance."""
    labels = {}
    for m in MD_LINK_RE.finditer(text):
        label = m.group(1).strip().strip("`*_")
        if label and not label.startswith("http"):
            labels[clean_url(m.group(2))] = label
    out = []
    for m in URL_RE.finditer(text):
        url = clean_url(m.group(0))
        if len(url) > len("https://x.y"):
            out.append((url, labels.get(url)))
    return out


class LinkStore:
    """Deduplicated links; a repeated link moves to the newest position."""

    def __init__(self):
        self.links = {}  # url -> dict(label, kinds, seq)
        self.seq = 0

    def add(self, text, kind):
        changed = False
        for url, label in extract_links(text):
            self.seq += 1
            entry = self.links.get(url)
            if entry is None:
                self.links[url] = {"label": label, "kinds": {kind}, "seq": self.seq}
            else:
                entry["seq"] = self.seq
                entry["kinds"].add(kind)
                if label and not entry["label"]:
                    entry["label"] = label
            changed = True
        return changed

    def items(self, include_tools):
        rows = [
            (url, e) for url, e in self.links.items() if include_tools or "chat" in e["kinds"]
        ]
        rows.sort(key=lambda r: r[1]["seq"], reverse=True)
        return rows


# --------------------------------------------------------------------------- sources


def _texts_from_v2(record):
    """Kiro CLI >= 3.x: sessions/<hash>/sess_<id>/messages.jsonl."""
    p = record.get("payload") or {}
    t = p.get("type")
    if t == "user" and isinstance(p.get("content"), str):
        yield p["content"], "chat"
    elif t == "assistant" and p.get("operationType") != "Reasoning":
        if isinstance(p.get("content"), str):
            yield p["content"], "chat"
    elif t == "tool_call":
        yield json.dumps(p.get("args") or {}), "tool"
    elif t == "tool_result":
        c = p.get("content")
        yield (c if isinstance(c, str) else json.dumps(c)), "tool"


def _texts_from_v1(record):
    """Older Kiro CLI: sessions/cli/<uuid>.jsonl."""
    kind = record.get("kind")
    data = record.get("data") or {}
    if kind in ("Prompt", "AssistantMessage"):
        for item in data.get("content") or []:
            if item.get("kind") == "text" and isinstance(item.get("data"), str):
                yield item["data"], "chat"
            elif item.get("kind") == "toolUse":
                yield json.dumps(item.get("data") or {}), "tool"
    elif kind == "ToolResults":
        yield json.dumps(data), "tool"


class KiroTranscript:
    def __init__(self, path, session_name):
        self.path = path
        self.session_name = session_name
        self.offset = 0
        self.partial = b""
        self.v2 = path.endswith("messages.jsonl")

    def describe(self):
        name = self.session_name
        return "kiro " + (name[:13] if name.startswith("sess_") else name[:8])

    def poll(self, store):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:  # file rewritten
            self.offset, self.partial = 0, b""
        if size == self.offset:
            return False
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()
        changed = False
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            texts = _texts_from_v2(record) if self.v2 else _texts_from_v1(record)
            for text, kind in texts:
                changed = store.add(text, kind) or changed
        return changed


class PaneScrollback:
    def __init__(self, pane_id):
        self.pane_id = pane_id
        self.last = None

    def describe(self):
        return "scrollback " + self.pane_id

    def poll(self, store):
        try:
            out = herdr("pane", "read", self.pane_id, "--source", "recent-unwrapped", "--lines", "5000")
        except Exception:
            return False
        if isinstance(out, dict):
            out = json.dumps(out)
        if out == self.last:
            return False
        self.last = out
        return store.add(out, "chat")


def _kiro_session_path(name):
    if name.startswith("sess_"):
        hits = glob.glob(os.path.join(KIRO_HOME, "sessions", "*", name, "messages.jsonl"))
        return max(hits, key=os.path.getmtime) if hits else None
    uuid = name[4:40] if name.startswith("cli_") else name
    path = os.path.join(KIRO_HOME, "sessions", "cli", uuid + ".jsonl")
    return path if os.path.exists(path) else None


def resolve_kiro_session(pane_id):
    """Return (path, session_name) for the Kiro session running in pane_id, or None."""
    info = herdr("pane", "process-info", "--pane", pane_id)["result"]["process_info"]
    procs = info.get("foreground_processes") or []
    pids = {p.get("pid") for p in procs}
    resume_id = None
    for p in procs:
        argv = p.get("argv") or []
        if "--resume-id" in argv and argv.index("--resume-id") + 1 < len(argv):
            resume_id = argv[argv.index("--resume-id") + 1]

    best = None
    for lease in glob.glob(os.path.join(KIRO_HOME, "session-leases", "*.lease")):
        try:
            with open(lease) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if d.get("pid") in pids or d.get("parentPid") in pids:
            name = os.path.basename(lease)[: -len(".lease")]
            key = d.get("acquiredAt") or ""
            if best is None or key > best[0]:
                best = (key, name)

    for name in ([best[1]] if best else []) + ([resume_id] if resume_id else []):
        path = _kiro_session_path(name)
        if path:
            return path, name
    return None


def resolve_source(pane_id):
    try:
        found = resolve_kiro_session(pane_id)
    except Exception:
        found = None
    if found:
        return KiroTranscript(*found)
    return PaneScrollback(pane_id)


# --------------------------------------------------------------------------- panel UI


def osc8(url, text):
    return "\x1b]8;;%s\x1b\\%s\x1b]8;;\x1b\\" % (url, text)


def short_url(url):
    return re.sub(r"^https?://(www\.)?", "", url)


def fit(text, width):
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def open_url(url):
    cmd = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_url(url):
    cmd = ["pbcopy"] if sys.platform == "darwin" else ["wl-copy"]
    try:
        subprocess.run(cmd, input=url, text=True, timeout=5)
        return True
    except Exception:
        return False


class Panel:
    def __init__(self, target):
        self.target = target
        self.store = LinkStore()
        self.source = None
        self.include_tools = False
        self.selected = 0
        self.top = 0
        self.flash = ""
        self.flash_until = 0.0
        self.dirty = True

    def rebind(self):
        source = resolve_source(self.target)
        same = (
            self.source is not None
            and type(source) is type(self.source)
            and source.describe() == self.source.describe()
        )
        if not same:
            self.source = source
            self.store = LinkStore()
            self.selected = self.top = 0
            self.dirty = True

    def rows(self):
        return self.store.items(self.include_tools)

    def say(self, msg):
        self.flash, self.flash_until, self.dirty = msg, time.time() + 2.5, True

    def render(self):
        cols, lines = shutil.get_terminal_size((80, PANEL_ROWS))
        rows = self.rows()
        body = max(1, lines - 1)
        if rows:
            self.selected = min(self.selected, len(rows) - 1)
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + body:
            self.top = self.selected - body + 1

        hints = "↑↓ select  ⏎ open  c copy  t tools:%s  q close" % ("on" if self.include_tools else "off")
        if self.flash and time.time() < self.flash_until:
            hints = self.flash
        left = " Links (%d) · %s" % (len(rows), self.source.describe() if self.source else "…")
        gap = cols - len(left) - len(hints) - 1
        header = left + " " * max(1, gap) + hints if gap > 0 else left
        out = ["\x1b[H\x1b[2J", "\x1b[1;7m", fit(header, cols).ljust(cols), "\x1b[0m"]

        if not rows:
            out.append("\r\n\x1b[2m  No links in this session yet.\x1b[0m")
        for i in range(self.top, min(len(rows), self.top + body)):
            url, entry = rows[i]
            num = "%3d " % (i + 1)
            avail = cols - len(num) - 2
            label = entry["label"]
            if label:
                main = fit(label, avail)
                rest = avail - len(main) - 2
                tail = ("  \x1b[2m" + fit(short_url(url), rest) + "\x1b[22m") if rest > 8 else ""
            else:
                main, tail = fit(short_url(url), avail), ""
            mark = "›" if i == self.selected else " "
            style = "\x1b[7m" if i == self.selected else ""
            out.append("\r\n%s%s\x1b[2m%s\x1b[22m%s%s\x1b[0m" % (style, mark, num, osc8(url, main + tail), " "))
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.dirty = False

    def key(self, k):
        rows = self.rows()
        if k in ("q", "\x1b"):
            return False
        if k in ("\x1b[A", "k"):
            self.selected = max(0, self.selected - 1)
        elif k in ("\x1b[B", "j"):
            self.selected = min(max(0, len(rows) - 1), self.selected + 1)
        elif k == "g":
            self.selected = 0
        elif k == "G":
            self.selected = max(0, len(rows) - 1)
        elif k in ("\r", "\n", "o") and rows:
            open_url(rows[self.selected][0])
            self.say("opened " + short_url(rows[self.selected][0]))
        elif k in ("c", "y") and rows:
            ok = copy_url(rows[self.selected][0])
            self.say("copied" if ok else "copy failed")
        elif k == "t":
            self.include_tools = not self.include_tools
            self.selected = self.top = 0
        elif k == "r":
            self.source = None
            self.rebind()
        self.dirty = True
        return True


def read_key(fd):
    data = os.read(fd, 32).decode("utf-8", "replace")
    if data.startswith("\x1b[") or data.startswith("\x1bO"):
        return "\x1b[" + data[2:3]
    if data == "\x1b":
        return "\x1b"
    return data[:1]


def run_panel():
    target = os.environ.get("HERDR_LINKS_TARGET")
    if not target:
        try:
            ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
        except ValueError:
            ctx = {}
        target = ctx.get("focused_pane_id") or (ctx.get("pane") or {}).get("pane_id")
    if not target:
        print("herdr-links: no target pane (set HERDR_LINKS_TARGET)")
        time.sleep(5)
        return 1

    panel = Panel(target)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    signal.signal(signal.SIGWINCH, lambda *_: setattr(panel, "dirty", True))
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        tty.setcbreak(fd)
        last_poll = last_bind = 0.0
        missing = 0
        while True:
            now = time.time()
            if now - last_bind > 5:
                last_bind = now
                if pane_exists(target):
                    missing = 0
                    panel.rebind()
                else:
                    missing += 1
                    if missing >= 2:  # agent pane closed: close the panel too
                        return 0
            if panel.source and now - last_poll > (1 if isinstance(panel.source, KiroTranscript) else 3):
                last_poll = now
                if panel.source.poll(panel.store):
                    panel.dirty = True
            if panel.flash and now >= panel.flash_until:
                panel.flash = ""
                panel.dirty = True
            if panel.dirty:
                panel.render()
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready and not panel.key(read_key(fd)):
                return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


# --------------------------------------------------------------------------- toggle


def _state_path():
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, "panels.json")


def _load_state():
    try:
        with open(_state_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    with open(_state_path(), "w") as fh:
        json.dump(state, fh)


def _find_pane_id(obj):
    if isinstance(obj, dict):
        pane = obj.get("pane")
        if isinstance(pane, dict) and pane.get("pane_id"):
            return pane["pane_id"]
        for value in obj.values():
            found = _find_pane_id(value)
            if found:
                return found
    return None


def toggle():
    focused = os.environ.get("HERDR_PANE_ID")
    if not focused:
        print("herdr-links: no focused pane", file=sys.stderr)
        return 1
    state = {p: t for p, t in _load_state().items() if pane_exists(p)}  # panel -> target

    if focused in state:  # toggled from inside the panel
        herdr("pane", "close", focused)
        state.pop(focused)
        _save_state(state)
        return 0
    for panel_id, target in list(state.items()):
        if target == focused:
            herdr("pane", "close", panel_id)
            state.pop(panel_id)
            _save_state(state)
            return 0

    res = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID, "--entrypoint", "panel",
        "--placement", "split", "--target-pane", focused, "--direction", "down",
        "--env", "HERDR_LINKS_TARGET=" + focused,
        "--no-focus",
    )
    panel_id = _find_pane_id(res)
    if not panel_id:
        print("herdr-links: could not read the new pane id from: %s" % res, file=sys.stderr)
        return 1
    state[panel_id] = focused
    _save_state(state)
    place_on_top(panel_id, focused)
    return 0


def place_on_top(panel_id, target):
    """Move the panel above the target and shrink it to about PANEL_ROWS rows."""
    herdr("pane", "swap", "--source-pane", panel_id, "--target-pane", target)
    try:
        layout = herdr("pane", "layout", "--pane", panel_id)
        total = _rows_of(layout, target) + _rows_of(layout, panel_id)
    except Exception:
        total = 0
    if total > 0:
        # Each resize step moves the split by --amount (fraction of the parent).
        current = _rows_of(layout, panel_id)
        delta = (current - PANEL_ROWS) / float(total)
        if delta > 0:
            herdr("pane", "resize", "--pane", panel_id, "--direction", "up", "--amount", "%.3f" % delta)
    # Focus the panel, which now sits directly above the agent pane.
    try:
        herdr("pane", "focus", "--direction", "up", "--pane", target)
    except Exception:
        pass


def _rows_of(layout, pane_id):
    """Best-effort: find the row count for pane_id in a `pane layout` response."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("pane_id") == pane_id:
                for key in ("rows", "height", "h"):
                    if isinstance(obj.get(key), int):
                        found.append(obj[key])
                        return
                rect = obj.get("rect") or obj.get("area") or {}
                if isinstance(rect, dict) and isinstance(rect.get("height"), int):
                    found.append(rect["height"])
                    return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(layout)
    return found[0] if found else 0


# --------------------------------------------------------------------------- main


def dump(argv):
    pane = argv[argv.index("--pane") + 1] if "--pane" in argv else os.environ.get("HERDR_PANE_ID")
    tools = "--tools" in argv
    source = resolve_source(pane)
    store = LinkStore()
    source.poll(store)
    print("# %s" % source.describe())
    for url, entry in store.items(tools):
        print("%s\t%s" % (url, entry["label"] or ""))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "panel"
    if cmd == "panel":
        return run_panel()
    if cmd == "toggle":
        return toggle()
    if cmd == "dump":
        return dump(argv)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
