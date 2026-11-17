import json
import os
import shutil
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


def _first_existing(*candidates):
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def example_config_path():
    """随仓库/安装包发布的配置模板（不含密钥材料）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'config.example.json')


def load_config(path=None):
    default_path = default_config_path()
    path = os.path.abspath(path or default_path)
    bootstrap = (getattr(sys, 'frozen', False)
                 and os.path.normcase(path) == os.path.normcase(default_path)
                 and not os.path.exists(path))
    source = path
    if bootstrap:
        bundle_dir = sys._MEIPASS
        # 首次启动仅使用 bundle 默认模板，后续读取稳定配置。
        source = _first_existing(os.path.join(bundle_dir, 'config.json'),
                                 os.path.join(bundle_dir, 'config.example.json'))
        if source is None:
            raise FileNotFoundError(
                f'安装包里没有配置模板: {os.path.join(bundle_dir, "config.example.json")}')
    elif (not os.path.exists(path)
          and os.path.normcase(path) == os.path.normcase(default_path)
          and _first_existing(example_config_path())):
        # 源码版首次运行：仓库不再跟踪 config.json（它会被改、且含有密匣密钥
        # 材料），从模板生成一份用户配置，避免新 clone 连启动都抛
        # FileNotFoundError。
        source = example_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(source, path)
        source = path
    with open(source, encoding='utf-8') as stream:
        cfg = json.load(stream)
    if not isinstance(cfg, dict):
        raise ValueError(f'配置文件必须是 JSON 对象: {source}')
    cfg = _resolve_db_path(cfg, path)
    if bootstrap:
        save_config(cfg, path)
    return cfg


def _relative_db_path(cfg, directory):
    """把落在配置目录内的绝对 db_path 折回相对写法。

    ``load_config`` 会把相对路径解析成绝对路径（运行期必须），而 ``save_config``
    写回的是这份内存副本。不折回去的话，改一次设置（闹钟、面板尺寸、密匣口令）
    就会把仓库里的 ``"db_path": "data/lumina.db"`` 改成开发机的绝对路径，
    打包进 exe 后换台机器就指向不存在的盘符。指向配置目录之外的绝对路径
    （用户有意指定）保持原样。
    """
    raw = cfg.get("db_path")
    if not isinstance(raw, str) or raw == ":memory:" or not os.path.isabs(raw):
        return cfg
    try:
        relative = os.path.relpath(raw, directory)
    except ValueError:      # 跨盘符，折不了
        return cfg
    if os.path.isabs(relative) or relative.startswith(os.pardir):
        return cfg
    adjusted = dict(cfg)
    adjusted["db_path"] = relative.replace(os.sep, "/")
    return adjusted


def save_config(cfg, path=None):
    path = os.path.abspath(path or default_config_path())
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(_relative_db_path(cfg, directory), stream,
                      ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
