"""Current-user Windows login autostart for the native Laya prototype.

This module only defines helpers. Importing it never reads or changes the
registry; call ``set_enabled`` explicitly to change autostart.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "LayaLocalPrototype"


def _windows_paths() -> tuple[Path, Path]:
    if sys.platform != "win32":
        raise OSError("Laya login autostart is supported only on Windows.")

    # The dashboard runs from its own PySide6 venv outside the checkout, so
    # reuse whichever interpreter is running it.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    app = Path(__file__).resolve().with_name("laya_app.py")

    if not pythonw.is_file():
        raise FileNotFoundError(f"Venv pythonw.exe not found: {pythonw}")
    if not app.is_file():
        raise FileNotFoundError(f"Native Laya app not found: {app}")
    return pythonw.resolve(), app


def enable_executable_autostart(executable: str | Path, startup_script: str | Path | None = None) -> Path:
    """Install a Windows Startup launcher that runs EXE and local MCP runtime."""
    if sys.platform != "win32":
        raise OSError("Laya login autostart is supported only on Windows.")
    import winreg
    exe = Path(executable).resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    script = Path(startup_script).resolve() if startup_script else Path(__file__).with_name("laya_startup.vbs").resolve()
    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)
    launcher = startup_dir / "Laya Local.vbs"
    text = script.read_text(encoding="utf-8-sig")
    escaped = str(exe).replace('"', '""')
    import re
    text, count = re.subn(r'^app\s*=\s*".*"\s*$', f'app = "{escaped}"', text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError("Startup launcher has no app path assignment")
    launcher.write_text(text, encoding="utf-8")
    command = f'"{exe}" --background'
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)
    return launcher


def _command() -> str:
    pythonw, app = _windows_paths()
    # Windows Run values are command lines parsed by the process command-line
    # parser. Quote each absolute path so spaces remain part of the path.
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return f'"{exe}" --background'
    return f'"{pythonw}" "{app}" --background'


def is_enabled() -> bool:
    """Return whether the app is configured in Startup or HKCU Run."""
    if sys.platform != "win32":
        raise OSError("Laya login autostart is supported only on Windows.")

    import winreg

    if getattr(sys, "frozen", False):
        launcher = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Laya Local.vbs"
        if launcher.is_file() and str(Path(sys.executable).resolve()).casefold() in launcher.read_text(encoding="utf-8-sig").casefold():
            return True

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _value_type = winreg.QueryValueEx(key, _VALUE_NAME)
    except FileNotFoundError:
        return False
    return value == _command()


def set_enabled(enabled: bool) -> None:
    """Enable or disable current-user login autostart, idempotently.

    Enabling stores the absolute venv ``pythonw.exe`` and ``native_app.py``
    paths under HKCU Run. Disabling deletes only this module's named value.
    No administrator rights are required.
    """
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    if sys.platform != "win32":
        raise OSError("Laya login autostart is supported only on Windows.")

    import winreg

    if getattr(sys, "frozen", False):
        launcher = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Laya Local.vbs"
        if enabled:
            enable_executable_autostart(Path(sys.executable))
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
        else:
            launcher.unlink(missing_ok=True)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
        return

    access = winreg.KEY_SET_VALUE
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, access) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass
