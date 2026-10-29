#!/usr/bin/env python3
"""Computer Use MCP Server for Linux (KDE Wayland + X11 fallback)."""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer as FastMCP
from PIL import Image
import pytesseract

os.environ.setdefault("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")

mcp = FastMCP(
    "computer-use",
    version="0.5.0",
)

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def _which(name: str) -> str | None:
    return shutil.which(name)


def _detect_screenshot_backend() -> str:
    for backend in ("spectacle", "grim", "scrot", "import"):
        if _which(backend):
            return backend
    raise RuntimeError("No screenshot tool found. Install spectacle, grim, scrot, or imagemagick.")


def _detect_input_backend() -> str | None:
    if _which("ydotool"):
        return "ydotool"
    if _which("xdotool"):
        return "xdotool"
    return None


def _detect_clipboard_backend() -> str | None:
    if _which("wl-copy") and _which("wl-paste"):
        return "wl-clipboard"
    if _which("xclip"):
        return "xclip"
    if _which("xsel"):
        return "xsel"
    return None


def _detect_session_type() -> str:
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland":
        return "wayland"
    if session == "x11":
        return "x11"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


SCREENSHOT_BACKEND = _detect_screenshot_backend()
INPUT_BACKEND = _detect_input_backend()
CLIPBOARD_BACKEND = _detect_clipboard_backend()
SESSION_TYPE = _detect_session_type()

MAX_SCREENSHOT_DIM = 600

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], timeout: float = 10) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_kwin_script(js_code: str) -> str:
    """Run a KWin JavaScript snippet via qdbus6 (fire-and-forget)."""
    if not _which("qdbus6"):
        raise RuntimeError("qdbus6 not found — KWin scripting unavailable")
    with tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False) as f:
        f.write(js_code)
        script_path = f.name
    try:
        sid = _run(["qdbus6", "org.kde.KWin", "/Scripting",
                     "org.kde.kwin.Scripting.loadScript", script_path])
        sid = sid.strip()
        _run(["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}",
              "org.kde.kwin.Script.run"])
        time.sleep(0.15)
        try:
            _run(["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}",
                  "org.kde.kwin.Script.stop"])
        except Exception:
            pass
        return sid
    finally:
        Path(script_path).unlink(missing_ok=True)


def _kwin_script_with_output(js_code: str, timeout: float = 3.0) -> str:
    """Run a KWin script that uses console.log(MARKER + data) and capture the output
    from journalctl (Plasma 6)."""
    marker = f"COMPUSE_{uuid.uuid4().hex[:8]}"
    js_with_marker = js_code.replace("__MARKER__", marker)
    _run_kwin_script(js_with_marker)
    time.sleep(0.4)

    start = time.time()
    while time.time() - start < timeout:
        try:
            out = subprocess.check_output(
                ["journalctl", "-t", "kwin_wayland", "--since", "-10s",
                 "--no-pager", "-o", "cat"],
                text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            for line in out.strip().splitlines():
                if marker in line:
                    idx = line.find(marker) + len(marker)
                    return line[idx:].strip()
        except Exception:
            pass
        time.sleep(0.3)
    return ""


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def _kill_spectacle() -> None:
    """Kill any lingering spectacle processes."""
    try:
        subprocess.run(["pkill", "-9", "spectacle"], capture_output=True, timeout=3)
        time.sleep(0.3)
    except Exception:
        pass


def _capture_screenshot(region: str | None = None) -> str:
    """Capture screenshot, return base64-encoded JPEG."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name

    try:
        if SCREENSHOT_BACKEND == "spectacle":
            _kill_spectacle()
            cmd = ["spectacle", "-b", "-n", "-f", "-o", tmp]
            subprocess.run(cmd, check=True, timeout=15, capture_output=True)

        elif SCREENSHOT_BACKEND == "grim":
            cmd = ["grim"]
            if region:
                parts = region.replace(",", " ").replace("x", " ").split()
                if len(parts) == 4:
                    x, y, w, h = parts
                    cmd.extend(["-g", f"{x},{y} {w}x{h}"])
            cmd.append(tmp)
            subprocess.run(cmd, check=True, timeout=10, capture_output=True)

        elif SCREENSHOT_BACKEND == "scrot":
            subprocess.run(["scrot", tmp], check=True, timeout=10, capture_output=True)

        elif SCREENSHOT_BACKEND == "import":
            subprocess.run(["import", "-window", "root", tmp],
                           check=True, timeout=10, capture_output=True)

        if not Path(tmp).exists() or Path(tmp).stat().st_size < 100:
            raise RuntimeError(f"Screenshot file empty or missing after {SCREENSHOT_BACKEND}")

        img = Image.open(tmp)

        if region and SCREENSHOT_BACKEND in ("spectacle", "scrot", "import"):
            parts = region.replace(",", " ").replace("x", " ").split()
            if len(parts) == 4:
                rx, ry, rw, rh = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                img = img.crop((rx, ry, rx + rw, ry + rh))

        w, h = img.size
        if max(w, h) > MAX_SCREENSHOT_DIM:
            scale = MAX_SCREENSHOT_DIM / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        buf = io.BytesIO()
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=45)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    finally:
        Path(tmp).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _require_input_backend() -> str:
    global INPUT_BACKEND
    if INPUT_BACKEND is None:
        INPUT_BACKEND = _detect_input_backend()
    if INPUT_BACKEND is None:
        raise RuntimeError(
            "No input tool installed. Run: sudo pacman -Sy ydotool && "
            "sudo systemctl enable --now ydotool"
        )
    return INPUT_BACKEND


def _mouse_move(x: int, y: int) -> None:
    _require_input_backend()
    if INPUT_BACKEND == "ydotool":
        _run(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
    else:
        _run(["xdotool", "mousemove", str(x), str(y)])


def _mouse_click(button: str = "left", click_type: str = "single") -> None:
    _require_input_backend()
    btn_map_ydotool = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
    btn_map_xdotool = {"left": "1", "right": "3", "middle": "2"}

    if INPUT_BACKEND == "ydotool":
        btn = btn_map_ydotool.get(button, "0xC0")
        if click_type == "triple":
            _run(["ydotool", "click", btn, btn, btn])
        elif click_type == "double":
            _run(["ydotool", "click", btn, btn])
        else:
            _run(["ydotool", "click", btn])
    else:
        btn = btn_map_xdotool.get(button, "1")
        repeat = {"single": "1", "double": "2", "triple": "3"}.get(click_type, "1")
        if repeat != "1":
            _run(["xdotool", "click", "--repeat", repeat, "--delay", "80", btn])
        else:
            _run(["xdotool", "click", btn])


def _type_text(text: str, delay_ms: int = 12) -> None:
    _require_input_backend()
    if INPUT_BACKEND == "ydotool":
        _run(["ydotool", "type", "--delay", str(delay_ms), text])
    else:
        _run(["xdotool", "type", "--delay", str(delay_ms), text])


def _key_press(keys: str) -> None:
    _require_input_backend()
    if INPUT_BACKEND == "ydotool":
        _run(["ydotool", "key", keys])
    else:
        _run(["xdotool", "key", keys])


def _scroll(direction: str = "down", clicks: int = 3) -> None:
    _require_input_backend()
    if INPUT_BACKEND == "ydotool":
        if direction in ("up", "down"):
            amount = clicks if direction == "up" else -clicks
            _run(["ydotool", "mousemove", "--wheel", "--", "0", str(amount)])
        elif direction in ("left", "right"):
            amount = -clicks if direction == "left" else clicks
            _run(["ydotool", "mousemove", "--wheel", "--", str(amount), "0"])
    else:
        btn_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
        btn = btn_map.get(direction, "5")
        _run(["xdotool", "click", "--repeat", str(clicks), "--delay", "50", btn])


def _get_cursor_position() -> tuple[int, int]:
    if _which("xdotool"):
        out = _run(["xdotool", "getmouselocation"])
        parts = dict(p.split(":") for p in out.split() if ":" in p)
        return int(parts.get("x", 0)), int(parts.get("y", 0))
    return 0, 0


def _get_screen_size() -> tuple[int, int]:
    monitors = _get_monitors()
    if monitors:
        m = monitors[0]
        return m["width"], m["height"]
    return 1920, 1080


def _get_monitors() -> list[dict]:
    """Get all monitors with name, geometry, scale, and enabled status."""
    monitors = []
    if _which("kscreen-doctor"):
        try:
            out = subprocess.check_output(
                ["kscreen-doctor", "--outputs"], text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            current = {}
            for line in out.splitlines():
                line_clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                if line_clean.startswith("Output:"):
                    if current.get("name"):
                        monitors.append(current)
                    parts = line_clean.split()
                    current = {"name": parts[2] if len(parts) > 2 else "unknown",
                               "enabled": False, "x": 0, "y": 0,
                               "width": 0, "height": 0, "scale": 1.0}
                elif "enabled" in line_clean and current:
                    current["enabled"] = True
                elif "Geometry:" in line_clean and current:
                    geom = line_clean.split("Geometry:")[1].strip()
                    parts = geom.replace(",", " ").split()
                    if len(parts) >= 2:
                        xy = parts[0].split(",") if "," in parts[0] else parts[0].split()
                        wh = parts[-1].split("x")
                        if len(wh) == 2:
                            current["width"] = int(wh[0])
                            current["height"] = int(wh[1])
                        if len(xy) >= 2:
                            current["x"] = int(xy[0])
                            current["y"] = int(xy[1])
                        elif len(parts) >= 2 and "x" not in parts[0]:
                            current["x"] = int(parts[0].rstrip(","))
                            current["y"] = int(parts[1]) if len(parts) > 1 else 0
                elif "Scale:" in line_clean and current:
                    try:
                        current["scale"] = float(line_clean.split("Scale:")[1].strip())
                    except ValueError:
                        pass
            if current.get("name"):
                monitors.append(current)
            monitors = [m for m in monitors if m.get("enabled")]
        except Exception:
            pass

    if not monitors:
        try:
            out = subprocess.check_output(["xdpyinfo"], text=True, timeout=5,
                                          stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "dimensions:" in line:
                    dim = line.split("dimensions:")[1].strip().split()[0]
                    w, h = dim.split("x")
                    monitors.append({"name": "default", "enabled": True,
                                     "x": 0, "y": 0, "width": int(w), "height": int(h),
                                     "scale": 1.0})
                    break
        except Exception:
            pass

    return monitors


# ---------------------------------------------------------------------------
# Clipboard helpers
# ---------------------------------------------------------------------------

def _clipboard_get() -> str:
    if CLIPBOARD_BACKEND == "wl-clipboard":
        return _run(["wl-paste", "--no-newline"])
    elif CLIPBOARD_BACKEND == "xclip":
        return _run(["xclip", "-selection", "clipboard", "-o"])
    elif CLIPBOARD_BACKEND == "xsel":
        return _run(["xsel", "--clipboard", "--output"])
    raise RuntimeError("No clipboard tool. Install wl-clipboard or xclip.")


def _clipboard_set(text: str) -> None:
    if CLIPBOARD_BACKEND == "wl-clipboard":
        subprocess.run(["wl-copy", text], check=True, timeout=5)
    elif CLIPBOARD_BACKEND == "xclip":
        subprocess.run(["xclip", "-selection", "clipboard"],
                        input=text, text=True, check=True, timeout=5)
    elif CLIPBOARD_BACKEND == "xsel":
        subprocess.run(["xsel", "--clipboard", "--input"],
                        input=text, text=True, check=True, timeout=5)
    else:
        raise RuntimeError("No clipboard tool. Install wl-clipboard or xclip.")


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _read_notifications(count: int = 10, since: str = "1h") -> list[dict]:
    """Read recent desktop notifications from journalctl user session logs."""
    notifications = []

    try:
        out = subprocess.check_output(
            ["journalctl", "--user", "--since", f"-{since}",
             "--no-pager", "-o", "json", "-n", str(count * 5)],
            text=True, timeout=10, stderr=subprocess.DEVNULL
        )
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                msg = entry.get("MESSAGE", "")
                unit = entry.get("_SYSTEMD_UNIT", "")
                ident = entry.get("SYSLOG_IDENTIFIER", "")
                if any(kw in msg.lower() for kw in ("notification", "notify")) or \
                   "notif" in unit.lower() or "notif" in ident.lower():
                    notifications.append({
                        "timestamp": entry.get("__REALTIME_TIMESTAMP", ""),
                        "source": ident or unit,
                        "message": msg[:500],
                    })
            except json.JSONDecodeError:
                pass
        notifications = notifications[:count]
    except Exception:
        pass

    return notifications


def _send_notification(summary: str, body: str = "", app: str = "compuse") -> bool:
    """Send a desktop notification via notify-send."""
    cmd = ["notify-send"]
    if app:
        cmd.extend(["--app-name", app])
    cmd.append(summary)
    if body:
        cmd.append(body)
    try:
        subprocess.run(cmd, check=True, timeout=5, capture_output=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Health check helpers
# ---------------------------------------------------------------------------

def _check_backend_health() -> dict:
    """Verify each backend is actually functional, not just installed."""
    health = {}

    if SCREENSHOT_BACKEND == "spectacle":
        try:
            _kill_spectacle()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp = f.name
            subprocess.run(["spectacle", "-b", "-n", "-f", "-o", tmp],
                           check=True, timeout=10, capture_output=True)
            ok = Path(tmp).exists() and Path(tmp).stat().st_size > 100
            Path(tmp).unlink(missing_ok=True)
            health["screenshot"] = "ok" if ok else "fail: empty output"
        except Exception as e:
            health["screenshot"] = f"fail: {e}"
    else:
        health["screenshot"] = f"detected: {SCREENSHOT_BACKEND}"

    if INPUT_BACKEND == "ydotool":
        sock = Path(os.environ.get("YDOTOOL_SOCKET", "/tmp/.ydotool_socket"))
        if sock.exists():
            health["input"] = "ok (ydotool socket exists)"
        else:
            health["input"] = f"warn: ydotool socket missing at {sock}"
    elif INPUT_BACKEND == "xdotool":
        health["input"] = "ok (xdotool)"
    else:
        health["input"] = "fail: no input backend"

    if CLIPBOARD_BACKEND == "wl-clipboard":
        try:
            subprocess.run(["wl-paste", "--no-newline"],
                           capture_output=True, text=True, timeout=3)
            health["clipboard"] = "ok (wl-clipboard responsive)"
        except subprocess.TimeoutExpired:
            health["clipboard"] = "warn: wl-paste timed out (no focused Wayland client?)"
        except Exception as e:
            health["clipboard"] = f"fail: {e}"
    elif CLIPBOARD_BACKEND:
        health["clipboard"] = f"detected: {CLIPBOARD_BACKEND}"
    else:
        health["clipboard"] = "fail: no clipboard backend"

    if _which("qdbus6"):
        try:
            _run(["qdbus6", "org.kde.KWin", "/Scripting"])
            health["kwin"] = "ok"
        except Exception as e:
            health["kwin"] = f"fail: {e}"
    else:
        health["kwin"] = "unavailable"

    if _which("tesseract"):
        try:
            ver = subprocess.check_output(["tesseract", "--version"],
                                          text=True, timeout=5, stderr=subprocess.STDOUT).splitlines()[0]
            health["ocr"] = f"ok ({ver})"
        except Exception as e:
            health["ocr"] = f"fail: {e}"
    else:
        health["ocr"] = "unavailable (install tesseract-ocr)"

    return health


# ---------------------------------------------------------------------------
# Window helpers (KWin Wayland-aware)
# ---------------------------------------------------------------------------

def _kwin_list_windows() -> list[dict]:
    """List windows via KWin scripting (sees Wayland-native windows)."""
    js = """
    var wins = workspace.windowList();
    var result = [];
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        if (w.skipTaskbar || w.specialWindow) continue;
        result.push({
            resourceClass: w.resourceClass,
            caption: w.caption,
            x: w.frameGeometry.x,
            y: w.frameGeometry.y,
            width: w.frameGeometry.width,
            height: w.frameGeometry.height,
            active: (workspace.activeWindow === w),
            minimized: w.minimized,
            desktop: w.desktops.length > 0 ? w.desktops[0].name : "all"
        });
    }
    console.log("__MARKER__" + JSON.stringify(result));
    """
    raw = _kwin_script_with_output(js)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return []


def _kwin_focus_window(resource_class: str = "", caption: str = "") -> bool:
    """Focus a window via KWin scripting."""
    if resource_class:
        match = f'wins[i].resourceClass.toLowerCase() === "{resource_class.lower()}"'
    elif caption:
        safe_caption = caption.replace('"', '\\"')
        match = f'wins[i].caption.toLowerCase().indexOf("{safe_caption.lower()}") >= 0'
    else:
        return False

    js = f"""
    var wins = workspace.windowList();
    for (var i = 0; i < wins.length; i++) {{
        if ({match}) {{
            if (wins[i].minimized) wins[i].minimized = false;
            workspace.activeWindow = wins[i];
            break;
        }}
    }}
    """
    _run_kwin_script(js)
    return True


def _kwin_window_action(resource_class: str = "", caption: str = "",
                        action: str = "minimize") -> bool:
    """Perform action (minimize/maximize/close/unminimize) on a window."""
    if resource_class:
        match = f'wins[i].resourceClass.toLowerCase() === "{resource_class.lower()}"'
    elif caption:
        safe = caption.replace('"', '\\"')
        match = f'wins[i].caption.toLowerCase().indexOf("{safe.lower()}") >= 0'
    else:
        return False

    action_js = {
        "minimize": "wins[i].minimized = true;",
        "unminimize": "wins[i].minimized = false;",
        "maximize": "wins[i].setMaximize(true, true);",
        "unmaximize": "wins[i].setMaximize(false, false);",
        "close": "wins[i].closeWindow();",
        "fullscreen": "wins[i].fullScreen = true;",
        "unfullscreen": "wins[i].fullScreen = false;",
    }.get(action, "")

    if not action_js:
        return False

    js = f"""
    var wins = workspace.windowList();
    for (var i = 0; i < wins.length; i++) {{
        if ({match}) {{
            {action_js}
            break;
        }}
    }}
    """
    _run_kwin_script(js)
    return True


def _kwin_window_geometry(resource_class: str = "", caption: str = "",
                          x: int | None = None, y: int | None = None,
                          width: int | None = None, height: int | None = None) -> bool:
    """Move and/or resize a window via KWin scripting."""
    if resource_class:
        match = f'wins[i].resourceClass.toLowerCase() === "{resource_class.lower()}"'
    elif caption:
        safe = caption.replace('"', '\\"')
        match = f'wins[i].caption.toLowerCase().indexOf("{safe.lower()}") >= 0'
    else:
        return False

    set_lines = []
    if x is not None and y is not None:
        set_lines.append(f"wins[i].frameGeometry.x = {x};")
        set_lines.append(f"wins[i].frameGeometry.y = {y};")
    if width is not None and height is not None:
        set_lines.append(f"wins[i].frameGeometry.width = {width};")
        set_lines.append(f"wins[i].frameGeometry.height = {height};")

    if not set_lines:
        return False

    action_js = "\n            ".join(set_lines)
    js = f"""
    var wins = workspace.windowList();
    for (var i = 0; i < wins.length; i++) {{
        if ({match}) {{
            {action_js}
            break;
        }}
    }}
    """
    _run_kwin_script(js)
    return True


# ---------------------------------------------------------------------------
# Screenshot diffing
# ---------------------------------------------------------------------------

_last_screenshot_hash: str = ""
_last_screenshot_b64: str = ""


def _screenshot_hash(region: str | None = None) -> tuple[str, str]:
    """Take a screenshot and return (md5_hash, base64_data)."""
    b64 = _capture_screenshot(region)
    h = hashlib.md5(base64.b64decode(b64)).hexdigest()
    return h, b64


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def _ocr_screenshot(region: str = "") -> Image.Image:
    """Take a full-res screenshot for OCR (no downscaling), return PIL Image."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        if SCREENSHOT_BACKEND == "spectacle":
            _kill_spectacle()
            subprocess.run(["spectacle", "-b", "-n", "-f", "-o", tmp],
                           check=True, timeout=15, capture_output=True)
        elif SCREENSHOT_BACKEND == "grim":
            subprocess.run(["grim", tmp], check=True, timeout=10, capture_output=True)
        elif SCREENSHOT_BACKEND == "scrot":
            subprocess.run(["scrot", tmp], check=True, timeout=10, capture_output=True)
        elif SCREENSHOT_BACKEND == "import":
            subprocess.run(["import", "-window", "root", tmp],
                           check=True, timeout=10, capture_output=True)
        img = Image.open(tmp).copy()
        return img
    finally:
        Path(tmp).unlink(missing_ok=True)


def _ocr_find_text(text: str, region: str = "", language: str = "eng") -> list[dict]:
    """Find text on screen via OCR, return list of {text, x, y, bounds} dicts."""
    img = _ocr_screenshot(region)
    offset_x, offset_y = 0, 0
    if region:
        parts = region.replace(",", " ").replace("x", " ").split()
        if len(parts) == 4:
            rx, ry, rw, rh = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            img = img.crop((rx, ry, rx + rw, ry + rh))
            offset_x, offset_y = rx, ry

    img = img.convert("RGB")
    scale = 3
    img_scaled = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    data = pytesseract.image_to_data(img_scaled, lang=language,
                                      config="--psm 6", output_type=pytesseract.Output.DICT)

    matches = []
    search = text.lower()
    n = len(data["text"])

    for i in range(n):
        word = data["text"][i].strip()
        if not word:
            continue
        if search in word.lower():
            x = data["left"][i] // scale + offset_x
            y = data["top"][i] // scale + offset_y
            w = data["width"][i] // scale
            h = data["height"][i] // scale
            cx, cy = x + w // 2, y + h // 2
            matches.append({"text": word, "x": cx, "y": cy, "bounds": f"{x},{y},{w}x{h}"})

    words = search.split()
    if len(words) > 1 and not matches:
        all_words = [(data["text"][i].strip().lower(), i) for i in range(n) if data["text"][i].strip()]
        for idx in range(len(all_words) - len(words) + 1):
            chunk = " ".join(all_words[idx + j][0] for j in range(len(words)))
            if search in chunk:
                first_i = all_words[idx][1]
                last_i = all_words[idx + len(words) - 1][1]
                x1 = data["left"][first_i] // scale + offset_x
                y1 = data["top"][first_i] // scale + offset_y
                x2 = (data["left"][last_i] + data["width"][last_i]) // scale + offset_x
                y2 = (data["top"][last_i] + data["height"][last_i]) // scale + offset_y
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                matches.append({"text": chunk, "x": cx, "y": cy,
                                "bounds": f"{x1},{y1},{x2 - x1}x{y2 - y1}"})

    return matches


# ---------------------------------------------------------------------------
# MCP Tools (9 merged tools)
# ---------------------------------------------------------------------------

@mcp.tool()
def screen(action: str = "capture", region: str = "", name: str = "",
           resource_class: str = "", x: int = 0, y: int = 0,
           timeout: float = 10.0, interval: float = 0.5) -> list[dict] | str:
    """Vision. action: capture/window/diff/pixel/wait_change. region: 'x,y,WxH'."""
    global _last_screenshot_hash, _last_screenshot_b64

    if action == "capture":
        b64 = _capture_screenshot(region if region else None)
        return [
            {"type": "text", "text": "ok"},
            {"type": "image", "data": b64, "mimeType": "image/jpeg"},
        ]

    elif action == "window":
        if name or resource_class:
            if SESSION_TYPE == "wayland" and _which("qdbus6"):
                _kwin_focus_window(resource_class=resource_class, caption=name)
            elif _which("xdotool"):
                search = name or resource_class
                try:
                    wid = _run(["xdotool", "search", "--name", search]).split("\n")[0]
                    _run(["xdotool", "windowactivate", wid])
                except Exception:
                    pass
            time.sleep(0.3)
        b64 = _capture_screenshot()
        return [
            {"type": "text", "text": "ok"},
            {"type": "image", "data": b64, "mimeType": "image/jpeg"},
        ]

    elif action == "diff":
        r = region if region else None
        new_hash, new_b64 = _screenshot_hash(r)
        if not _last_screenshot_hash:
            _last_screenshot_hash = new_hash
            _last_screenshot_b64 = new_b64
            return [
                {"type": "text", "text": "first capture, no previous to diff"},
                {"type": "image", "data": new_b64, "mimeType": "image/jpeg"},
            ]
        changed = new_hash != _last_screenshot_hash
        old_img = Image.open(io.BytesIO(base64.b64decode(_last_screenshot_b64))).convert("RGB")
        new_img = Image.open(io.BytesIO(base64.b64decode(new_b64))).convert("RGB")
        desc = "CHANGED" if changed else "NO CHANGE"
        if old_img.size == new_img.size:
            old_px = old_img.load()
            new_px = new_img.load()
            w, h = old_img.size
            changed_pixels = 0
            for py in range(0, h, 4):
                for px in range(0, w, 4):
                    if old_px[px, py] != new_px[px, py]:
                        changed_pixels += 1
            sampled = (h // 4) * (w // 4)
            pct = round(changed_pixels / sampled * 100, 1) if sampled > 0 else 0
            desc += f" ({pct}%)"
        _last_screenshot_hash = new_hash
        _last_screenshot_b64 = new_b64
        return [
            {"type": "text", "text": desc},
            {"type": "image", "data": new_b64, "mimeType": "image/jpeg"},
        ]

    elif action == "pixel":
        b64 = _capture_screenshot(f"{x},{y},1x1")
        img_data = base64.b64decode(b64)
        img = Image.open(io.BytesIO(img_data))
        pr, pg, pb = img.getpixel((0, 0))[:3]
        return f"#{pr:02X}{pg:02X}{pb:02X}"

    elif action == "wait_change":
        timeout = min(timeout, 30.0)
        interval = max(interval, 0.3)
        r = region if region else None
        initial_hash, _ = _screenshot_hash(r)
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(interval)
            current_hash, _ = _screenshot_hash(r)
            if current_hash != initial_hash:
                return f"changed after {round(time.time() - start, 2)}s"
        return f"no change after {timeout}s"

    return f"unknown action '{action}'"


@mcp.tool()
def ocr(action: str = "read", text: str = "", region: str = "",
        language: str = "eng", button: str = "left", click_type: str = "single",
        occurrence: int = 1) -> str:
    """OCR tools. action: read/find/describe/click. No image sent — token efficient."""
    if not _which("tesseract"):
        return "tesseract not installed"

    if action == "read":
        b64 = _capture_screenshot(region if region else None)
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        scale = 3
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        try:
            result = pytesseract.image_to_string(img, lang=language, config="--psm 6").strip()
            return result if result else "no text"
        except Exception as e:
            return f"err: {e}"

    elif action == "find":
        if not text:
            return "need text param"
        matches = _ocr_find_text(text, region, language)
        if not matches:
            return f"'{text}' not found"
        lines = [f"{len(matches)} match(es):"]
        for m in matches[:10]:
            lines.append(f"  '{m['text']}' click({m['x']},{m['y']})")
        return "\n".join(lines)

    elif action == "describe":
        img = _ocr_screenshot(region)
        screen_w, screen_h = img.size
        offset_x, offset_y = 0, 0
        if region:
            parts = region.replace(",", " ").replace("x", " ").split()
            if len(parts) == 4:
                rx, ry, rw, rh = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                img = img.crop((rx, ry, rx + rw, ry + rh))
                offset_x, offset_y = rx, ry
        img = img.convert("RGB")
        scale = 3
        img_scaled = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
        data = pytesseract.image_to_data(img_scaled, lang=language,
                                          config="--psm 6", output_type=pytesseract.Output.DICT)
        lines_by_block = {}
        n = len(data["text"])
        for i in range(n):
            word = data["text"][i].strip()
            if not word:
                continue
            key = (data["block_num"][i], data["line_num"][i])
            if key not in lines_by_block:
                lines_by_block[key] = {
                    "words": [],
                    "top": data["top"][i] // scale + offset_y,
                }
            lines_by_block[key]["words"].append(word)
            lines_by_block[key]["bottom"] = (data["top"][i] + data["height"][i]) // scale + offset_y
        if not lines_by_block:
            return "no text"
        result = [f"({screen_w}x{screen_h}):"]
        for key in sorted(lines_by_block.keys()):
            entry = lines_by_block[key]
            cy = (entry["top"] + entry.get("bottom", entry["top"])) // 2
            result.append(f"  y={cy}: {' '.join(entry['words'])}")
        return "\n".join(result)

    elif action == "click":
        if not text:
            return "need text param"
        matches = _ocr_find_text(text, region, language)
        if not matches:
            return f"'{text}' not found"
        idx = min(occurrence, len(matches)) - 1
        mx, my = matches[idx]["x"], matches[idx]["y"]
        _mouse_move(mx, my)
        time.sleep(0.05)
        _mouse_click(button, click_type)
        return f"ok ({mx},{my})"

    return f"unknown action '{action}'"


@mcp.tool()
def inp(action: str, x: int = 0, y: int = 0, text: str = "", keys: str = "",
        button: str = "left", click_type: str = "single", direction: str = "down",
        clicks: int = 3, delay_ms: int = 12, start_x: int = 0, start_y: int = 0,
        end_x: int = 0, end_y: int = 0, duration_ms: int = 300) -> str:
    """Input. action: click/type/key/move/scroll/drag."""
    if action == "click":
        _mouse_move(x, y)
        time.sleep(0.05)
        _mouse_click(button, click_type)
        return f"ok ({x},{y})"

    elif action == "type":
        _type_text(text, delay_ms)
        return "ok"

    elif action == "key":
        _key_press(keys)
        return f"ok {keys}"

    elif action == "move":
        _mouse_move(x, y)
        return f"ok ({x},{y})"

    elif action == "scroll":
        if x >= 0 and y >= 0 and (x > 0 or y > 0):
            _mouse_move(x, y)
            time.sleep(0.05)
        _scroll(direction, clicks)
        return "ok"

    elif action == "drag":
        _require_input_backend()
        _mouse_move(start_x, start_y)
        time.sleep(0.05)
        if INPUT_BACKEND == "xdotool":
            btn_map = {"left": "1", "right": "3", "middle": "2"}
            btn = btn_map.get(button, "1")
            _run(["xdotool", "mousedown", btn])
            steps = max(5, duration_ms // 20)
            for i in range(1, steps + 1):
                t = i / steps
                ix = int(start_x + (end_x - start_x) * t)
                iy = int(start_y + (end_y - start_y) * t)
                _run(["xdotool", "mousemove", str(ix), str(iy)])
                time.sleep(0.02)
            _run(["xdotool", "mouseup", btn])
        else:
            btn_down = {"left": "0x40", "right": "0x41", "middle": "0x42"}
            btn_up = {"left": "0x80", "right": "0x81", "middle": "0x82"}
            _run(["ydotool", "click", btn_down.get(button, "0x40")])
            time.sleep(0.05)
            steps = max(5, duration_ms // 20)
            for i in range(1, steps + 1):
                t = i / steps
                ix = int(start_x + (end_x - start_x) * t)
                iy = int(start_y + (end_y - start_y) * t)
                _run(["ydotool", "mousemove", "--absolute", "-x", str(ix), "-y", str(iy)])
                time.sleep(0.02)
            _run(["ydotool", "click", btn_up.get(button, "0x80")])
        return "ok"

    return f"unknown action '{action}'"


@mcp.tool()
def window(action: str = "list", name: str = "", resource_class: str = "",
           window_id: str = "", x: int = 0, y: int = 0,
           width: int = 0, height: int = 0) -> str:
    """Window mgmt. action: list/focus/active/move/resize/minimize/maximize/close/unminimize/fullscreen/unfullscreen."""
    if action == "list":
        if SESSION_TYPE == "wayland" and _which("qdbus6"):
            try:
                windows = _kwin_list_windows()
                if windows:
                    return json.dumps(windows, indent=2)
            except Exception:
                pass
        if _which("xdotool"):
            try:
                out = _run(["xdotool", "search", "--onlyvisible", "--name", ""])
                window_ids = out.strip().split("\n")[:30]
                windows = []
                for wid in window_ids:
                    if not wid.strip():
                        continue
                    try:
                        wname = _run(["xdotool", "getwindowname", wid])
                        geo = _run(["xdotool", "getwindowgeometry", wid])
                        windows.append({"id": wid, "name": wname, "geometry": geo})
                    except Exception:
                        pass
                return json.dumps(windows, indent=2)
            except Exception as e:
                return f"err: {e}"
        return "no window tool"

    elif action == "focus":
        if not window_id and not name and not resource_class:
            return "need window_id, name, or resource_class"
        if SESSION_TYPE == "wayland" and _which("qdbus6") and (resource_class or name):
            try:
                if _kwin_focus_window(resource_class=resource_class, caption=name):
                    return "ok"
            except Exception:
                pass
        if _which("xdotool"):
            if window_id:
                _run(["xdotool", "windowactivate", window_id])
                return "ok"
            search = name or resource_class
            try:
                wid = _run(["xdotool", "search", "--name", search]).split("\n")[0]
                _run(["xdotool", "windowactivate", wid])
                return "ok"
            except Exception as e:
                return f"not found: {e}"
        return "no window tool"

    elif action == "active":
        if SESSION_TYPE == "wayland" and _which("qdbus6"):
            js = """
            var w = workspace.activeWindow;
            if (w) {
                console.log("__MARKER__" + JSON.stringify({
                    resourceClass: w.resourceClass,
                    caption: w.caption,
                    x: w.frameGeometry.x,
                    y: w.frameGeometry.y,
                    width: w.frameGeometry.width,
                    height: w.frameGeometry.height,
                    maximized: w.maximized,
                    fullScreen: w.fullScreen,
                    minimized: w.minimized
                }));
            }
            """
            raw = _kwin_script_with_output(js)
            if raw:
                try:
                    return json.dumps(json.loads(raw), indent=2)
                except json.JSONDecodeError:
                    pass
        if _which("xdotool"):
            try:
                wid = _run(["xdotool", "getactivewindow"])
                wname = _run(["xdotool", "getwindowname", wid])
                geo = _run(["xdotool", "getwindowgeometry", wid])
                return json.dumps({"id": wid, "name": wname, "geometry": geo}, indent=2)
            except Exception as e:
                return f"err: {e}"
        return "no window tool"

    elif action == "move":
        if not name and not resource_class:
            return "need name or resource_class"
        if SESSION_TYPE == "wayland" and _which("qdbus6"):
            if _kwin_window_geometry(resource_class=resource_class, caption=name, x=x, y=y):
                return "ok"
        if _which("xdotool"):
            search = name or resource_class
            try:
                wid = _run(["xdotool", "search", "--name", search]).split("\n")[0]
                _run(["xdotool", "windowmove", wid, str(x), str(y)])
                return "ok"
            except Exception as e:
                return f"err: {e}"
        return "no window tool"

    elif action == "resize":
        if not name and not resource_class:
            return "need name or resource_class"
        if SESSION_TYPE == "wayland" and _which("qdbus6"):
            if _kwin_window_geometry(resource_class=resource_class, caption=name,
                                     width=width, height=height):
                return "ok"
        if _which("xdotool"):
            search = name or resource_class
            try:
                wid = _run(["xdotool", "search", "--name", search]).split("\n")[0]
                _run(["xdotool", "windowsize", wid, str(width), str(height)])
                return "ok"
            except Exception as e:
                return f"err: {e}"
        return "no window tool"

    elif action in ("minimize", "unminimize", "maximize", "unmaximize",
                    "close", "fullscreen", "unfullscreen"):
        if not name and not resource_class:
            return "need name or resource_class"
        if SESSION_TYPE == "wayland" and _which("qdbus6"):
            try:
                if _kwin_window_action(resource_class=resource_class, caption=name, action=action):
                    return "ok"
            except Exception as e:
                return f"err: {e}"
        if _which("xdotool") and action in ("minimize", "maximize", "close"):
            search = name or resource_class
            try:
                wid = _run(["xdotool", "search", "--name", search]).split("\n")[0]
                xdo_action = {"minimize": "windowminimize", "maximize": "windowmaximize",
                              "close": "windowclose"}
                _run(["xdotool", xdo_action[action], wid])
                return "ok"
            except Exception as e:
                return f"err: {e}"
        return f"cannot '{action}'"

    return f"unknown action '{action}'"


@mcp.tool()
def clipboard(action: str = "get", text: str = "") -> str:
    """Clipboard. action: get/set."""
    if action == "get":
        try:
            content = _clipboard_get()
            return content[:10000] if len(content) > 10000 else content
        except Exception as e:
            return f"err: {e}"
    elif action == "set":
        try:
            _clipboard_set(text)
            return "ok"
        except Exception as e:
            return f"err: {e}"
    return f"unknown action '{action}'"


@mcp.tool()
def notify(action: str = "read", summary: str = "", body: str = "",
           count: int = 10, since: str = "1h") -> str:
    """Notifications. action: send/read."""
    if action == "send":
        return "ok" if _send_notification(summary, body) else "failed"
    elif action == "read":
        notifications = _read_notifications(count, since)
        if not notifications:
            return "none"
        return json.dumps(notifications, indent=2, ensure_ascii=False)
    return f"unknown action '{action}'"


@mcp.tool()
def system(action: str = "info", seconds: float = 1.0) -> str:
    """System. action: info/monitors/health/wait."""
    if action == "info":
        sw, sh = _get_screen_size()
        try:
            cx, cy = _get_cursor_position()
            cursor = f"({cx},{cy})"
        except Exception:
            cursor = "?"
        return json.dumps({
            "screen": f"{sw}x{sh}", "cursor": cursor,
            "session": SESSION_TYPE, "screenshot": SCREENSHOT_BACKEND,
            "input": INPUT_BACKEND, "clipboard": CLIPBOARD_BACKEND,
            "kwin": bool(_which("qdbus6")), "ocr": bool(_which("tesseract")),
            "monitors": _get_monitors(),
        }, indent=2)
    elif action == "monitors":
        mons = _get_monitors()
        return json.dumps(mons, indent=2) if mons else "none"
    elif action == "health":
        return json.dumps(_check_backend_health(), indent=2)
    elif action == "wait":
        time.sleep(min(seconds, 30.0))
        return "ok"
    return f"unknown action '{action}'"


@mcp.tool()
def multi_action(actions: str) -> str:
    """Chain actions as JSON array. Types: click, move, type, key, scroll, wait, drag."""
    try:
        steps = json.loads(actions)
    except json.JSONDecodeError as e:
        return f"bad json: {e}"

    if not isinstance(steps, list):
        return "need JSON array"

    results = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or "type" not in step:
            results.append(f"{i}:invalid")
            continue
        t = step["type"]
        try:
            if t == "click":
                _mouse_move(step["x"], step["y"])
                time.sleep(0.05)
                _mouse_click(step.get("button", "left"), step.get("click_type", "single"))
            elif t == "move":
                _mouse_move(step["x"], step["y"])
            elif t == "type":
                _type_text(step["text"], step.get("delay_ms", 12))
            elif t == "key":
                _key_press(step["keys"])
            elif t == "scroll":
                if step.get("x", -1) >= 0 and step.get("y", -1) >= 0:
                    _mouse_move(step["x"], step["y"])
                    time.sleep(0.05)
                _scroll(step.get("direction", "down"), step.get("clicks", 3))
            elif t == "wait":
                time.sleep(min(step.get("seconds", 0.5), 10.0))
            elif t == "drag":
                _mouse_move(step["start_x"], step["start_y"])
                time.sleep(0.05)
                btn = step.get("button", "left")
                if INPUT_BACKEND == "xdotool":
                    btn_id = {"left": "1", "right": "3", "middle": "2"}.get(btn, "1")
                    _run(["xdotool", "mousedown", btn_id])
                    _mouse_move(step["end_x"], step["end_y"])
                    time.sleep(0.05)
                    _run(["xdotool", "mouseup", btn_id])
                else:
                    btn_down = {"left": "0x40", "right": "0x41", "middle": "0x42"}.get(btn, "0x40")
                    btn_up = {"left": "0x80", "right": "0x81", "middle": "0x82"}.get(btn, "0x80")
                    _run(["ydotool", "click", btn_down])
                    time.sleep(0.05)
                    _mouse_move(step["end_x"], step["end_y"])
                    time.sleep(0.05)
                    _run(["ydotool", "click", btn_up])
            else:
                results.append(f"{i}:unknown '{t}'")
                continue
            results.append(f"{i}:ok")
        except Exception as e:
            results.append(f"{i}:err {e}")
            break
    return "\n".join(results)


@mcp.tool()
def file_dialog(action: str = "open", path: str = "", title: str = "Select File",
                filters: str = "") -> str:
    """Native file dialog. action: open/save/directory."""
    if _which("zenity"):
        cmd = ["zenity"]
        if action == "save":
            cmd.extend(["--file-selection", "--save"])
        elif action == "directory":
            cmd.extend(["--file-selection", "--directory"])
        else:
            cmd.append("--file-selection")
        cmd.extend(["--title", title])
        if path:
            cmd.extend(["--filename", path])
        if filters:
            cmd.extend(["--file-filter", filters])
    elif _which("kdialog"):
        cmd = ["kdialog"]
        if action == "save":
            cmd.extend(["--getsavefilename", path or "~"])
        elif action == "directory":
            cmd.extend(["--getexistingdirectory", path or "~"])
        else:
            cmd.extend(["--getopenfilename", path or "~"])
        cmd.extend(["--title", title])
        if filters:
            cmd.append(filters)
    else:
        return "no dialog tool"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "cancelled"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return f"err: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    print(f"Computer Use MCP Server v0.5.0", file=sys.stderr)
    print(f"  Session: {SESSION_TYPE}", file=sys.stderr)
    print(f"  Screenshot: {SCREENSHOT_BACKEND}", file=sys.stderr)
    print(f"  Input: {INPUT_BACKEND}", file=sys.stderr)
    print(f"  Clipboard: {CLIPBOARD_BACKEND}", file=sys.stderr)
    print(f"  KWin: {'yes' if _which('qdbus6') else 'no'}", file=sys.stderr)
    print(f"  Screen: {_get_screen_size()}", file=sys.stderr)
    mcp.run(transport="stdio")
