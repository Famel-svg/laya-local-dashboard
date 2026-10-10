"""Small stdlib-only Windows notification-area controller for Tkinter."""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from typing import Callable


if sys.platform != "win32":
    raise OSError("windows_tray requires Windows")

import win32api
import win32con
import win32gui


_WM_DESTROY = 0x0002
_WM_COMMAND = 0x0111
_WM_USER = 0x0400
_WM_TRAY = _WM_USER + 20
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONDBLCLK = 0x0203
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONDOWN = 0x0204
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B
_NIM_SETVERSION = 0x00000004
_NOTIFYICON_VERSION_4 = 4
_NIN_SELECT = _WM_USER
_NIN_KEYSELECT = _WM_USER + 1
_NIM_ADD = 0x00000000
_NIM_DELETE = 0x00000002
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_IDI_APPLICATION = 32512
_MF_STRING = 0x00000000
_MF_SEPARATOR = 0x00000800
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100
_IMAGE_ICON = 1
_LR_SHARED = 0x00008000
_OPEN = 1001
_REFRESH = 1002
_EXIT = 1003
_RESTART = 1004
_LR_LOADFROMFILE = 0x00000010


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


class WindowsTray:
    """Attach a tray icon to a Tk root; callbacks always run via Tk's event loop.

    Contract: construct and call ``attach`` on Tk's main thread. Bind WM_DELETE_WINDOW
    to ``hide``. ``refresh`` and ``on_exit`` are optional app callbacks. Call
    ``shutdown`` before destroying the root; a dedicated hidden Win32 window receives
    shell callbacks without replacing Tk's window procedure.
    """

    def __init__(self, root: tk.Tk, refresh: Callable[[], object] | None = None,
                 on_exit: Callable[[], object] | None = None,
                 on_restart: Callable[[], object] | None = None,
                 tooltip: str = "Laya Local") -> None:
        self.root = root
        self.refresh_callback = refresh or (lambda: None)
        self.exit_callback = on_exit
        self.restart_callback = on_restart or (lambda: None)
        self.tooltip = tooltip[:127]
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self._user32.CreatePopupMenu.restype = wintypes.HMENU
        self._user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        self._user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
        self._user32.TrackPopupMenu.restype = wintypes.UINT
        self._user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self._user32.LoadImageW.restype = wintypes.HANDLE
        self._hwnd = None
        self._hinstance = win32api.GetModuleHandle(None)
        self._class_name = f"LayaLocalTrayWindow_{os.getpid()}"
        self._class_registered = False
        self._tray_thread: threading.Thread | None = None
        self._tray_thread_started = threading.Event()
        self._tray_thread_done = threading.Event()
        self._tray_thread_error: BaseException | None = None
        self._events: queue.SimpleQueue[tuple[str, int | None]] = queue.SimpleQueue()
        self._icon_added = False
        self._closed = False
        self._version4 = False
        self._show_pending = False
        self._icon_shared = False
        bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_path = bundle_dir / "laya_tray.ico"
        self._icon = self._user32.LoadImageW(None, str(icon_path), _IMAGE_ICON, 0, 0, _LR_LOADFROMFILE)
        if self._icon:
            self._user32.DestroyIcon.argtypes = [wintypes.HANDLE]
            self._user32.DestroyIcon.restype = wintypes.BOOL
        else:
            self._icon = self._user32.LoadImageW(None, ctypes.cast(ctypes.c_void_p(_IDI_APPLICATION), wintypes.LPCWSTR),
                                                  _IMAGE_ICON, 0, 0, _LR_SHARED)
            self._icon_shared = True
        if not self._icon:
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid = _NOTIFYICONDATAW()
        self._nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        self._nid.uCallbackMessage = _WM_TRAY
        self._nid.hIcon = self._icon
        self._nid.szTip = self.tooltip
        self._log("controller created")

    def _log(self, message: str) -> None:
        try:
            folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Codex" / "Laya"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "tray.log").open("a", encoding="utf-8") as stream:
                from datetime import datetime
                stream.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass

    def _create_callback_window(self) -> None:
        self._tray_thread = threading.Thread(
            target=self._tray_message_loop, name="laya-tray-message-loop", daemon=True,
        )
        self._tray_thread.start()
        if not self._tray_thread_started.wait(5):
            raise TimeoutError("notification-area window did not start")
        if self._tray_thread_error is not None:
            raise RuntimeError("notification-area window failed") from self._tray_thread_error
        self._nid.hWnd = self._hwnd
        self._log(f"callback window created hwnd={self._hwnd}")

    def _tray_message_loop(self) -> None:
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = self._hinstance
            wc.lpszClassName = self._class_name
            wc.lpfnWndProc = self._dispatch_win32
            win32gui.RegisterClass(wc)
            self._class_registered = True
            self._hwnd = win32gui.CreateWindow(
                self._class_name, "LayaLocalTrayWindow", win32con.WS_POPUP,
                0, 0, 0, 0, 0, 0, self._hinstance, None,
            )
            self._tray_thread_started.set()
            win32gui.PumpMessages()
        except BaseException as exc:
            self._tray_thread_error = exc
            self._tray_thread_started.set()
            self._log(f"message loop failed: {type(exc).__name__}")
        finally:
            if self._class_registered:
                try:
                    win32gui.UnregisterClass(self._class_name, self._hinstance)
                except Exception as exc:
                    self._log(f"class cleanup failed: {type(exc).__name__}")
                self._class_registered = False
            self._hwnd = None
            self._tray_thread_done.set()

    def attach(self) -> None:
        """Install window callback and tray icon. Call on Tk main thread."""
        if self._closed:
            raise RuntimeError("tray controller already shut down")
        if self._icon_added:
            return
        self._create_callback_window()
        if not self._shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(self._nid)):
            error = ctypes.get_last_error()
            self._destroy_callback_window()
            self._log(f"NIM_ADD failed winerror={error}")
            raise ctypes.WinError(error)
        self._icon_added = True
        self._nid.uVersion = _NOTIFYICON_VERSION_4
        self._version4 = bool(self._shell32.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(self._nid)))
        self._log(f"NIM_ADD ok; version4={self._version4}")
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self.root.after(40, self._drain_events)

    def hide(self) -> None:
        """Hide main window without ending Tk's event loop."""
        if not self._closed:
            self.root.withdraw()

    def show(self) -> None:
        """Restore and focus main window. Safe to call only on Tk main thread."""
        if not self._closed:
            self.root.state("normal")
            self.root.deiconify()
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
            self._log("main window restored")

    def shutdown(self) -> None:
        """Remove icon and restore original window procedure; idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._icon_added:
            self._shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
            self._icon_added = False
        if self._hwnd:
            self._user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE on owner thread
        if self._tray_thread is not None and self._tray_thread is not threading.current_thread():
            self._tray_thread.join(timeout=2)
        if self._icon and not self._icon_shared:
            self._user32.DestroyIcon(self._icon)
            self._icon = None

    def _dispatch_win32(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == _WM_TRAY:
            event = (lparam & 0xFFFF) if self._version4 else lparam
            self._log(f"tray event=0x{event & 0xFFFFFFFF:08X} version4={self._version4}")
            if event in (_WM_LBUTTONDOWN, _WM_LBUTTONUP, _WM_LBUTTONDBLCLK, _NIN_SELECT, _NIN_KEYSELECT):
                self._events.put(("show", None))
                return 0
            if event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                self._show_menu()
                return 0
        if message == _WM_COMMAND:
            command = wparam & 0xFFFF
            self._events.put(("command", command))
            return 0
        if message == 0x0010:  # WM_CLOSE
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == _WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return int(win32gui.DefWindowProc(hwnd, message, wparam, lparam))

    def _drain_events(self) -> None:
        if self._closed:
            return
        while True:
            try:
                event, value = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                if event == "show":
                    self.show()
                elif value == _OPEN:
                    self.show()
                elif value == _REFRESH:
                    self.refresh_callback()
                elif value == _EXIT:
                    self._exit()
                elif value == _RESTART:
                    self.restart_callback()
            except Exception as exc:
                self._log(f"event {event}/{value} failed: {type(exc).__name__}")
        try:
            self.root.after(40, self._drain_events)
        except tk.TclError:
            pass

    def _show_menu(self) -> None:
        if self._closed:
            return
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self._user32.AppendMenuW(menu, _MF_STRING, _OPEN, "Abrir Laya")
            self._user32.AppendMenuW(menu, _MF_STRING, _REFRESH, "Atualizar")
            self._user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, _MF_STRING, _RESTART, "Reiniciar Laya")
            self._user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, _MF_STRING, _EXIT, "Sair")
            point = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            self._user32.SetForegroundWindow(self._hwnd)
            command = self._user32.TrackPopupMenu(menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
                                                   point.x, point.y, 0, self._hwnd, None)
            if command:
                self._user32.PostMessageW(self._hwnd, _WM_COMMAND, command, 0)
        finally:
            self._user32.DestroyMenu(menu)

    def _exit(self) -> None:
        if self._closed:
            return
        self.shutdown()
        try:
            if self.exit_callback is not None:
                self.exit_callback()
        finally:
            if self.root.winfo_exists():
                self.root.destroy()

    def _on_root_destroy(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self.shutdown()
