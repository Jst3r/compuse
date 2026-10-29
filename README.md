# compuse

Computer Use MCP Server for Linux.

Computer use with Claude works great on macOS, but on Linux and Windows it's a different story. I wanted the same experience on my KDE Plasma desktop, so I built one.

Built for **KDE Plasma 6 on Wayland**, with automatic fallback to X11 tools.

## Tools

9 merged tools that cover everything — screenshots, OCR, input, window management, clipboard, notifications, and more.

| Tool | Actions | What it does |
|---|---|---|
| `screen` | capture, window, diff, pixel, wait_change | Screenshots, pixel color, screen change detection |
| `ocr` | read, find, describe, click | OCR text extraction, text search, smart clicking |
| `inp` | click, type, key, move, scroll, drag | Mouse and keyboard input |
| `window` | list, focus, active, move, resize, minimize, maximize, close | Window management |
| `clipboard` | get, set | Read/write system clipboard |
| `notify` | send, read | Desktop notifications |
| `system` | info, monitors, health, wait | System info, health checks, delays |
| `multi_action` | — | Chain multiple actions in one call |
| `file_dialog` | open, save, directory | Native file picker |

Tools are designed to minimize token usage — OCR tools send text instead of images (~95% token savings), and the merged tool architecture reduces schema overhead by ~70%.

## Backends

| Function | Wayland (KDE) | X11 Fallback |
|---|---|---|
| Screenshots | spectacle | scrot / import |
| Input | ydotool | xdotool |
| Windows | KWin scripting (qdbus6) | xdotool |
| Clipboard | wl-clipboard | xclip / xsel |
| OCR | tesseract | tesseract |

## Requirements

- Python 3.10+
- Linux desktop (KDE Plasma 6 recommended)

```bash
# KDE Wayland
sudo pacman -S spectacle ydotool wl-clipboard qt6-tools

# OCR
sudo pacman -S tesseract tesseract-data-eng

# File dialogs (optional)
sudo pacman -S kdialog  # or: zenity

# Enable ydotool
sudo systemctl enable --now ydotool
sudo usermod -aG input $USER
# Log out and back in after usermod

# X11 alternative
sudo pacman -S scrot xdotool xclip
```

> On Debian/Ubuntu, replace `pacman -S` with `apt install`.

## Setup

```bash
git clone https://github.com/Jst3r/compuse.git
cd compuse
chmod +x install.sh
./install.sh
```

Add to your Claude Code MCP config (`.mcp.json` in project root or `~/.claude/.mcp.json`):

```json
{
  "mcpServers": {
    "compuse": {
      "command": "/path/to/compuse/.venv/bin/python",
      "args": ["/path/to/compuse/server.py"]
    }
  }
}
```

Or register globally:

```bash
claude mcp add -s user compuse /path/to/compuse/.venv/bin/python /path/to/compuse/server.py
```

Then restart Claude Code.

## How It Works

- MCP server over **stdio** transport
- KDE window management via **KWin scripting** + `qdbus6`, output from **journalctl**
- Input via **ydotool** daemon (`/tmp/.ydotool_socket`)
- Screenshots via **spectacle**, compressed to JPEG (quality 45, max 600px)
- OCR via **tesseract** with 3x upscaling

**Warning:** This gives Claude full access to your screen, keyboard, and mouse. It can see everything on your desktop. Don't leave sensitive data visible while using it.

---

*Built by [@Jst3r](https://github.com/Jst3r)*
