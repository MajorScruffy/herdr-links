# herdr-links

A herdr plugin that opens a small, focused panel above an agent pane and lists the links from that agent's current chat session, newest first. Each row is a clickable OSC 8 link.

## Install

```sh
herdr plugin install MajorScruffy/herdr-links
```

Or, from a local clone:

```sh
git clone https://github.com/MajorScruffy/herdr-links.git
herdr plugin link ./herdr-links
```

Bind the toggle action in `~/.config/herdr/config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "herdr-links.toggle"
description = "toggle session links panel"
```

The default herdr prefix is `Ctrl+B`. Run the toggle with the agent pane focused; the panel opens above it and takes focus. Running it again, from the agent pane or from the panel, closes the panel.

## Panel keys

| Key | Action |
|---|---|
| `↑`/`↓`, `k`/`j` | Select |
| `Enter`, `o` | Open in the browser |
| `c`, `y` | Copy to the clipboard |
| `t` | Include links that appear only in tool calls and tool output (off by default) |
| `r` | Re-detect the session |
| `q`, `Esc` | Close the panel |

## Where links come from

- **Kiro CLI.** The plugin reads the session transcript under `~/.kiro/sessions/`. It finds the session through `~/.kiro/session-leases/*.lease`: the lease whose `pid` or `parentPid` is one of the pane's foreground processes, newest `acquiredAt` first. If you start a new session in the same pane, the panel switches to it within 5 seconds. Both transcript formats are read: `sessions/<hash>/sess_<id>/messages.jsonl` (current) and `sessions/cli/<uuid>.jsonl` (older).
- **Any other agent.** The plugin falls back to the pane's scrollback (`herdr pane read --source recent-unwrapped`), which only holds what is still on screen or in the buffer.

By default only user prompts and assistant replies are scanned. Reasoning text is skipped.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `HERDR_LINKS_ROWS` | `8` | Panel height in rows, header included |

## Debug

```sh
/usr/bin/python3 links.py dump --pane <pane_id> [--tools]
herdr plugin log list --plugin herdr-links
```

## Requirements

- herdr 0.9.0 or later
- Python 3.9 or later, standard library only (the macOS system Python works)
- macOS or Linux. Opening and copying use `open`/`pbcopy` on macOS and `xdg-open`/`wl-copy` on Linux.

## License

MIT
