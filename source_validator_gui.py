#!/usr/bin/env python3
"""Windows desktop UI for the standalone Readori source validator.

The UI intentionally uses only Tkinter from the Python standard library. The
network/Legado implementation remains in validator/validate_source_packages.py
and is launched as a child process so that canceling a large batch can safely
terminate all worker threads.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


PROGRESS_RE = re.compile(r"Progress:\s*(\d+)/(\d+),\s*passed=(\d+)")
ROUND_RE = re.compile(r"=== .*?(\d+)/(\d+) 轮")
LOADED_RE = re.compile(r"Loaded\s+(\d+)\s+records.*?(\d+)\s+unique source URLs")
STAGE_RE = re.compile(r"stage=([\w-]+)")


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def child_command() -> list[str]:
    root = application_dir()
    if getattr(sys, "frozen", False):
        cli = root / "ReadoriSourceValidatorCLI.exe"
        if not cli.is_file():
            raise FileNotFoundError(
                "找不到 ReadoriSourceValidatorCLI.exe。请把 CLI 和 GUI 两个构建结果放在同一目录。"
            )
        return [str(cli)]
    return [sys.executable, "-u", str(root / "source_validator_cli.py")]


class ValidatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Readori 书源验证器")
        self.geometry("980x720")
        self.minsize(800, 560)
        self.configure(bg="#f3f5f7")

        # Keep file and directory selections independent. A shared variable
        # made the two rows mirror each other and caused the CLI to receive a
        # directory when the user had selected a single JSON file (and vice
        # versa).
        self.file_input_var = tk.StringVar()
        self.directory_input_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(application_dir() / "output"))
        self.workers_var = tk.IntVar(value=min(20, max(8, (os.cpu_count() or 4) * 2)))
        self.rounds_var = tk.IntVar(value=1)
        self.min_pass_var = tk.IntVar(value=1)
        self.quick_timeout_var = tk.IntVar(value=8)
        self.source_timeout_var = tk.IntVar(value=30)
        self.idle_timeout_var = tk.IntVar(value=180)
        self.limit_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="请选择 JSON 文件或目录，然后开始验证")
        self.detail_var = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0)
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.run_output_dir: Path | None = None
        self.last_return_code: int | None = None

        self._build_widgets()
        self.after(100, self._drain_output)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#56616b")
        style.configure("Primary.TButton", padding=(12, 7))

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Readori 书源验证器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="搜索/发现 → 详情 → 目录 → 正文；每个书源有独立硬超时，不会拖死整批任务。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(3, 14))

        paths = ttk.LabelFrame(outer, text="输入与输出", padding=12)
        paths.pack(fill="x")
        self._path_row(paths, 0, "书源 JSON 文件", self.file_input_var, self._choose_file, "选择文件")
        self._path_row(paths, 1, "书源 JSON 目录", self.directory_input_var, self._choose_directory, "选择目录")
        self._path_row(paths, 2, "输出目录", self.output_var, self._choose_output, "选择目录")
        ttk.Label(
            paths,
            text="文件和目录二选一；选择目录时会读取其中的 JSON/JSON5 书源包。",
            style="Hint.TLabel",
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(8, 0))

        options = ttk.LabelFrame(outer, text="验证参数", padding=12)
        options.pack(fill="x", pady=(14, 0))
        self._spin(options, 0, 0, "并发数", self.workers_var, 1, 64)
        self._spin(options, 0, 2, "稳定复测轮次", self.rounds_var, 1, 5)
        self._spin(options, 0, 4, "最低通过轮次", self.min_pass_var, 1, 5)
        self._spin(options, 1, 0, "快速扫描超时", self.quick_timeout_var, 0, 60)
        self._spin(options, 1, 2, "完整验证超时", self.source_timeout_var, 0, 600)
        self._spin(options, 1, 4, "无进度超时(秒)", self.idle_timeout_var, 0, 3600)
        self._spin(options, 2, 0, "仅验证前 N 个", self.limit_var, 0, 1_000_000)
        ttk.Label(
            options,
            text="流水线：先去重，再以 5-10 秒快速扫描筛选，最后对通过源做完整链路和稳定复测；0 表示不限制。",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(9, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(14, 8))
        self.start_button = ttk.Button(actions, text="开始验证", style="Primary.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消任务", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(actions, text="打开输出目录", command=self._open_output, state="disabled")
        self.open_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        progress = ttk.Frame(outer)
        progress.pack(fill="x")
        self.progress = ttk.Progressbar(progress, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", side="left", expand=True)
        ttk.Label(progress, textvariable=self.detail_var, width=30, anchor="e").pack(side="right", padx=(10, 0))

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_frame, wrap="word", height=18, state="disabled", background="#101418", foreground="#d9e2ea", insertbackground="#ffffff")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    @staticmethod
    def _path_row(parent: ttk.LabelFrame, row: int, label: str, variable: tk.StringVar, command, button: str) -> None:
        ttk.Label(parent, text=label, width=16).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(parent, text=button, command=command).grid(row=row, column=2, pady=4)
        parent.columnconfigure(1, weight=1)

    def _spin(self, parent: ttk.LabelFrame, row: int, column: int, label: str, variable: tk.IntVar, minimum: int, maximum: int) -> None:
        # 🐛 Bug 修复：Spinbox 默认允许用户直接键盘输入任意文本（不限于用上下
        # 箭头调整数值），如果输入框被清空或者输入了非数字内容，绑定的 IntVar
        # 在 .get() 时会抛出 TclError——之前这个异常在 _start() 里完全没有被
        # 捕获，打包成免控制台窗口的 exe 之后用户点"开始验证"会看起来毫无反应，
        # 不知道发生了什么。这里用 validate="key" 在按键输入这一步就直接挡掉
        # 非数字字符（允许输入空字符串，方便用户先删除旧值再输入新值），从源头
        # 减少发生这种情况的概率；_start() 里那层 try/except 仍然保留作为兜底，
        # 两层防护配合。
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 5), pady=4)
        vcmd = (self.register(self._validate_digits), "%P")
        ttk.Spinbox(
            parent,
            from_=minimum,
            to=maximum,
            textvariable=variable,
            width=9,
            validate="key",
            validatecommand=vcmd,
        ).grid(row=row, column=column + 1, sticky="w", padx=(0, 22), pady=4)

    @staticmethod
    def _validate_digits(proposed: str) -> bool:
        return proposed == "" or proposed.isdigit()

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(title="选择书源 JSON", filetypes=[("JSON files", "*.json *.json5"), ("All files", "*.*")])
        if path:
            self.file_input_var.set(path)
            self.directory_input_var.set("")
            self.status_var.set("已选择单个 JSON 文件")

    def _choose_directory(self) -> None:
        path = filedialog.askdirectory(title="选择书源 JSON 目录")
        if path:
            self.directory_input_var.set(path)
            self.file_input_var.set("")
            self.status_var.set("已选择 JSON 目录")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_var.set(path)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        file_value = self.file_input_var.get().strip()
        directory_value = self.directory_input_var.get().strip()
        # 🐛 Bug 修复：原来的写法是 `input_value = file_value or directory_value`
        # 决定实际用哪一个，但下面两个 if 分支各自独立地检查 `file_value`/
        # `directory_value` 是否非空，而不是检查"当前实际生效的是哪一个"。
        # 正常通过"选择文件"/"选择目录"按钮操作时，选中一个会自动清空另一个
        # （见 _choose_file / _choose_directory），不会触发这个问题；但如果用户
        # 直接在两个输入框里手动打字（不经过选择按钮），两个框就可能同时有内容——
        # 这时即使用户选的文件路径完全合法，也会因为"目录"那个框里还留着字符串、
        # 而 file_value 优先生效导致 input_path 实际指向一个文件，被"目录路径不是
        # 目录"这条完全不相关、容易让人摸不着头脑的错误提示挡住。改成先明确判断
        # 这次生效的是"文件"还是"目录"这两种模式之一，只校验被选中的那一种，
        # 不再让两个输入框的残留内容互相干扰。
        if file_value:
            mode = "file"
            input_value = file_value
        elif directory_value:
            mode = "directory"
            input_value = directory_value
        else:
            mode = None
            input_value = ""
        input_path = Path(input_value).expanduser() if input_value else None
        if mode is None or input_path is None or not input_path.exists():
            messagebox.showerror("缺少输入", "请选择存在的 JSON 文件或目录。")
            return
        if mode == "file" and not input_path.is_file():
            messagebox.showerror("输入无效", "书源 JSON 文件路径不是文件，请重新选择。")
            return
        if mode == "directory" and not input_path.is_dir():
            messagebox.showerror("输入无效", "书源 JSON 目录路径不是目录，请重新选择。")
            return
        # 🐛 Bug 修复：下面这些 Spinbox 绑定的都是 IntVar，如果用户直接在 Spinbox
        # 里手动打字输入了空值或者非数字内容（Spinbox 默认允许直接键入，不限制
        # 只能用上下箭头），IntVar.get() 会抛出 TclError。这个异常之前没有被捕获，
        # 在打包成免控制台窗口的 .exe 之后，用户点"开始验证"会看起来完全没反应
        # （异常信息只会打印到一个根本不存在的控制台，用户什么提示都看不到），
        # 非常令人困惑。这里补上明确的提示，能拿到具体是哪个参数有问题就说清楚，
        # 拿不到就退化成一个通用但至少可见的错误提示。
        try:
            workers = max(1, self.workers_var.get())
            rounds = max(1, self.rounds_var.get())
            min_pass_rounds = max(1, self.min_pass_var.get())
            idle_timeout = max(0, self.idle_timeout_var.get())
            quick_timeout = max(0, self.quick_timeout_var.get())
            source_timeout = max(0, self.source_timeout_var.get())
            limit = self.limit_var.get()
        except tk.TclError:
            messagebox.showerror(
                "参数无效",
                "「验证参数」区域里有一个数值框内容不是有效的整数（可能是空的或者输入了非数字字符），"
                "请检查后重试。",
            )
            return
        if min_pass_rounds > rounds:
            # 🐛 之前这个组合会被静默 clamp 到 rounds（CLI 内部逻辑会纠正，见
            # validate_source_packages.py 的 run_staged_pipeline），用户完全不知道
            # 自己设置的"最低通过轮次"实际上没有生效。这里改成明确提示后再继续，
            # 而不是悄悄改成别的值。
            if not messagebox.askyesno(
                "参数提示",
                f"「最低通过轮次」({min_pass_rounds}) 大于「稳定复测轮次」({rounds})，"
                f"实际执行时会自动按 {rounds} 轮处理。是否继续？",
            ):
                return

        output_root = Path(self.output_var.get().strip() or (application_dir() / "output")).expanduser()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_output_dir = output_root / f"run_{stamp}"
        self.run_output_dir.mkdir(parents=True, exist_ok=True)
        report = self.run_output_dir / "validation_report.json"
        validated = self.run_output_dir / "validated_sources.json"
        validated_full = self.run_output_dir / "validated_sources_full.json"
        command = child_command() + [
            "--input", str(input_path),
            "--report-path", str(report),
            "--validated-output", str(validated),
            "--validated-output-full", str(validated_full),
            "--workers", str(workers),
            "--rounds", str(rounds),
            "--min-pass-rounds", str(min_pass_rounds),
            "--idle-timeout", str(idle_timeout),
            "--quick-timeout", str(quick_timeout),
            "--source-timeout", str(source_timeout),
            "--no-mirror",
        ]
        if limit > 0:
            command.extend(["--limit", str(limit)])

        # The CLI is a console executable (and python.exe in source mode).
        # Keep it in its own process group for cancellation, but do not let
        # Windows create a visible console window behind the GUI.
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child_environment = os.environ.copy()
        child_environment["PYTHONUNBUFFERED"] = "1"
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(application_dir()),
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return

        self._clear_log()
        self._append_log("后台验证进程已启动。详细阶段进度会显示在下方日志中。\n\n")
        self.status_var.set("验证进行中")
        self.detail_var.set("")
        self.progress_var.set(0)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        threading.Thread(target=self._read_process, args=(self.process,), daemon=True).start()

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                self.output_queue.put(line)
        return_code = process.wait()
        self.output_queue.put(f"__PROCESS_EXIT__:{return_code}\n")

    def _drain_output(self) -> None:
        try:
            while True:
                line = self.output_queue.get_nowait()
                if line.startswith("__PROCESS_EXIT__:"):
                    self._finish(int(line.split(":", 1)[1]))
                    continue
                self._append_log(line)
                loaded_match = LOADED_RE.search(line)
                if loaded_match:
                    if str(self.progress.cget("mode")) == "indeterminate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=max(1, int(loaded_match.group(2))))
                    self.detail_var.set(f"已加载 {loaded_match.group(1)} 条，去重后 {loaded_match.group(2)} 个")
                match = PROGRESS_RE.search(line)
                if match:
                    if str(self.progress.cget("mode")) == "indeterminate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    done, total, passed = (int(item) for item in match.groups())
                    self.progress.configure(maximum=max(1, total))
                    self.progress_var.set(done)
                    self.detail_var.set(f"{done}/{total}，通过 {passed}")
                    stage_match = STAGE_RE.search(line)
                    if stage_match:
                        stage_labels = {
                            "quick-scan": "快速扫描",
                            "full-validation": "完整验证",
                        }
                        stage = stage_match.group(1)
                        if stage.startswith("stability-"):
                            stage = "稳定性复测 " + stage.split("-", 1)[1]
                        self.status_var.set(stage_labels.get(stage, stage))
                round_match = ROUND_RE.search(line)
                if round_match:
                    self.status_var.set(f"第 {round_match.group(1)}/{round_match.group(2)} 轮")
        except queue.Empty:
            pass
        self.after(100, self._drain_output)

    def _finish(self, return_code: int) -> None:
        self.last_return_code = return_code
        self.process = None
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.open_button.configure(state="normal" if self.run_output_dir and self.run_output_dir.exists() else "disabled")
        if return_code == 0:
            self.status_var.set("验证完成")
            self._append_log("\n验证完成，报告已写入输出目录。\n")
        else:
            self.status_var.set(f"验证结束（退出码 {return_code}）")
            self._append_log(f"\n验证未正常完成，退出码：{return_code}\n")

    @staticmethod
    def _terminate_process_tree(pid: int) -> None:
        """终止指定进程及其所有子进程。

        🐛 Bug 修复：验证流水线里，动态（JS 渲染）书源的校验会用
        `subprocess.run(["node", "-e", ...])` 启动一个 node.exe 子进程来跑源
        规则里的 JS 代码（见 validate_source_packages.py 的 run_node）。之前
        「取消任务」/关闭窗口时用的是 Popen.terminate()，在 Windows 上这只会
        杀掉直接子进程（CLI 本身），不会级联杀掉它再往下启动的孙进程。如果取消
        的时候正好有 worker 线程卡在这个 node 子进程调用里，这个 node.exe 就会
        变成孤儿进程，继续占用资源，直到自己的超时（默认几秒到几十秒）自然退出，
        或者被用户在任务管理器里手动结束——批量验证时并发 worker 数量可能有
        十几二十个，取消不及时清理的话最坏情况下会同时留下十几个孤儿 node.exe。
        改用 `taskkill /T` 连带整棵进程树一起结束，这是 Windows 上处理这类问题
        的标准做法。`/F` 强制结束，避免 node.exe 本身没有正确响应普通关闭请求
        导致 taskkill 也卡住。
        """
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            # 兜底：taskkill 本身不可用或者失败时，至少尝试结束这一个进程，
            # 好过完全不做任何事。
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    def _cancel(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if not messagebox.askyesno("取消验证", "确定要停止当前批量验证吗？已写入的日志会保留。"):
            return
        self.status_var.set("正在取消")
        self._terminate_process_tree(self.process.pid)

    def _open_output(self) -> None:
        if not self.run_output_dir or not self.run_output_dir.exists():
            return
        try:
            os.startfile(str(self.run_output_dir))
        except AttributeError:
            subprocess.Popen(["explorer", str(self.run_output_dir)])

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            if not messagebox.askyesno("退出", "验证仍在进行，确定停止并退出吗？"):
                return
            self._terminate_process_tree(self.process.pid)
        self.destroy()


if __name__ == "__main__":
    ValidatorApp().mainloop()
