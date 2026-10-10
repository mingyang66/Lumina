import json
import os
import subprocess
import sys
import tempfile


def default_config_path():
    if getattr(sys, 'frozen', False):
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        if not base:
            base = os.path.join(os.path.expanduser('~'), '.lumina')
        return os.path.abspath(os.path.join(base, 'Lumina', 'config.json'))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.json')


def _resolve_db_path(cfg, config_path):
    raw = os.fspath(cfg['db_path'])
    result = dict(cfg)
    if raw != ':memory:' and not os.path.isabs(raw):
        result['db_path'] = os.path.abspath(os.path.join(os.path.dirname(config_path), raw))
    return result

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

        config_path = os.path.abspath(config_path or default_config_path())
        if getattr(sys, "frozen", False):
            command = [sys.executable, "-c", config_path, "run"]
        else:
            script = os.path.abspath(sys.argv[0])
            executable = sys.executable
            if os.path.basename(executable).lower() == "python.exe":
                executable = os.path.join(os.path.dirname(executable), "pythonw.exe")
            command = [executable, script, "-c", config_path, "run"]
        winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ,
                          subprocess.list2cmdline(command))


def load_config(path=None):
    path = os.path.abspath(path or default_config_path())
    bootstrap = (getattr(sys, 'frozen', False)
                 and os.path.normcase(path) == os.path.normcase(default_config_path())
                 and not os.path.exists(path))
    source = path
    if bootstrap:
        bundle_dir = sys._MEIPASS
        # 首次启动仅使用 bundle 默认模板，后续读取稳定配置。
        source = os.path.join(bundle_dir, 'config.json')
    with open(source, 'r', encoding='utf-8') as stream:
        cfg = json.load(stream)
    if not isinstance(cfg, dict):
        raise ValueError(f'配置文件必须是 JSON 对象: {source}')
    cfg = _resolve_db_path(cfg, path)
    if bootstrap:
        save_config(cfg, path)
    return cfg


def save_config(cfg, path=None):
    path = os.path.abspath(path or default_config_path())
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
