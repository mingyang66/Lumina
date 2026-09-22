import io
import os
import queue
import threading
import time
import traceback

from PIL import Image

from . import clipboard_api
from .clipboard_listener import ClipboardListener
from .media import grab_screen, image_to_dib, to_png
from .settings import save_config, set_autostart
from .database import Database
from .system_tray import TrayIcon
from .text_classifier import classify_text
from .ui import UiServer


class LuminaApp:
    def __init__(self, config, config_path=None):
        self.config = config
        self.config_path = config_path
        self.db = Database(config["db_path"])
        self.db.init_schema()
        self.ui = UiServer(self.db, config, actions=self)
        self.listener = ClipboardListener(
            on_text=self._on_text,
            on_image=self._on_image,
            on_files=self._on_files,
            hotkeys=[
                (config.get("hotkey_capture"), self.capture_screen),
                (config.get("hotkey_region"), self.start_region_capture),
                (config.get("hotkey_panel"), self.ui.toggle_panel),
            ],
            on_worker_stop=self.db.close,
        )
        self._stop = threading.Event()
        self._capture_lock = threading.Lock()
        self._file_queue = queue.Queue()
        self._file_worker = threading.Thread(
            target=self._file_archive_loop, name="lumina-file-archive", daemon=True)
        self._file_worker.start()
        self._cleaner_thread = None
        self.tray = TrayIcon(self)

    def stop(self):
        """请求主循环退出；资源由 start() 的 finally 按顺序回收。"""
        self._stop.set()

    # ---------- 剪贴板事件 ----------
    def _on_text(self, text, source):
        if not text or not text.strip():
            return
        category = classify_text(text)
        clip_id = self.db.add_text(
            text, source=source, category=category,
            max_bytes=int(self.config.get("max_text_kb", 512)) * 1024)
        self._log(f"{category} #{clip_id} saved ({len(text)} chars) "
                  f"from {source or '?'}")
        self.ui.notify(category, clip_id, preview=" ".join(text.split())[:200],
                       source=source)

    def _on_files(self, files, source):
        self._log(f"clipboard files detected: {len(files)}")
        clip_ids = []
        names = []
        archive = bool(self.config.get("archive_files", True))
        for path in files:
            clip_id = self.db.add_file_pending(
                path, source=source, status="pending" if archive else "skipped")
            clip_ids.append(clip_id)
            names.append(os.path.basename(path.rstrip("\\/")) or path)
            if archive:
                self._file_queue.put((clip_id, path))
                self._log(f"file #{clip_id} queued: {path}")
            else:
                self._log(f"file #{clip_id} archive skipped by config")
        preview = "、".join(names[:3])
        if len(names) > 3:
            preview += f" 等{len(names)}项"
        self._log(f"file #{','.join(map(str, clip_ids))} saved ({len(files)} path(s)) "
                  f"from {source or '?'}")
        if clip_ids:
            self.ui.notify("file", clip_ids[-1], preview=preview[:200], source=source)

    def _file_archive_loop(self):
        while True:
            item = self._file_queue.get()
            if item is None:
                self._file_queue.task_done()
                return
            clip_id, path = item
            try:
                size = os.path.getsize(path)
                max_bytes = int(self.config.get("max_file_mb", 100)) * 1024 * 1024
                if max_bytes > 0 and size > max_bytes:
                    raise ValueError(f"file exceeds {max_bytes // 1024 // 1024} MB limit")
                with open(path, "rb") as stream:
                    data = stream.read()
                self.db.complete_file(clip_id, data)
                self._log(f"file #{clip_id} archived ({size // 1024} KB)")
            except Exception as exc:
                self.db.fail_file(clip_id, exc)
                self._log(f"file #{clip_id} archive failed: {exc}")
            finally:
                self._file_queue.task_done()

    def _on_image(self, png, source):
        clip_id = self._store_image(png, source)
        if clip_id is None:
            return
        self._log(f"image #{clip_id} saved ({len(png) // 1024} KB) from {source or '?'}")
        self.ui.notify("image", clip_id, png=png, source=source)

    def _store_image(self, png, source):
        max_bytes = int(self.config.get("max_image_mb", 64)) * 1024 * 1024
        if len(png) > max_bytes:
            self._log(f"image skipped: {len(png) // 1024 // 1024} MB exceeds limit")
            return None
        return self.db.add_image(png, source=source)

    @staticmethod
    def _log(msg):
        print(f"[lumina] {msg}", flush=True)

    # ---------- 截图 ----------
    def _hide_panel_for_capture(self):
        """热键截图路径：面板可见且启用隐藏时，先隐藏再等待画面刷新。"""
        if (self.config.get("hide_panel_on_capture", True)
                and self.ui.panel_visible):
            self.ui.request_hide_panel()
            time.sleep(0.35)

    def capture_screen(self):
        if not self._capture_lock.acquire(blocking=False):
            self._log("screenshot already in progress")
            return
        try:
            self._hide_panel_for_capture()
            img = grab_screen(int(self.config.get("capture_monitor", 1)))
            png = to_png(img)
            clip_id = self._store_image(png, "screenshot")
            if clip_id is None:
                return
            if self.config.get("copy_screenshot_to_clipboard", True):
                dib = image_to_dib(img)
                self.listener.write_clipboard(clipboard_api.set_clipboard_dib, dib)
            print(f"[lumina] screenshot #{clip_id} saved ({len(png) // 1024} KB)",
                  flush=True)
            self.ui.notify("screenshot", clip_id, png=png, source="screenshot")
            self.ui.refresh_panel()
        except Exception:
            traceback.print_exc()
        finally:
            self._capture_lock.release()

    # ---------- 区域截图 ----------
    def start_region_capture(self):
        """热键线程：先冻结屏幕（避免拍到遮罩），再交给 UI 线程显示选区遮罩。"""
        if not self._capture_lock.acquire(blocking=False):
            self._log("capture already in progress")
            return
        try:
            self._hide_panel_for_capture()
            img = grab_screen(1)
            if not self.ui.request_region_capture(to_png(img)):
                self._capture_lock.release()
        except Exception:
            self._capture_lock.release()
            traceback.print_exc()

    def finish_region(self, png, source="region"):
        """后台线程：选区完成，入库 + 回写剪贴板 + 提示。source=download 时不写剪贴板。"""
        try:
            if png is None:
                return
            img = Image.open(io.BytesIO(png))
            w, h = img.size
            clip_id = self._store_image(png, source)
            if clip_id is None:
                return
            self._log(f"{source} #{clip_id} saved ({w}x{h}, {len(png) // 1024} KB)")
            if (source != "download"
                    and self.config.get("copy_screenshot_to_clipboard", True)):
                dib = image_to_dib(img)
                self.listener.write_clipboard(clipboard_api.set_clipboard_dib, dib)
            self.ui.notify("download" if source == "download" else "region",
                           clip_id, png=png, source=source)
            self.ui.refresh_panel()
        finally:
            if self._capture_lock.locked():
                self._capture_lock.release()

    # ---------- 面板动作 ----------
    def set_hide_panel_on_capture(self, value):
        self.config["hide_panel_on_capture"] = bool(value)
        if self.config_path:
            try:
                save_config(self.config, self.config_path)
            except OSError:
                traceback.print_exc()

    def save_settings(self, values):
        """Persist settings edited by the UI settings dialog."""
        if "autostart" in values:
            set_autostart(values["autostart"], self.config_path)
        self.config.update(values)
        if self.config_path:
            save_config(self.config, self.config_path)

    def copy_to_clipboard(self, row):
        """面板回贴/复制：写入剪贴板并抑制自身监听（内容已在库）。

        图片记录写回为图片；归档文件如果实际是图片，也写回为图片，
        否则按文本写回文件路径。
        """
        try:
            data = row["data"] if "data" in row.keys() else None
            file_path = row["file_path"] if "file_path" in row.keys() else ""
            if data is None and row["id"]:
                full_row = self.db.get(row["id"])
                if full_row is not None:
                    row = full_row
                    data = row["data"]
                    file_path = row["file_path"] if "file_path" in row.keys() else ""

            if file_path and data is None:
                abs_path = os.path.join(
                    os.path.dirname(os.path.abspath(self.db.path)),
                    file_path)
                if os.path.exists(abs_path):
                    data = open(abs_path, "rb").read()

            if row["kind"] == "image":
                img = Image.open(io.BytesIO(data))
                payload = image_to_dib(img)
                writer = clipboard_api.set_clipboard_dib
            elif row["category"] == "file" and data:
                try:
                    img = Image.open(io.BytesIO(data))
                    img.load()
                except (OSError, SyntaxError, ValueError):
                    payload = row["content"] or ""
                    writer = clipboard_api.set_clipboard_text
                else:
                    payload = image_to_dib(img)
                    writer = clipboard_api.set_clipboard_dib
            else:
                payload = row["content"] or ""
                writer = clipboard_api.set_clipboard_text
            ok = self.listener.write_clipboard(writer, payload)
            self._log(f"copy back #{row['id']} ({row['kind']}) ok={ok}")
            return ok
        except Exception:
            traceback.print_exc()
            return False

    # ---------- 清理 ----------
    def _cleaner_loop(self):
        interval = max(1, int(self.config.get("cleanup_interval_minutes", 60))) * 60
        try:
            while not self._stop.wait(interval):
                deleted = self.db.cleanup(
                    self.config.get("retention_days", 30),
                    self.config.get("max_rows", 5000),
                )
                if deleted:
                    print(f"[lumina] cleanup: removed {deleted} expired record(s)")
        except Exception:
            traceback.print_exc()
        finally:
            self.db.close()

    def start(self):
        try:
            deleted = self.db.cleanup(
                self.config.get("retention_days", 30),
                self.config.get("max_rows", 5000),
            )
            if deleted:
                print(f"[lumina] startup cleanup: removed {deleted} expired record(s)")
            self._cleaner_thread = threading.Thread(
                target=self._cleaner_loop, name="lumina-cleaner", daemon=True)
            self._cleaner_thread.start()
            self.ui.start()
            if ((self.config.get("popup_enabled", True) or
                 self.config.get("panel_enabled", True) or
                 self.config.get("hotkey_region")) and not self.ui.started):
                raise RuntimeError("UI failed to start")
            self.listener.start()
            if not self.listener.wait_ready(3):
                raise RuntimeError(
                    f"clipboard listener failed to start: {self.listener._startup_error}")
            self.tray.start()
            print("[lumina] clipboard monitor running", flush=True)
            print(f"[lumina] db: {os.path.abspath(self.db.path)}", flush=True)
            print(f"[lumina] screenshot hotkey: {self.config.get('hotkey_capture')}",
                  flush=True)
            print(f"[lumina] region capture hotkey: {self.config.get('hotkey_region')}",
                  flush=True)
            print(f"[lumina] history panel hotkey: {self.config.get('hotkey_panel')}",
                  flush=True)
            print(f"[lumina] retention: {self.config.get('retention_days')} days / "
                  f"max {self.config.get('max_rows')} rows", flush=True)
            print("[lumina] press Ctrl+C to exit", flush=True)
            while not self._stop.wait(0.5):
                if not self.listener.is_alive():
                    raise RuntimeError("clipboard listener stopped unexpectedly")
        except KeyboardInterrupt:
            print("\n[lumina] shutting down...")
        finally:
            self._stop.set()
            self._file_queue.put(None)
            self.tray.stop()
            if self.listener.ident is not None:
                self.listener.stop()
                self.listener.join(timeout=5)
            self.ui.stop()
            if self._cleaner_thread:
                self._cleaner_thread.join(timeout=2)
            self._file_worker.join(timeout=5)
            self.db.checkpoint()
            self.db.close()
