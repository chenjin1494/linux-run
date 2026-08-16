#!/home/dctc1494/data/linux-run/venv/bin/python3
# -*- coding: utf-8 -*-
"""
运行对话框 (Run Dialog)

Linux 将根据你所输入的名称，为你打开相应的程序、文件夹、文档或 Internet 资源。

功能:
  - 输入命令后回车或点"确定"执行 (支持管道、参数等 shell 语法)
  - 输入文件/文件夹路径或 http(s):// 链接时用 xdg-open 打开
  - 勾选"使用 Root 权限"后用 pkexec 以管理员权限执行
  - "浏览(B)..." 选择要运行的程序
  - 记住历史输入 (QSettings, 最近 20 条, 支持补全)
  - Esc 关闭

实现说明:
  所有启动均使用 QProcess.startDetached() 分离启动 —— 被启动的程序
  不随对话框/程序退出而销毁，也不会因 QProcess 生命周期问题崩溃。

用法:
  ./run.py            (需要可执行权限)
  venv/bin/python run.py
"""

import os
import re
import shutil
import subprocess
import sys

from PyQt5 import QtCore, QtGui, QtWidgets, uic

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """数据文件路径：PyInstaller 打包后从 _MEIPASS 解压目录读取。"""
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, name)


UI_FILE = resource_path("run.ui")
ICON_FILE = resource_path("run.ico")

HISTORY_KEY = "history"
HISTORY_LIMIT = 20

# /bin/sh 内建命令（不在 PATH 中，但确实可执行）
_SHELL_BUILTINS = frozenset(
    "cd echo exit export pwd read set shift test true false wait eval exec "
    "umask type break continue return alias unalias hash trap times ulimit .".split()
)

# 终端模拟器候选（按优先级）
_TERMINALS = ["gnome-terminal", "x-terminal-emulator", "konsole", "xterm", "kgx", "mate-terminal", "tilix"]

# 查找 .desktop 文件的目录（用于判断 GUI 程序）
_DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    "/var/lib/snapd/desktop/applications",
]

# 明确的交互式终端程序（即使链接了图形库，也应在终端中打开）
_TERMINAL_PROGRAMS = frozenset(
    "vim vi nvim nano htop top btop tmux screen less more man "
    "python python3 ipython ipython3 node npm npx git ssh gitk".split()
)

# 常见命令别名兜底：若系统没有 python，则用 python3
_ALIASES = {"python": "python3"}


class RunDialog(QtWidgets.QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        uic.loadUi(UI_FILE, self)

        # ---- 图标 (iconLabel) ----
        self._load_icon()

        # ---- 历史记录 (QSettings) ----
        self._settings = QtCore.QSettings("RunDialog", "run")
        raw = self._settings.value(HISTORY_KEY, [])
        self._history = [str(x) for x in (raw if isinstance(raw, list) else [])]

        # ---- 输入框: 历史 + 补全 + 默认焦点 ----
        self.comboBox.addItems(self._history)
        if self._history:
            self.comboBox.setCurrentText(self._history[0])
        completer = QtWidgets.QCompleter(self._history, self.comboBox)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        self.comboBox.setCompleter(completer)

        # ---- 信号 ----
        self.pushButton.clicked.connect(self._on_ok)                 # 确定
        self.pushButton_2.clicked.connect(self.close)                # 取消
        self.pushButton_3.clicked.connect(self._on_browse)           # 浏览(B)...
        self.comboBox.lineEdit().returnPressed.connect(self._on_ok)  # 回车
        QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self, activated=self.close)

        # 窗口显示后定位到光标所在屏幕的左下角，并全选输入框内容
        QtCore.QTimer.singleShot(0, self._place_bottom_left)
        QtCore.QTimer.singleShot(0, self._focus_and_select_all)

    def _focus_and_select_all(self):
        """聚焦输入框并全选已有内容，直接输入即可覆盖上次的命令。"""
        self.comboBox.setFocus()
        self.comboBox.lineEdit().selectAll()

    # ------------------------------------------------------------- window
    def _place_bottom_left(self):
        """将窗口定位到光标所在屏幕的左下角（避开任务栏/面板/左侧 dock）。"""
        cursor_pos = QtGui.QCursor.pos()
        screen = QtWidgets.QApplication.screenAt(cursor_pos)
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        geo = screen.availableGeometry()  # 可用区域：已排除任务栏/面板
        margin = 8
        dock_offset = 76  # 左侧 dock 宽度 + 留白
        x = geo.left() + dock_offset
        y = geo.bottom() - self.frameGeometry().height() - margin
        self.move(x, y)

    # ------------------------------------------------------------------ UI
    def _load_icon(self):
        """加载 run.ico 到 iconLabel；缺失时回退到系统主题图标。"""
        if os.path.exists(ICON_FILE):
            pixmap = QtGui.QIcon(ICON_FILE).pixmap(32, 32)
        else:
            pixmap = QtGui.QIcon.fromTheme("system-run").pixmap(32, 32)
        self.iconLabel.setPixmap(pixmap)

    # ------------------------------------------------------------- actions
    def _on_ok(self):
        cmd = self.comboBox.currentText().strip()
        if not cmd:
            return
        if self._launch(cmd):
            self._save_history(cmd)
            self.close()

    def _on_browse(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择要运行的程序", os.path.expanduser("~"),
            "可执行文件 (*);;所有文件 (*)")
        if path:
            self.comboBox.setEditText(path)
            self.comboBox.lineEdit().setFocus()

    # ------------------------------------------------------------- running
    def _launch(self, cmd):
        """按语义启动: URL/路径 -> xdg-open; 否则执行命令; Root 用 pkexec 包装。"""
        use_root = self.checkBox.isChecked()

        # 1) URL 或已存在的文件/文件夹 -> xdg-open (不用 Root)
        if self._looks_like_url(cmd) or os.path.exists(os.path.expanduser(cmd)):
            return self._start("xdg-open", [cmd])

        # 2) 普通命令
        resolved = self._resolve_aliases(cmd)
        if not self._command_plausible(resolved):
            first = resolved.split()[0]
            QtWidgets.QMessageBox.warning(
                self, "运行", "找不到命令：%s\n请检查名称或路径是否正确。" % first)
            return False
        if use_root:
            if not shutil.which("pkexec"):
                QtWidgets.QMessageBox.warning(
                    self, "运行", "系统中未找到 pkexec，无法使用 Root 权限执行。")
                return False
            program, args = self._build_plain(resolved)
            return self._start("pkexec", self._pkexec_args(program, args))
        program, args = self._build_plain(resolved)
        return self._start(program, args)

    _PKEXEC_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")

    def _pkexec_args(self, program, args):
        """构造 pkexec 参数：仅透传图形会话变量（用户实测配方）。

        pkexec 会清空环境变量；只传 DISPLAY/XAUTHORITY 即可 ——
        DBUS_SESSION_BUS_ADDRESS 不传时，程序（gnome-terminal、VS Code
        等）会通过 libdbus 自动拉起自己的会话总线；传用户的 bus 反而
        会出问题（gnome-terminal 报 Factory 错误）。
        实测可用配方：
          pkexec env DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY <程序> <参数>
        """
        env_items = [f"{k}={v}" for k in self._PKEXEC_ENV if (v := os.environ.get(k))]
        return ["env"] + env_items + [program] + args

    def _build_plain(self, cmd):
        """决定普通命令如何执行，返回 (program, args)：

        - 整条命令含 shell 语法（管道/引号等）→ sh -c 分离执行（保持原行为）
        - 简单命令：
            * 程序是 GUI（有 .desktop 或链接图形库）→ 直接启动
            * 否则是终端程序（如 python/vim/htop）→ 在终端模拟器中打开
        """
        if any(ch in cmd for ch in "|&;<>()$`'\"\\*?[]{}~=!"):
            return "/bin/sh", ["-c", cmd]

        first = cmd.split()[0]
        exe = shutil.which(first)
        if exe is None and os.path.exists(first):
            exe = first
        if exe is not None and not self._is_gui_app(exe):
            term = self._find_terminal()
            if term:
                return term, self._terminal_args(term, cmd)
        return "/bin/sh", ["-c", cmd]

    # ---------------------------------------------------- terminal & gui
    @staticmethod
    def _find_terminal():
        for t in _TERMINALS:
            if shutil.which(t):
                return t
        return None

    @staticmethod
    def _terminal_args(term, cmd):
        """在终端中通过 sh -c 执行命令，兼容各种终端模拟器的参数风格。"""
        if term == "gnome-terminal" or term == "kgx":
            return ["--", "/bin/sh", "-c", cmd]
        return ["-e", "/bin/sh", "-c", cmd]

    _desktop_exec_cache = None

    @classmethod
    def _desktop_exec_names(cls):
        """所有 .desktop 文件中 Exec= 的程序名集合（带缓存）。"""
        if cls._desktop_exec_cache is None:
            names = set()
            for d in _DESKTOP_DIRS:
                if not os.path.isdir(d):
                    continue
                try:
                    for fn in os.listdir(d):
                        if not fn.endswith(".desktop"):
                            continue
                        path = os.path.join(d, fn)
                        try:
                            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                                for line in f:
                                    if line.startswith("Exec="):
                                        parts = line[5:].strip().split()
                                        if parts:
                                            names.add(os.path.basename(parts[0]))
                                        break
                        except OSError:
                            continue
                except OSError:
                    continue
            cls._desktop_exec_cache = names
        return cls._desktop_exec_cache

    @classmethod
    def _is_gui_app(cls, exe):
        """启发式判断是否为 GUI 程序（终端程序白名单优先）。"""
        name = os.path.basename(exe)
        if name in _TERMINAL_PROGRAMS:
            return False
        if name in cls._desktop_exec_names():
            return True
        try:
            out = subprocess.run(
                ["ldd", exe], capture_output=True, text=True, timeout=3).stdout
            return any(lib in out for lib in (
                "libgtk-3", "libgtk-4", "libQt5", "libQt6", "libX11", "libwayland"))
        except Exception:
            return False

    @staticmethod
    def _resolve_aliases(cmd):
        """常见别名兜底：没有 python 时用 python3。"""
        parts = cmd.split()
        if parts and parts[0] in _ALIASES and shutil.which(parts[0]) is None:
            return _ALIASES[parts[0]] + cmd[len(parts[0]):]
        return cmd

    def _start(self, program, args):
        """分离启动：程序独立于本对话框运行，互不销毁。"""
        if not shutil.which(program):
            QtWidgets.QMessageBox.warning(
                self, "运行", "无法启动程序：%s 不存在。" % program)
            return False
        try:
            ok = QtCore.QProcess.startDetached(program, args)
        except Exception as exc:  # 防御：任何异常都不应让程序崩溃
            QtWidgets.QMessageBox.warning(self, "运行", "无法启动程序：%s" % exc)
            return False
        if not ok:
            QtWidgets.QMessageBox.warning(self, "运行", "无法启动程序：%s" % program)
            return False
        return True

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _looks_like_url(s):
        return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s) is not None

    @classmethod
    def _command_plausible(cls, cmd):
        """粗略判断命令是否存在（保留"命令没找到"的友好提示）。

        遇到 shell 语法（管道/重定向/变量/引号等）或内建命令时不做判断，
        直接放行交给 sh 处理；只有简单程序名才检查 PATH。
        """
        parts = cmd.split()
        if not parts:
            return False
        first = parts[0]
        if any(ch in first for ch in "|&;<>()$`'\"\\*?[]{}~=!"):
            return True  # shell 构造，跳过校验
        if first in _SHELL_BUILTINS:
            return True
        if first.startswith("~"):
            return os.path.exists(os.path.expanduser(first))
        if "/" in first:
            return os.path.exists(first)
        return shutil.which(first) is not None

    def _save_history(self, cmd):
        hist = [cmd] + [x for x in self._history if x != cmd]
        self._history = hist[:HISTORY_LIMIT]
        self._settings.setValue(HISTORY_KEY, self._history)
        self.comboBox.clear()
        self.comboBox.addItems(self._history)
        self.comboBox.setCurrentText(cmd)


def _selftest():
    """打包自检（无界面）：验证 UI/图标加载与分离启动，成功退出码 0。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication([sys.argv[0]])
    win = RunDialog()
    win.show()
    app.processEvents()
    pm = win.iconLabel.pixmap()
    assert pm is not None and not pm.isNull(), "图标未加载"
    assert win.comboBox is not None, "UI 未加载"
    ok = QtCore.QProcess.startDetached("/bin/sh", ["-c", "true"])
    assert ok, "startDetached 失败"
    print("SELFTEST OK")
    return 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("运行")
    win = RunDialog()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
