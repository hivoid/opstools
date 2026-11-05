#!/usr/bin/env python3
"""dly.py 的单元测试，覆盖 PRD 第 9 节验收用例。

运行方式：
    python3 -m unittest test_dly.py -v
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import dly

# 固定“今天”，避免测试依赖运行日期
TODAY = date(2026, 11, 5)

# 共用目录场景下各类型列表的期望输出（字典序逆序）
EXPECTED_NORMAL = ["global.md", "20261105.md", "20251231.md"]
EXPECTED_TASK = ["global.task.md", "20261105.task.md", "20261001.task.md"]
EXPECTED_ALL = [
    "global.task.md",
    "global.md",
    "20261105.task.md",
    "20261105.md",
    "20261001.task.md",
    "20251231.md",
]


class ParseDateExprTests(unittest.TestCase):
    """PRD 第 7 节：日期表达式解析。"""

    def test_empty_is_today(self):
        self.assertEqual(dly.parse_date_expr("", TODAY), TODAY)

    def test_relative_days(self):
        self.assertEqual(dly.parse_date_expr("+1", TODAY), date(2026, 11, 6))
        self.assertEqual(dly.parse_date_expr("-1", TODAY), date(2026, 11, 4))
        self.assertEqual(dly.parse_date_expr("+3d", TODAY), date(2026, 11, 8))
        self.assertEqual(dly.parse_date_expr("-3d", TODAY), date(2026, 11, 2))
        self.assertEqual(dly.parse_date_expr("+0", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("-0", TODAY), TODAY)

    def test_relative_weeks(self):
        self.assertEqual(dly.parse_date_expr("+2w", TODAY), date(2026, 11, 19))
        self.assertEqual(dly.parse_date_expr("-1w", TODAY), date(2026, 10, 29))

    def test_relative_months(self):
        self.assertEqual(dly.parse_date_expr("+1m", TODAY), date(2026, 12, 5))
        self.assertEqual(dly.parse_date_expr("-1m", TODAY), date(2026, 10, 5))
        self.assertEqual(dly.parse_date_expr("+2m", TODAY), date(2027, 1, 5))
        self.assertEqual(dly.parse_date_expr("-11m", TODAY), date(2025, 12, 5))

    def test_relative_months_truncate_to_month_end(self):
        # 目标月不存在该日时截断到目标月最后一天
        self.assertEqual(dly.parse_date_expr("+1m", date(2026, 1, 31)), date(2026, 2, 28))
        self.assertEqual(dly.parse_date_expr("+1m", date(2026, 3, 31)), date(2026, 4, 30))
        self.assertEqual(dly.parse_date_expr("-1m", date(2026, 3, 31)), date(2026, 2, 28))

    def test_relative_years_leap_day(self):
        # 非闰年的 2 月 29 日截断为 2 月 28 日，闰年保持 2 月 29 日
        self.assertEqual(dly.parse_date_expr("+1y", date(2024, 2, 29)), date(2025, 2, 28))
        self.assertEqual(dly.parse_date_expr("-1y", date(2024, 2, 29)), date(2023, 2, 28))
        self.assertEqual(dly.parse_date_expr("+4y", date(2024, 2, 29)), date(2028, 2, 29))
        self.assertEqual(dly.parse_date_expr("+1y", TODAY), date(2027, 11, 5))

    def test_dd_uses_current_month(self):
        self.assertEqual(dly.parse_date_expr("5", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("05", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("13", TODAY), date(2026, 11, 13))

    def test_mmdd_left_padded(self):
        self.assertEqual(dly.parse_date_expr("1105", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("101", TODAY), date(2026, 1, 1))
        self.assertEqual(dly.parse_date_expr("0101", TODAY), date(2026, 1, 1))

    def test_yymmdd_is_20yy(self):
        self.assertEqual(dly.parse_date_expr("110312", TODAY), date(2011, 3, 12))
        self.assertEqual(dly.parse_date_expr("991231", TODAY), date(2099, 12, 31))

    def test_yyyymmdd_full_year(self):
        self.assertEqual(dly.parse_date_expr("20261105", TODAY), TODAY)
        self.assertEqual(dly.parse_date_expr("19991231", TODAY), date(1999, 12, 31))
        self.assertEqual(dly.parse_date_expr("21000101", TODAY), date(2100, 1, 1))

    def test_full_date_separators(self):
        for expr in ("2026-11-05", "2026/11/05", "2026.11.05"):
            with self.subTest(expr=expr):
                self.assertEqual(dly.parse_date_expr(expr, TODAY), TODAY)

    def test_invalid_calendar_dates(self):
        for expr in ("20261301", "20260230", "20261131", "2026-13-01", "2026.00.10", "32"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_mixed_separators_rejected(self):
        for expr in ("2026-11/05", "2026/11-05", "2026.11-05"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_unrecognized_formats(self):
        for expr in ("n", "foo", "1e5", "2026-1-5", "26-11-05", "++1", "+", "-1x", "2D"):
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(ValueError, "无效日期"):
                    dly.parse_date_expr(expr, TODAY)

    def test_error_message_contains_expr(self):
        with self.assertRaisesRegex(ValueError, "^无效日期: 20261301$"):
            dly.parse_date_expr("20261301", TODAY)


class NotesDirTestBase(unittest.TestCase):
    """提供临时笔记目录、配置 patch 与 CLI 调用辅助的测试基类。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.notes_dir = self.base / "daily"
        self.tasks_dir = self.base / "tasks"
        self.notes_dir.mkdir()
        self._make_files(self.notes_dir)
        self._patch_dir("NOTES_DIR", self.notes_dir)
        self._patch_dir("TASK_DIR", None)

    def _patch_dir(self, name: str, path: Path | None) -> None:
        """测试期间替换 dly 模块级目录配置，结束后自动恢复。"""
        patcher = mock.patch.object(dly, name, path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *args: str) -> tuple[int, str, str]:
        """调用 dly.main 并捕获 stdout/stderr，返回 (退出码, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dly.main(list(args))
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _make_files(dir_path: Path) -> None:
        """创建覆盖命名规则边界的文件集合。"""
        for name in (
            "global.md",
            "global.task.md",
            "20261105.md",
            "20261105.task.md",
            "20261001.task.md",
            "20251231.md",
            "20261301.md",   # 非法日期，不应列出
            "20261105.txt",  # 后缀不符，不应列出
        ):
            (dir_path / name).write_text("", encoding="utf-8")
        (dir_path / "subdir").mkdir()  # 子目录，不应列出


class PathTests(NotesDirTestBase):
    """PRD 第 3 节：路径生成（TASK_DIR 为 None 与独立目录两种情况）。"""

    def test_paths_shared_dir(self):
        # TASK_DIR 为 None 时任务路径与普通路径共用 NOTES_DIR
        d5 = date(2026, 11, 5)
        self.assertEqual(dly.normal_note_path(d5), self.notes_dir / "20261105.md")
        self.assertEqual(dly.task_note_path(d5), self.notes_dir / "20261105.task.md")
        self.assertEqual(dly.global_note_path(), self.notes_dir / "global.md")
        self.assertEqual(dly.global_task_path(), self.notes_dir / "global.task.md")

    def test_paths_separate_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # 仅生成路径，不要求目录存在
        d5 = date(2026, 11, 5)
        self.assertEqual(dly.normal_note_path(d5), self.notes_dir / "20261105.md")
        self.assertEqual(dly.task_note_path(d5), self.tasks_dir / "20261105.task.md")
        self.assertEqual(dly.global_note_path(), self.notes_dir / "global.md")
        self.assertEqual(dly.global_task_path(), self.tasks_dir / "global.task.md")

    def test_full_year_padding(self):
        self.assertEqual(dly.normal_note_path(date(999, 1, 2)).name, "09990102.md")
        self.assertEqual(dly.task_note_path(date(2100, 1, 1)).name, "21000101.task.md")

    def test_task_dir_defaults_to_notes_dir(self):
        self.assertEqual(dly._task_dir(), dly.NOTES_DIR)


class ListNotesTests(NotesDirTestBase):
    """PRD FR-5：list_notes 类型、筛选与错误。"""

    def test_normal_type(self):
        self.assertEqual(dly.list_notes("normal", "*"), EXPECTED_NORMAL)

    def test_task_type(self):
        self.assertEqual(dly.list_notes("task", "*"), EXPECTED_TASK)

    def test_all_type_dedup(self):
        self.assertEqual(dly.list_notes("all", "*"), EXPECTED_ALL)

    def test_substring_pattern(self):
        self.assertEqual(dly.list_notes("normal", "2026"), ["20261105.md"])
        self.assertEqual(dly.list_notes("task", ".task"), EXPECTED_TASK)

    def test_glob_pattern(self):
        self.assertEqual(dly.list_notes("task", "*01.task.md"), ["20261001.task.md"])
        self.assertEqual(dly.list_notes("all", "20261105.*"), ["20261105.task.md", "20261105.md"])

    def test_invalid_and_foreign_files_excluded(self):
        names = dly.list_notes("all", "*")
        self.assertNotIn("20261301.md", names)
        self.assertNotIn("20261105.txt", names)
        self.assertNotIn("subdir", names)

    def test_no_match_returns_empty(self):
        self.assertEqual(dly.list_notes("normal", "zzz"), [])

    def test_missing_notes_dir(self):
        self._patch_dir("NOTES_DIR", self.base / "missing-daily")
        with self.assertRaisesRegex(dly.NotesDirError, "笔记目录不存在"):
            dly.list_notes("normal", "*")

    def test_missing_task_dir_separate(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        with self.assertRaisesRegex(dly.NotesDirError, "任务目录不存在"):
            dly.list_notes("task", "*")

    def test_all_type_separate_dirs(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        (self.tasks_dir / "20260101.task.md").write_text("", encoding="utf-8")
        (self.tasks_dir / "global.md").write_text("", encoding="utf-8")  # 任务目录中的普通命名文件不算任务类型
        # 目录分离后：普通类型仅来自 notes_dir，任务类型仅来自 tasks_dir；
        # notes_dir 中的任务命名文件与 tasks_dir 中的 global.md 均不属于任何类型
        self.assertEqual(
            dly.list_notes("all", "*"),
            ["global.md", "20261105.md", "20260101.task.md", "20251231.md"],
        )


class RunEditorAndViewFileTests(NotesDirTestBase):
    """PRD FR-4 / FR-6：编辑器与查看器调用。"""

    def test_run_editor_invokes_cmd_with_path(self):
        with mock.patch.object(dly, "EDITOR_CMD", ["fake-editor"]):
            with mock.patch.object(dly, "subprocess") as sp:
                sp.run.return_value = mock.Mock(returncode=3)
                code = dly.run_editor(self.notes_dir / "a.md")
        self.assertEqual(code, 3)
        sp.run.assert_called_once_with(["fake-editor", str(self.notes_dir / "a.md")])

    def test_run_editor_start_failure(self):
        with mock.patch.object(dly, "EDITOR_CMD", ["dly-no-such-editor"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dly.run_editor(self.notes_dir / "a.md")
        self.assertEqual(code, 1)
        self.assertIn("编辑器启动失败", err.getvalue())

    def test_view_file_uses_pydoc_pager(self):
        target = self.notes_dir / "20261105.md"
        target.write_text("第一行\n", encoding="utf-8")
        with mock.patch.object(dly, "pydoc") as pydoc_mock:
            code = dly.view_file(target)
        self.assertEqual(code, 0)
        pydoc_mock.pager.assert_called_once_with("第一行\n")

    def test_view_file_replaces_invalid_utf8(self):
        target = self.notes_dir / "20261105.md"
        target.write_bytes(b"\xff\xfe tail")
        with mock.patch.object(dly, "pydoc") as pydoc_mock:
            code = dly.view_file(target)
        self.assertEqual(code, 0)
        self.assertIn("tail", pydoc_mock.pager.call_args.args[0])

    def test_view_file_external_cmd(self):
        target = self.notes_dir / "global.md"
        target.write_text("hello dly", encoding="utf-8")
        with mock.patch.object(dly, "VIEWER_CMD", ["cat"]):
            # 子进程继承真实的 fd 1，redirect_stdout 捕获不到；需在 fd 级别重定向
            with tempfile.TemporaryFile("w+") as tmp_out:
                saved_fd = os.dup(1)
                try:
                    os.dup2(tmp_out.fileno(), 1)
                    code = dly.view_file(target)
                finally:
                    os.dup2(saved_fd, 1)
                    os.close(saved_fd)
                    tmp_out.seek(0)
                    captured = tmp_out.read()
        self.assertEqual(code, 0)
        self.assertEqual(captured, "hello dly")

    def test_view_file_external_cmd_invokes_with_path(self):
        with mock.patch.object(dly, "VIEWER_CMD", ["less", "-R"]):
            with mock.patch.object(dly, "subprocess") as sp:
                sp.run.return_value = mock.Mock(returncode=2)
                code = dly.view_file(self.notes_dir / "a.md")
        self.assertEqual(code, 2)
        sp.run.assert_called_once_with(["less", "-R", str(self.notes_dir / "a.md")])

    def test_view_file_external_start_failure(self):
        with mock.patch.object(dly, "VIEWER_CMD", ["dly-no-such-viewer"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dly.view_file(self.notes_dir / "a.md")
        self.assertEqual(code, 1)
        self.assertIn("查看命令启动失败", err.getvalue())

    def test_view_file_external_exit_code_passthrough(self):
        with mock.patch.object(dly, "VIEWER_CMD", ["cat"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dly.view_file(self.base / "no-such-file.md")
        self.assertEqual(code, 1)


class CliListTests(NotesDirTestBase):
    """PRD 4.2 / 9.2：列表模式 CLI。"""

    def test_list_normal(self):
        code, out, err = self._run("-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_NORMAL)
        self.assertEqual(err, "")

    def test_list_all_flag(self):
        for flag in ("-a", "--all"):
            with self.subTest(flag=flag):
                code, out, err = self._run("-l", flag)
                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines(), EXPECTED_ALL)

    def test_list_all_with_pattern(self):
        code, out, _ = self._run("-l", "--all", "2026")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261105.task.md", "20261105.md", "20261001.task.md"])

    def test_list_normal_with_pattern(self):
        code, out, _ = self._run("-l", "2026")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261105.md"])

    def test_task_list(self):
        code, out, _ = self._run("task", "-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_TASK)

    def test_task_list_short_flag_and_substring(self):
        code, out, _ = self._run("t", "-l", ".task")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_TASK)

    def test_task_list_glob(self):
        code, out, _ = self._run("task", "-l", "*01.task.md")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261001.task.md"])

    def test_global_list_is_normal_type(self):
        code, out, _ = self._run("global", "-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), EXPECTED_NORMAL)

    def test_list_no_match(self):
        code, out, err = self._run("-l", "nomatch")
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的文件: nomatch", err)

    def test_list_all_no_match(self):
        code, _, err = self._run("-l", "-a", "zzz")
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的文件: zzz", err)

    def test_list_with_view_rejected(self):
        for args in (("-l", "-v"), ("-l", "--view"), ("task", "-l", "-v"), ("task", "-l", "p", "-v")):
            with self.subTest(args=args):
                code, _, _ = self._run(*args)
                self.assertEqual(code, 1)

    def test_task_list_rejects_all(self):
        code, _, err = self._run("task", "-l", "-a")
        self.assertEqual(code, 1)
        self.assertIn("-a", err)

    def test_duplicate_all_rejected(self):
        code, _, err = self._run("-l", "-a", "-a")
        self.assertEqual(code, 1)
        self.assertIn("-a", err)

    def test_task_list_separate_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        (self.tasks_dir / "20261105.task.md").write_text("", encoding="utf-8")
        code, out, _ = self._run("task", "-l")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["20261105.task.md"])

    def test_task_list_missing_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        code, _, err = self._run("task", "-l")
        self.assertEqual(code, 1)
        self.assertIn(f"任务目录不存在: {self.tasks_dir}", err)


class CliOpenTests(NotesDirTestBase):
    """PRD 4.1 / 9.1：打开与查看模式的参数路由。"""

    def test_edit_today(self):
        expected = dly.normal_note_path(date.today())
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run()
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_edit_expr_routes_to_normal_note(self):
        for expr in ("5", "1105", "110312", "19991231", "21000101", "2026-11-05",
                     "2026/11/05", "2026.11.05", "+2w", "-1"):
            with self.subTest(expr=expr):
                expected = dly.normal_note_path(dly.parse_date_expr(expr, date.today()))
                with mock.patch.object(dly, "run_editor", return_value=0) as editor:
                    code, _, _ = self._run(expr)
                self.assertEqual(code, 0)
                editor.assert_called_once_with(expected)

    def test_edit_task_today_and_expr(self):
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("t")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(dly.task_note_path(date.today()))
        expected = dly.task_note_path(date(2026, 11, 5))
        with mock.patch.object(dly, "run_editor", return_value=0) as editor:
            code, _, _ = self._run("task", "2026-11-05")
        self.assertEqual(code, 0)
        editor.assert_called_once_with(expected)

    def test_edit_global_normal_and_task_both_orders(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()  # 编辑模式同样要求任务目录存在（PRD 第 2 节）
        cases = (
            (("global",), self.notes_dir / "global.md"),
            (("g",), self.notes_dir / "global.md"),
            (("global", "task"), self.tasks_dir / "global.task.md"),
            (("g", "t"), self.tasks_dir / "global.task.md"),
            (("task", "global"), self.tasks_dir / "global.task.md"),
            (("t", "g"), self.tasks_dir / "global.task.md"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                with mock.patch.object(dly, "run_editor", return_value=0) as editor:
                    code, _, _ = self._run(*args)
                self.assertEqual(code, 0)
                editor.assert_called_once_with(expected)

    def test_view_routes_to_view_file(self):
        target = self.notes_dir / "20261105.md"  # _make_files 已创建
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("-v", "20261105")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_view_flag_any_position(self):
        target = self.notes_dir / "20261105.md"
        for args in (("-v", "2026-11-05"), ("2026-11-05", "-v")):
            with self.subTest(args=args):
                with mock.patch.object(dly, "view_file", return_value=0) as viewer:
                    code, _, _ = self._run(*args)
                self.assertEqual(code, 0)
                viewer.assert_called_once_with(target)

    def test_view_global(self):
        target = self.notes_dir / "global.md"
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("g", "-v")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_view_global_task_both_orders(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        target = self.tasks_dir / "global.task.md"
        target.write_text("", encoding="utf-8")
        for args in (("g", "t", "-v"), ("task", "global", "-v")):
            with self.subTest(args=args):
                with mock.patch.object(dly, "view_file", return_value=0) as viewer:
                    code, _, _ = self._run(*args)
                self.assertEqual(code, 0)
                viewer.assert_called_once_with(target)

    def test_view_task_date_separate_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        target = self.tasks_dir / "20261105.task.md"
        target.write_text("", encoding="utf-8")
        with mock.patch.object(dly, "view_file", return_value=0) as viewer:
            code, _, _ = self._run("t", "-v", "2026-11-05")
        self.assertEqual(code, 0)
        viewer.assert_called_once_with(target)

    def test_view_today_missing_file(self):
        target = dly.normal_note_path(date.today())
        target.unlink(missing_ok=True)  # 确保目标不存在，验证“文件不存在”路径
        code, _, err = self._run("-v")
        self.assertEqual(code, 1)
        self.assertIn(f"文件不存在: {target}", err)

    def test_view_task_today_missing_file(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)
        self.tasks_dir.mkdir()
        code, _, err = self._run("t", "-v")
        self.assertEqual(code, 1)
        self.assertIn("文件不存在", err)

    def test_editor_exit_code_passthrough(self):
        with mock.patch.object(dly, "run_editor", return_value=7):
            code, _, _ = self._run("global")
        self.assertEqual(code, 7)

    def test_open_missing_notes_dir(self):
        missing = self.base / "missing-daily"
        self._patch_dir("NOTES_DIR", missing)
        for args in ([], ["task"], ["global"], ["g", "t"]):
            with self.subTest(args=args):
                code, _, err = self._run(*args)
                self.assertEqual(code, 1)
                self.assertIn(f"笔记目录不存在: {missing}", err)

    def test_open_missing_task_dir_separate(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建；notes_dir 存在
        with mock.patch.object(dly, "run_editor") as editor:
            code, _, err = self._run("task")
        self.assertEqual(code, 1)
        self.assertIn(f"任务目录不存在: {self.tasks_dir}", err)
        editor.assert_not_called()

    def test_open_global_task_missing_task_dir(self):
        self._patch_dir("TASK_DIR", self.tasks_dir)  # tasks_dir 未创建
        code, _, err = self._run("global", "task")
        self.assertEqual(code, 1)
        self.assertIn("任务目录不存在", err)


class CliErrorTests(NotesDirTestBase):
    """PRD 9.3：CLI 用法错误用例。"""

    def test_unknown_arg_n(self):
        code, _, err = self._run("n")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: n", err)
        self.assertIn("用法", err)

    def test_unknown_short_task_flag(self):
        code, _, err = self._run("-t")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: -t", err)

    def test_unknown_long_task_flag(self):
        code, _, err = self._run("--task")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: --task", err)

    def test_unknown_type_flag(self):
        code, _, err = self._run("--type", "task")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: --type", err)

    def test_global_with_extra_arg(self):
        code, _, err = self._run("global", "foo")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: foo", err)

    def test_duplicate_view_flag(self):
        code, _, err = self._run("-v", "-v")
        self.assertEqual(code, 1)
        self.assertIn("-v", err)

    def test_invalid_date_cli(self):
        code, _, err = self._run("20261301")
        self.assertEqual(code, 1)
        self.assertIn("无效日期: 20261301", err)
        self.assertNotIn("用法", err)  # 无效日期只输出错误信息，不带用法说明

    def test_invalid_date_in_task(self):
        code, _, err = self._run("task", "20261302")
        self.assertEqual(code, 1)
        self.assertIn("无效日期: 20261302", err)

    def test_date_then_extra_arg(self):
        code, _, err = self._run("2026-11-05", "extra")
        self.assertEqual(code, 1)
        self.assertIn("未知参数: extra", err)

    def test_usage_error_prints_usage(self):
        code, _, err = self._run("-v", "-v")
        self.assertEqual(code, 1)
        self.assertIn("用法：", err)
        self.assertIn("dly task|t -l|--list [pattern]", err)


if __name__ == "__main__":
    unittest.main()
