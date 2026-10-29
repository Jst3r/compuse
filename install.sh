#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Computer Use MCP Server Setup ==="

# System dependencies
echo ""
echo "--- Checking system tools ---"

NEED_INSTALL=()

if ! command -v spectacle &>/dev/null && ! command -v grim &>/dev/null && ! command -v scrot &>/dev/null; then
    echo "WARNING: No screenshot tool found."
    echo "  KDE:    sudo pacman -S spectacle"
    echo "  Sway:   sudo pacman -S grim slurp"
    echo "  X11:    sudo pacman -S scrot"
fi

if ! command -v ydotool &>/dev/null && ! command -v xdotool &>/dev/null; then
    echo "WARNING: No input automation tool found."
    echo "  Wayland: sudo pacman -S ydotool"
    echo "  X11:     sudo pacman -S xdotool"
    echo ""
    echo "For ydotool on Wayland, also run:"
    echo "  sudo systemctl enable --now ydotool"
    echo "  sudo usermod -aG input $USER"
    echo "  (then log out and back in)"
fi

# Python venv
echo ""
echo "--- Setting up Python venv ---"
if [ ! -d "$DIR/.venv" ]; then
    python3 -m venv "$DIR/.venv"
fi
source "$DIR/.venv/bin/activate"
pip install -q -r "$DIR/requirements.txt" 2>/dev/null || pip install -q "$DIR"

echo ""
echo "--- Setup complete ---"
echo ""
echo "Add to your Claude Code MCP config (.mcp.json in project root or ~/.claude/.mcp.json):"
echo ""
cat <<JSONEOF
{
  "mcpServers": {
    "compuse": {
      "command": "$DIR/.venv/bin/python",
      "args": ["$DIR/server.py"]
    }
  }
}
JSONEOF
echo ""
echo "Then restart Claude Code."
