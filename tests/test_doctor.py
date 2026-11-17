"""``main.py doctor`` 的纯逻辑测试：不碰真实环境，全部注入替身。"""
import unittest
from unittest.mock import patch

from lumina import doctor


def _find_spec_with(present):
    def find_spec(name, *_args, **_kwargs):
        return object() if name in present else None
    return find_spec


class DependencyRowsTests(unittest.TestCase):
    def test_missing_dependency_is_flagged(self):
        rows = doctor.dependency_rows(_find_spec_with({"PIL", "mss"}))
        status = {label: state for label, _purpose, state in rows}
        self.assertEqual(status["依赖 PIL"], doctor.OK)
        self.assertEqual(status["依赖 mss"], doctor.OK)
        self.assertEqual(status["依赖 pytesseract"], doctor.BAD)

    def test_every_runtime_module_is_listed(self):
        rows = doctor.dependency_rows(_find_spec_with(set()))
        labels = [label for label, _purpose, _state in rows]
        self.assertEqual(len(labels), len(doctor.RUNTIME_MODULES))
        self.assertIn("依赖 pytesseract", labels)


class InterpreterRowsTests(unittest.TestCase):
    def test_venv_is_ok_and_system_python_warns(self):
        with patch.object(doctor.sys, "prefix", "C:/Python313"), \
                patch.object(doctor.sys, "base_prefix", "C:/Python313"):
            rows = doctor.interpreter_rows()
        labels = {label: state for label, _value, state in rows}
        self.assertEqual(labels["解释器"], doctor.WARN)
        self.assertIn("虚拟环境", labels)

        with patch.object(doctor.sys, "prefix", "C:/proj/.venv"), \
                patch.object(doctor.sys, "base_prefix", "C:/Python313"):
            rows = doctor.interpreter_rows()
        labels = {label: state for label, _value, state in rows}
        self.assertEqual(labels["解释器"], doctor.OK)
        self.assertNotIn("虚拟环境", labels)


class TesseractTests(unittest.TestCase):
    def test_missing_executable_is_failure(self):
        rows = doctor.tesseract_rows(None, (None, None, "未找到"))
        self.assertEqual(rows[0][0], "Tesseract")
        self.assertEqual(rows[0][2], doctor.BAD)

    def test_chinese_language_pack_is_ok(self):
        rows = doctor.tesseract_rows(
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            ("tesseract v5.4.0", ["chi_sim", "eng"], None))
        status = {label: state for label, _value, state in rows}
        self.assertEqual(status["Tesseract"], doctor.OK)
        self.assertEqual(status["语言包"], doctor.OK)

    def test_missing_chinese_language_pack_warns(self):
        rows = doctor.tesseract_rows(r"C:\t\tesseract.exe", ("tesseract v5", ["eng"], None))
        status = {label: state for label, value, state in rows}
        self.assertEqual(status["语言包"], doctor.WARN)

    def test_probe_failure_is_reported(self):
        rows = doctor.tesseract_rows(r"C:\t\tesseract.exe", (None, None, "拒绝访问"))
        self.assertEqual(rows[0][2], doctor.BAD)
        self.assertIn("拒绝访问", rows[0][1])

    def test_find_prefers_known_install_paths(self):
        known = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        found = doctor.find_tesseract(exists=lambda path: path == known,
                                      which=lambda _name: r"C:\bin\tesseract.exe")
        self.assertEqual(found, known)

    def test_find_falls_back_to_path(self):
        found = doctor.find_tesseract(exists=lambda _path: False,
                                      which=lambda _name: r"C:\bin\tesseract.exe")
        self.assertEqual(found, r"C:\bin\tesseract.exe")

    def test_find_returns_none_when_nothing_is_installed(self):
        self.assertIsNone(doctor.find_tesseract(exists=lambda _p: False, which=lambda _n: None))


class StorageRowsTests(unittest.TestCase):
    def test_missing_database_is_warning_not_failure(self):
        rows = doctor.storage_rows({"db_path": "C:/definitely/not/here.db"}, None)
        self.assertEqual(rows[0][0], "数据库")
        self.assertEqual(rows[0][2], doctor.WARN)

    def test_missing_db_path_is_failure(self):
        rows = doctor.storage_rows({}, None)
        self.assertEqual(rows[0][2], doctor.BAD)

    def test_memory_database_is_ok(self):
        rows = doctor.storage_rows({"db_path": ":memory:"}, None)
        self.assertEqual(rows[0][2], doctor.OK)

    def test_config_file_presence_is_reported(self):
        rows = doctor.storage_rows({"db_path": ":memory:"}, "C:/not/here.json")
        status = {label: state for label, _value, state in rows}
        self.assertEqual(status["配置文件"], doctor.BAD)


class ReportTests(unittest.TestCase):
    def test_report_marks_status_and_counts(self):
        rows = [
            ("环境", "解释器", "C:/python.exe", doctor.OK),
            ("依赖", "依赖 pytesseract", "截图提取文字", doctor.BAD),
            ("OCR", "Tesseract", "未找到", doctor.WARN),
        ]
        text = doctor.format_report(rows)
        self.assertIn("[ OK ]", text)
        self.assertIn("[FAIL]", text)
        self.assertIn("[WARN]", text)
        self.assertIn("3 项检查：1 项失败，1 项警告", text)
        # 分组标题只出现一次（表格按分组分段）。
        self.assertEqual(text.count("环境"), 1)
        self.assertEqual(text.count("OCR"), 1)

    def test_empty_report_is_empty_string(self):
        self.assertEqual(doctor.format_report([]), "")

    def test_collect_reports_all_sections(self):
        rows = doctor.collect(
            config={"db_path": ":memory:"},
            config_path=None,
            log_file="C:/logs/lumina.log",
            find_spec=_find_spec_with({"PIL"}),
            exists=lambda _p: False,
            which=lambda _n: None,
            run=None,
        )
        sections = {section for section, _l, _v, _s in rows}
        self.assertEqual(sections, {"环境", "依赖", "OCR", "存储"})
        self.assertIn(("存储", "日志"), [(section, label) for section, label, _v, _s in rows])


if __name__ == "__main__":
    unittest.main()
