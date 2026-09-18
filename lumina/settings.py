import json
import os
import subprocess
import sys

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
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"配置文件必须是 JSON 对象: {path}")
    return cfg


def save_config(cfg, path="config.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
