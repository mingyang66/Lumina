import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import main
from lumina.database import Database
from lumina import settings


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(str(self.root / 'history.db'))
        self.db.init_schema()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def pending(self, name='source'):
        return self.db.add_file_pending(str(self.root / name))

    def test_memory_database_does_not_create_directories(self):
        with patch('lumina.database.os.makedirs', side_effect=AssertionError('内存库不应创建目录')):
            db = Database(':memory:')
            try:
                db.init_schema()
                clip = db.add_file_pending(str(self.root / 'memory-source'))
                with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
                    db.complete_file(clip, b'large')
                self.assertEqual(db.get_file_data(clip)['data'], b'large')
                db.delete(clip)
            finally:
                db.close()

    def test_single_legacy_blob_columns(self):
        for columns, values, expected in (
                ('image BLOB', (b'image',), b'image'),
                ('file_data BLOB', (b'',), b''),
                ('image BLOB, file_data BLOB', (None, b'file'), b'file'),
                ('data BLOB, image BLOB', (b'existing', b'old'), b'existing')):
            with self.subTest(columns=columns):
                db = Database(':memory:')
                try:
                    db.conn.execute(
                        'CREATE TABLE clipboard (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                        'kind TEXT, content TEXT, hash TEXT, source TEXT, created_at TEXT, '
                        + columns + ')')
                    placeholders = ','.join('?' for _ in values)
                    db.conn.execute("INSERT INTO clipboard VALUES (1,'image',NULL,'h','','2020-01-01',"
                                    + placeholders + ')', values)
                    db.conn.commit()
                    db.init_schema()
                    self.assertEqual(db.get(1)['data'], expected)
                    db.init_schema()
                    self.assertEqual(db.get(1)['data'], expected)
                finally:
                    db.close()

    def test_duplicate_migration_preserves_history(self):
        db = Database(':memory:')
        try:
            db.conn.execute('CREATE TABLE clipboard (id INTEGER PRIMARY KEY, kind TEXT, '
                            'content TEXT, data BLOB, hash TEXT, source TEXT, created_at TEXT)')
            db.conn.executemany("INSERT INTO clipboard VALUES (?,'text','same',NULL,'h','','2020-01-01')",
                                [(1,), (2,)])
            db.conn.commit()
            with self.assertRaisesRegex(ValueError, '重复历史'):
                db.init_schema()
            self.assertEqual(db.conn.execute('SELECT COUNT(*) FROM clipboard').fetchone()[0], 2)
        finally:
            db.close()

    def test_delete_and_replace_archive(self):
        with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
            clip = self.pending()
            self.assertTrue(self.db.complete_file(clip, b'large'))
            archived = Path(self.db.get_file_data(clip)['file_path'])
            self.assertEqual(archived.read_bytes(), b'large')
            self.db.complete_file(clip, b'')
            self.assertFalse(archived.exists())
            self.assertEqual(self.db.get_file_data(clip)['data'], b'')
            self.db.complete_file(clip, b'large')
            archived = Path(self.db.get_file_data(clip)['file_path'])
            self.assertTrue(self.db.delete(clip))
            self.assertFalse(archived.exists())
            self.assertFalse(self.db.complete_file(clip, b'late'))
            self.assertFalse(list((self.root / 'file_store').iterdir()))

    def test_cleanup_archives_and_pinned(self):
        with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
            ids = [self.pending(str(i)) for i in range(3)]
            for clip in ids:
                self.db.complete_file(clip, b'large')
            paths = [Path(self.db.get_file_data(clip)['file_path']) for clip in ids]
            self.db.set_pinned(ids[0])
            self.assertEqual(self.db.cleanup(0, 1), 1)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[1].exists())
            with self.db.conn:
                self.db.conn.execute("UPDATE clipboard SET created_at='2000-01-01'")
            self.assertEqual(self.db.cleanup(1, 0), 1)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[2].exists())

    def test_reclamation_permission_error_preserves_committed_results(self):
        for operation in ('complete_file', 'cleanup', 'delete'):
            with self.subTest(operation=operation):
                clip = self.pending(operation)
                with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
                    self.assertTrue(self.db.complete_file(clip, b'old archive'))
                archived = Path(self.db.get_file_data(clip)['file_path'])
                with patch('lumina.database.os.remove', side_effect=PermissionError('文件被占用')) as remove, \
                        self.assertLogs('lumina.database', level='WARNING') as captured:
                    if operation == 'complete_file':
                        self.assertTrue(self.db.complete_file(clip, b''))
                        row = self.db.get(clip)
                        self.assertEqual(row['file_status'], 'ready')
                        self.assertEqual(row['data'], b'')
                        self.assertEqual(row['file_path'], '')
                    elif operation == 'cleanup':
                        with self.db.conn:
                            self.db.conn.execute(
                                "UPDATE clipboard SET created_at='2000-01-01' WHERE id=?", (clip,))
                        self.assertEqual(self.db.cleanup(1, 0), 1)
                        self.assertIsNone(self.db.get(clip))
                    else:
                        self.assertTrue(self.db.delete(clip))
                        self.assertIsNone(self.db.get(clip))
                    remove.assert_called_once_with(str(archived))
                self.assertFalse(self.db.conn.in_transaction)
                self.assertEqual(archived.read_bytes(), b'old archive')
                self.assertEqual(len(captured.records), 1)
                self.assertIn('待后续回收', captured.records[0].getMessage())
                self.assertIn(str(archived), captured.records[0].getMessage())
                self.assertIsInstance(captured.records[0].exc_info[1], PermissionError)

    def test_shared_old_archive_preserved_until_last_delete(self):
        with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
            first, second = self.pending('one'), self.pending('two')
            self.db.complete_file(first, b'large')
            path = self.db.get(first)['file_path']
            with self.db.conn:
                self.db.conn.execute('UPDATE clipboard SET file_path=? WHERE id=?', (path, second))
            self.db.delete(first)
            self.assertTrue(self.db.resolve_file_path(path))
            self.assertTrue(Path(self.db.resolve_file_path(path)).exists())
            self.db.delete(second)
            self.assertFalse(Path(self.db.resolve_file_path(path)).exists())

    def test_escape_is_not_read_or_deleted(self):
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'private')
        for stored in ('../outside.bin', 'outside.bin', str(outside), 'file_store/../outside.bin'):
            with self.subTest(stored=stored):
                clip = self.pending(stored)
                with self.db.conn:
                    self.db.conn.execute('UPDATE clipboard SET file_path=? WHERE id=?', (stored, clip))
                with self.assertRaises(ValueError):
                    self.db.get_file_data(clip)
                self.db.delete(clip)
                self.assertEqual(outside.read_bytes(), b'private')

    def test_symlink_escape(self):
        store = self.root / 'file_store'
        store.mkdir()
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'private')
        link = store / 'link.bin'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('当前环境不允许创建符号链接')
        clip = self.pending()
        with self.db.conn:
            self.db.conn.execute("UPDATE clipboard SET file_path='file_store/link.bin' WHERE id=?", (clip,))
        with self.assertRaises(ValueError):
            self.db.get_file_data(clip)
        self.db.delete(clip)
        self.assertEqual(outside.read_bytes(), b'private')

    def test_archive_delete_race_across_connections(self):
        clip = self.pending()
        entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = self.db._store_file_on_disk

        def delayed(clip_id, data):
            entered.set()
            if not release.wait(10):
                raise AssertionError('归档测试同步超时')
            return original(clip_id, data)

        def archive():
            try:
                self.db.complete_file(clip, b'large')
            except Exception as exc:
                errors.append(exc)
            finally:
                self.db.close()

        def delete():
            other = Database(self.db.path)
            try:
                deleting.set()
                other.delete(clip)
            except Exception as exc:
                errors.append(exc)
            finally:
                other.close()

        with patch('lumina.database.FILE_STORE_THRESHOLD', 1), patch.object(self.db, '_store_file_on_disk', delayed):
            worker = threading.Thread(target=archive)
            remover = threading.Thread(target=delete)
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                remover.start()
                self.assertTrue(deleting.wait(10))
            finally:
                release.set()
                worker.join(10)
                if remover.ident is not None:
                    remover.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(remover.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(self.db.get(clip))
        self.assertEqual(list((self.root / 'file_store').iterdir()), [])

    def test_failed_archive_update_rolls_back_disk_file(self):
        clip = self.pending()
        self.db.conn.executescript("CREATE TRIGGER reject_archive BEFORE UPDATE ON clipboard "
                                   "BEGIN SELECT RAISE(ABORT, 'reject'); END;")
        with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.complete_file(clip, b'large')
        self.assertEqual(self.db.get(clip)['file_status'], 'pending')
        self.assertEqual(list((self.root / 'file_store').iterdir()), [])

    def test_delete_rollback_keeps_archive(self):
        clip = self.pending()
        with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
            self.db.complete_file(clip, b'large')
        archived = Path(self.db.get_file_data(clip)['file_path'])
        self.db.conn.executescript("CREATE TRIGGER reject_delete BEFORE DELETE ON clipboard "
                                   "BEGIN SELECT RAISE(ABORT, 'reject'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.delete(clip)
        self.assertIsNotNone(self.db.get(clip))
        self.assertEqual(archived.read_bytes(), b'large')

    def test_archive_root_symlink_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        store = self.root / 'file_store'
        try:
            store.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('当前环境不允许创建符号链接')
        clip = self.pending()
        with patch('lumina.database.FILE_STORE_THRESHOLD', 1):
            with self.assertRaises(ValueError):
                self.db.complete_file(clip, b'large')
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_process_exit_codes(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'db_path': self.db.path}), encoding='utf-8')
        clip = self.pending()
        self.db.complete_file(clip, b'')
        target = self.root / 'empty.out'
        command = [sys.executable, '-B', str(Path(main.__file__).resolve()), '-c', str(config), 'export']
        result = subprocess.run(command + [str(clip), str(target)], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), b'')
        missing = self.pending('missing')
        result = subprocess.run(command + [str(missing), str(self.root / 'missing.out')],
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn('export failed', result.stderr)
        self.assertFalse((self.root / 'missing.out').exists())

    def test_empty_export_and_missing_data_nonzero(self):
        cfg = {'db_path': self.db.path}
        clip = self.pending()
        self.db.complete_file(clip, b'')
        output = self.root / 'export.bin'
        with contextlib.redirect_stdout(io.StringIO()):
            main.cmd_export(cfg, clip, str(output))
        self.assertEqual(output.read_bytes(), b'')
        for kind in ('file', 'image', 'absent'):
            clip = self.pending(kind) if kind == 'file' else self.db.add_image(b'x')
            if kind == 'image':
                with self.db.conn:
                    self.db.conn.execute('UPDATE clipboard SET data=NULL WHERE id=?', (clip,))
            if kind == 'absent':
                clip = 999999
            target = self.root / (kind + '.out')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main.cmd_export(cfg, clip, str(target))
            self.assertEqual(error.exception.code, 1)
            self.assertFalse(target.exists())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def write_config(self, path, db_path='data/history.db'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'db_path': db_path, 'autostart': False}), encoding='utf-8')

    def test_relative_and_legacy_selection(self):
        config = self.root / 'configdir' / 'config.json'
        self.write_config(config)
        target = config.parent / 'data' / 'history.db'
        self.assertEqual(settings.load_config(config)['db_path'], str(target))
        legacy = self.root / 'data' / 'history.db'
        legacy.parent.mkdir()
        legacy.write_bytes(b'legacy')
        with self.assertWarns(RuntimeWarning):
            cfg = settings.load_config(config)
        self.assertEqual(cfg['db_path'], str(legacy))
        self.assertEqual(json.loads(config.read_text(encoding='utf-8'))['db_path'], str(legacy))
        settings.save_config(cfg, config)
        self.assertEqual(settings.load_config(config)['db_path'], str(legacy))
        self.write_config(config)
        target.parent.mkdir()
        target.write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, '多个历史数据库'):
            settings.load_config(config)
        self.assertEqual(legacy.read_bytes(), b'legacy')
        self.assertEqual(target.read_bytes(), b'other')

    def test_frozen_bootstrap_stable_and_legacy_history(self):
        executable = self.root / 'portable' / 'Lumina.exe'
        bundle = self.root / 'unpack'
        self.write_config(bundle / 'config.json')
        self.write_config(executable.parent / 'config.json')
        legacy = executable.parent / 'data' / 'history.db'
        legacy.parent.mkdir()
        legacy.write_bytes(b'old')
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, '_MEIPASS', str(bundle), create=True), \
                patch.object(sys, 'executable', str(executable)), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')}):
            stable = Path(settings.default_config_path())
            with self.assertWarns(RuntimeWarning):
                cfg = settings.load_config()
            self.assertNotIn(str(bundle), str(stable))
            self.assertTrue(stable.is_file())
            self.assertEqual(cfg['db_path'], str(legacy))
            cfg['autostart'] = True
            settings.save_config(cfg)
            self.assertEqual(settings.load_config()['autostart'], True)
            self.assertEqual(settings.load_config()['db_path'], str(legacy))
        self.assertEqual(legacy.read_bytes(), b'old')

    def test_frozen_new_database_ignores_template_unpack_dir(self):
        executable = self.root / 'portable' / 'Lumina.exe'
        bundle = self.root / 'unpack'
        self.write_config(bundle / 'config.json')
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, '_MEIPASS', str(bundle), create=True), \
                patch.object(sys, 'executable', str(executable)), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')}):
            stable = Path(settings.default_config_path())
            cfg = settings.load_config()
            self.assertEqual(cfg['db_path'], str(stable.parent / 'data' / 'history.db'))

    def test_autostart_uses_stable_config_without_registry_access(self):
        fake_registry = unittest.mock.MagicMock()
        executable = self.root / 'portable' / 'Lumina.exe'
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(executable)), \
                patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'local')}), \
                patch.dict(sys.modules, {'winreg': fake_registry}), patch.object(settings.os, 'name', 'nt'):
            settings.set_autostart(True, None)
            command = fake_registry.SetValueEx.call_args.args[-1]
            self.assertIn(settings.default_config_path(), command)
            self.assertIn(str(executable), command)


if __name__ == '__main__':
    unittest.main()
