"""Windows CI GUI rendering; synthetic status, no enrollment or network."""
import base64
import ctypes
from ctypes import wintypes as w
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmdb_agent import __version__, status
from cmdb_agent.config import AgentConfig
from cmdb_agent.gui.status_window import StatusWindow
from cmdb_agent.gui.settings_window import SettingsWindow
from cmdb_agent.gui import settings_window

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
config = AgentConfig(server_url="https://cmdb.example", data_dir=Path(tempfile.mkdtemp(prefix="cmdb-gui-qa-")))
snapshot = status.AgentStatus(agent_version="0.5.5", hostname="NewNode", tenant_slug="jps",
    server_url=config.server_url, last_status="ok", configured=True, enrolled=True,
    last_sync_at=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
    published_at=datetime.now(timezone.utc).isoformat(), discovery={"state": "disabled"})


def capture(window, name):
    window.geometry("+20+20")
    window.update()
    assert len(window._cmdb_icons) == 3, "Missing C window icons"
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetParent.argtypes, user.GetParent.restype = [w.HWND], w.HWND
    hwnd = user.GetParent(window.winfo_id())
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    rect = w.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    assert 400 < width < 1100 and 250 < height < 1100, (width, height)
    user.GetWindowDC.argtypes, user.GetWindowDC.restype = [w.HWND], w.HDC
    user.ReleaseDC.argtypes = [w.HWND, w.HDC]
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [w.HDC], w.HDC
    gdi.CreateCompatibleBitmap.argtypes, gdi.CreateCompatibleBitmap.restype = [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [w.HDC, w.HANDLE], w.HANDLE
    gdi.DeleteObject.argtypes, gdi.DeleteDC.argtypes = [w.HANDLE], [w.HDC]
    user.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
    class Header(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                    ("planes", w.WORD), ("bits", w.WORD), ("compression", w.DWORD),
                    ("image_size", w.DWORD), ("x", w.LONG), ("y", w.LONG),
                    ("used", w.DWORD), ("important", w.DWORD)]
    gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.POINTER(Header), w.UINT]
    dc = user.GetWindowDC(hwnd)
    memory = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    old = gdi.SelectObject(memory, bitmap)
    try:
        assert user.PrintWindow(hwnd, memory, 2), "PrintWindow failed"
        gdi.SelectObject(memory, old)
        header = Header(size=ctypes.sizeof(Header), width=width, height=-height, planes=1, bits=32)
        pixels = ctypes.create_string_buffer(width * height * 4)
        assert gdi.GetDIBits(dc, bitmap, 0, height, pixels, ctypes.byref(header), 0) == height
        picture = Image.frombuffer("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
        path = out / (name + ".png")
        picture.save(path)
        print("GUI_RENDER " + name + " " + base64.b64encode(path.read_bytes()).decode())
    finally:
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, dc)


with patch.object(status, "read", return_value=snapshot):
    root = tk.Tk()
    root.withdraw()
    app = StatusWindow(root, config, lambda: None, lambda: None, lambda: None)
    app.show()
    assert app.version_label.cget("text") == "v" + __version__
    assert not {"version", "report_version", "discovery"} & set(app.values)
    assert app.state_var.get() == "Brak świeżej synchronizacji"
    capture(app.window, "status")
    app.hide()
    root.destroy()
    for administrator in (True, False):
        with patch.object(settings_window, "is_admin", return_value=administrator):
            settings = SettingsWindow(config, config.data_dir / "agent.conf")
            assert not hasattr(settings, "discovery_var") and not hasattr(settings, "discovery_cidrs_var")
            assert hasattr(settings, "discovery_state_var") == administrator
            capture(settings.root, "settings-admin" if administrator else "settings-user")
            settings.root.destroy()
print("GUI: header version only, no scanner in status, scanner visible in administrator settings only OK")
