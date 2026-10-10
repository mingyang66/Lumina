import json
import os
import subprocess
import sys
import tempfile
import warnings


def default_config_path():
    if getattr(sys, 'frozen', False):
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        if not base:
            base = os.path.join(os.path.expanduser('~'), '.lumina')
        return os.path.abspath(os.path.join(base, 'Lumina', 'config.json'))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.json')


def _resolve_db_path(cfg, config_path, legacy_dirs=()):
    raw = os.fspath(cfg['db_path'])
    if raw == ':memory:' or os.path.isabs(raw):
        return dict(cfg)
    target = os.path.abspath(os.path.join(os.path.dirname(config_path), raw))
    candidates = [target] + [os.path.abspath(os.path.join(base, raw))
                             for base in (*legacy_dirs, os.getcwd())]
    existing = []
    for candidate in candidates:
        if os.path.isfile(candidate) and not any(os.path.samefile(candidate, other)
                                                for other in existing):
            existing.append(candidate)
    if len(existing) > 1:
        raise ValueError('发现多个历史数据库，请在配置中显式指定绝对 db_path: ' + ', '.join(existing))
    resolved = existing[0] if existing else target
    if os.path.normcase(resolved) != os.path.normcase(target):
        warnings.warn('继续使用旧历史数据库（未移动）: ' + resolved, RuntimeWarning)
    result = dict(cfg)
    result['db_path'] = resolved
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
    legacy_dirs = ((os.path.dirname(os.path.abspath(sys.executable)),)
                   if getattr(sys, 'frozen', False) else ())
    bootstrap = (getattr(sys, 'frozen', False)
                 and os.path.normcase(path) == os.path.normcase(default_config_path())
                 and not os.path.exists(path))
    source = path
    if bootstrap:
        executable_dir = os.path.dirname(os.path.abspath(sys.executable))
        bundle_dir = getattr(sys, '_MEIPASS', executable_dir)
        legacy_dirs = (executable_dir,)
        # 优先使用可执行文件旁用户旧配置，解压目录仅作为默认模板。
        source = os.path.join(executable_dir, 'config.json')
        if not os.path.isfile(source):
            source = os.path.join(bundle_dir, 'config.json')
    with open(source, 'r', encoding='utf-8') as stream:
        cfg = json.load(stream)
    if not isinstance(cfg, dict):
        raise ValueError(f'配置文件必须是 JSON 对象: {source}')
    raw_db_path = cfg['db_path']
    cfg = _resolve_db_path(cfg, path, legacy_dirs)
    target = os.path.abspath(os.path.join(os.path.dirname(path), raw_db_path))
    legacy_selected = (raw_db_path != ':memory:' and not os.path.isabs(raw_db_path)
                       and os.path.normcase(cfg['db_path']) != os.path.normcase(target))
    if bootstrap or legacy_selected:
        # 固定兼容选择，后续自启动的工作目录变化不能重新丢失历史。
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
