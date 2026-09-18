import json
import os
import subprocess
import sys

DEFAULTS = {
    "db_path": "data/lumina.db",
    "retention_days": 30,
    "max_rows": 5000,
    "max_text_kb": 512,
    "max_image_mb": 64,
    "archive_files": True,
    "max_file_mb": 100,
    "hotkey_capture": "ctrl+alt+a",
    "hotkey_region": "ctrl+alt+s",
    "hotkey_panel": "ctrl+alt+h",
    "capture_monitor": 1,
    "copy_screenshot_to_clipboard": True,
    "cleanup_interval_minutes": 60,
    "popup_enabled": True,
    "popup_seconds": 3,
    "panel_enabled": True,
    "hide_panel_on_capture": True,
    "download_dir": "",
    "autostart": False,
}

AUTOSTART_VALUE = "Lumina"


def set_autostart(enabled, config_path):
    """Enable or disable Lumina for the current Windows user."""
    if os.name != "nt":
        raise OSError("开机自启仅支持 Windows")
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                        winreg.KEY_SET_VALUE) as key:
        if not enabled:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass
            return

        config_path = os.path.abspath(config_path or "config.json")
        if getattr(sys, "frozen", False):
            command = [sys.executable, "-c", config_path, "run"]
        else:
            script = os.path.abspath(sys.argv[0])
            command = [sys.executable, script, "-c", config_path, "run"]
        winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ,
                          subprocess.list2cmdline(command))


def load_config(path="config.json"):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    else:
        save_config(cfg, path)
    return cfg


def save_config(cfg, path="config.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
