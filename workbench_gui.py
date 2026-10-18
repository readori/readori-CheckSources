#!/usr/bin/env python3
"""Readori book-source workbench window (tkinter, standard library only).

Threading model
---------------
* One long-lived child process ``<child_command> rpc`` (the frozen ``ReadoriSourceValidatorCLI.exe``, or
  ``python -u source_validator_cli.py rpc`` during development) does all validator work.  This module never imports
  the validator: the UI stays responsive and a stuck source can be killed without taking the window down.
* The UI thread only writes request lines to the child's stdin.  Two daemon reader threads (stdout / stderr) put
  decoded lines on a ``queue.Queue``; ``WorkbenchWindow._drain`` empties it every 50 ms through ``self.after(...)``, so
  every widget is touched from the UI thread only.
* Stop = send ``cancel``; if no ``done`` frame arrives within 3 seconds the child is killed and restarted, the files
  are loaded again and the edited sources are restored from the working copies the window keeps.

Protocol: see ``validator/workbench_cli.py`` (``RpcServer``).  ``python workbench_gui.py --self-test`` builds the
control tree without opening a window and exits.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

STOP_GRACE_MS = 3000
DRAIN_MS = 50
DRAIN_BATCH = 300
RAW_VIEW_BYTES = 512 * 1024

APP_VERSION_LABELS = {"1.1.1（当前公开版）": "1.1.1", "1.1.2（下一版）": "1.1.2", "两版（仅校验）": "both"}
RPC_FIELD_KEYS = ("name", "verdict", "ms")

# (path, label, kind, hint, testable).  kind: line | text | bool | int.  Hints are the Legado editor texts.
FIELD_SPEC: dict[str, list[tuple[str, str, str, str, bool]]] = {
    "基本": [
        ("bookSourceType", "源类型", "int", "必填：0 文本 / 1 音频 / 2 图片 / 3 文件", False),
        ("bookSourceUrl", "源域名", "line", "通常填写网站主页，例：https://www.qidian.com（必填；它是书源在 App 里的主键）", False),
        ("bookSourceName", "源名称", "line", "会显示在源列表（必填）", False),
        ("bookSourceGroup", "源分组", "line", "描述源的特征信息", False),
        ("bookSourceComment", "源注释", "text", "描述源作者和状态", False),
        ("loginUrl", "登录地址", "line", "填写网站登录网址，仅在需要登录的源有用", False),
        ("loginUi", "登录界面", "text", "自定义登录界面", False),
        ("loginCheckJs", "登录检测", "text", "登录检测 js", False),
        ("coverDecodeJs", "封面解密", "text", "封面解密 js", False),
        ("bookUrlPattern", "链接验证", "line", "书籍 URL 正则，当详情页 URL 与源 URL 的域名不一致时有效，用于添加网址", False),
        ("header", "请求头", "text", "客户端标识；JSON 对象，key 区分大小写（User-Agent、Referer）", False),
        ("variableComment", "变量说明", "text", "书源变量说明", False),
        ("concurrentRate", "并发率", "line", "如 1000（访问间隔 1000ms）或者 1/1000（1000ms 内访问 1 次）", False),
        ("jsLib", "js 库", "text", "可填写 js 或者 key-value object 获取在线 js 文件", False),
        ("enabled", "启用搜索", "bool", "关闭后批量验证会直接判为失效", False),
        ("enabledExplore", "启用发现", "bool", "", False),
        ("enabledCookieJar", "Cookie 保持", "bool", "", False),
        ("weight", "搜索权重", "int", "", False),
        ("customOrder", "排序编号", "int", "", False),
    ],
    "搜索": [
        ("searchUrl", "搜索地址", "text", "[域名可省略]/search.php@kw={{key}}；可带选项：url,{\"charset\":\"gbk\"}", False),
        ("ruleSearch.checkKeyWord", "校验关键字", "line", "校验用关键字，强烈建议填写", False),
        ("ruleSearch.bookList", "书籍列表", "text", "选择书籍节点（规则结果为 List<Element>）", True),
        ("ruleSearch.name", "书名", "text", "选择节点（规则结果为 String）", True),
        ("ruleSearch.author", "作者", "text", "选择节点（规则结果为 String）", True),
        ("ruleSearch.kind", "分类", "text", "选择节点（规则结果为 String）", True),
        ("ruleSearch.wordCount", "字数", "text", "选择节点（规则结果为 String）", True),
        ("ruleSearch.lastChapter", "最新章节", "text", "选择节点（规则结果为 String）", True),
        ("ruleSearch.intro", "简介", "text", "选择节点（规则结果为 String）", True),
        ("ruleSearch.coverUrl", "封面链接", "text", "规则结果为 String 类型的 url", True),
        ("ruleSearch.bookUrl", "详情页链接", "text", "选择书籍详情页网址（规则结果为 String 类型的 url）", True),
    ],
    "发现": [
        ("exploreUrl", "发现地址", "text", "单个格式 <name>::<url> 或 {url:<url>,title:<name>,style:...}；前者用换行或 && 连接，后者放在数组内；可用 js 动态生成", False),
        ("ruleExplore.bookList", "书籍列表", "text", "选择书籍节点（规则结果为 List<Element>）；为空时沿用搜索规则", True),
        ("ruleExplore.name", "书名", "text", "选择节点（规则结果为 String）", True),
        ("ruleExplore.author", "作者", "text", "选择节点（规则结果为 String）", True),
        ("ruleExplore.kind", "分类", "text", "选择节点（规则结果为 String）", True),
        ("ruleExplore.wordCount", "字数", "text", "选择节点（规则结果为 String）", True),
        ("ruleExplore.lastChapter", "最新章节", "text", "选择节点（规则结果为 String）", True),
        ("ruleExplore.intro", "简介", "text", "选择节点（规则结果为 String）", True),
        ("ruleExplore.coverUrl", "封面链接", "text", "规则结果为 String 类型的 url", True),
        ("ruleExplore.bookUrl", "详情页链接", "text", "选择书籍详情页网址（规则结果为 String 类型的 url）", True),
    ],
    "详情": [
        ("ruleBookInfo.init", "预处理", "text", "用于加速详情信息检索，只支持 AllInOne 规则", True),
        ("ruleBookInfo.name", "书名", "text", "", True),
        ("ruleBookInfo.author", "作者", "text", "", True),
        ("ruleBookInfo.kind", "分类", "text", "", True),
        ("ruleBookInfo.wordCount", "字数", "text", "", True),
        ("ruleBookInfo.lastChapter", "最新章节", "text", "", True),
        ("ruleBookInfo.intro", "简介", "text", "", True),
        ("ruleBookInfo.coverUrl", "封面链接", "text", "", True),
        ("ruleBookInfo.tocUrl", "目录链接", "text", "目录地址（String url，与详情页相同时可省略）", True),
        ("ruleBookInfo.canReName", "允许改名", "text", "允许修改书名作者（默认不允许）", False),
        ("ruleBookInfo.downloadUrls", "下载地址", "text", "文件类书源下载地址（多个链接返回数组）", False),
    ],
    "目录": [
        ("ruleToc.preUpdateJs", "更新前 JS", "text", "更新目录前调用 JS，动态更新目录链接", False),
        ("ruleToc.chapterList", "目录列表", "text", "目录列表章节节点（List<Element>）", True),
        ("ruleToc.chapterName", "章节名称", "text", "", True),
        ("ruleToc.chapterUrl", "章节链接", "text", "String Url", True),
        ("ruleToc.formatJs", "格式化 JS", "text", "遍历去重后章节列表回调，提供 index（从 1 开始）、title、gInt（初始 0），返回值作为新标题", False),
        ("ruleToc.isVolume", "是否卷名", "text", "Bool", True),
        ("ruleToc.updateTime", "更新时间", "text", "章节信息（如更新时间）", True),
        ("ruleToc.isVip", "是否 VIP", "text", "Bool", True),
        ("ruleToc.isPay", "是否付费", "text", "Bool", True),
        ("ruleToc.nextTocUrl", "目录下一页", "text", "目录下一页链接（List<String> 或 String）", True),
    ],
    "正文": [
        ("ruleContent.content", "正文内容", "text", "String", True),
        ("ruleContent.title", "章节标题", "text", "结果覆盖章节标题", True),
        ("ruleContent.nextContentUrl", "正文下一页", "text", "下一分页（不是下一章）链接", True),
        ("ruleContent.webJs", "webJs", "text", "注入 javascript，用于模拟点击等，必须有返回值", True),
        ("ruleContent.sourceRegex", "资源正则", "text", "匹配资源 url 特征，用于嗅探", False),
        ("ruleContent.replaceRegex", "正文净化", "text", "多页内容合并后替换，用于正文净化（##正则##替换）", True),
        ("ruleContent.imageStyle", "图片样式", "line", "FULL：铺满；不填：默认", False),
        ("ruleContent.imageDecode", "图片解密", "text", "返回解密图片的 bytes", False),
        ("ruleContent.payAction", "购买操作", "text", "返回购买链接或调用购买接口", False),
    ],
}

HELP_TEXT = """规则语法小抄（来自 Legado ruleHelp）

规则类型识别
  @CSS:        JSoup CSS 选择器，如 @CSS:div.book > a@href
  @@ 或无前缀  JSoup 默认语法：class.list@tag.li、id.content@text、tag.a.0@href；索引 .0 / .-1，排除 !0，区间 [0,2] / [1:3]
  @XPath: 或以 / 开头   XPath：//div[@class="x"]/a/@href
  @Json: 或以 $. / $[ 开头   JSONPath：$.data.list[*]、$.name
  : 开头       AllInOne 正则（只能用在书籍列表、目录列表、详情 init）
  <js>…</js> 或 @js:   JS（result 为上一步结果，baseUrl、book、java.*、source 可用）
  取值尾缀：@text @ownText @textNodes @html @all @href @src @<属性>

组合与净化
  A&&B 合并    A||B 前者为空才用后者    A%%B 交错合并    列表规则前缀 - 倒序、+ 正序
  规则##正则##替换   全部替换；##正则##替换###   只取第一个匹配并替换；##正则   删除匹配
  正文 replaceRegex 在多页合并后对全文执行

变量
  @put:{key:"规则"} 存、@get:{key} 取；{{…}} 内嵌规则或 JS；{{key}} {{page}} {{(page-1)*20}}

URL 选项
  url,{"method":"POST","body":"k={{key}}","charset":"gbk","headers":{"User-Agent":"…","Referer":"…"},"retry":2,"webView":true,"js":"…"}
  分页 <首页,{{page}}>；header 的 key 区分大小写（User-Agent、Referer）；charset:"escape" 不编码
  发现地址：名称::url 用换行或 && 分隔，或 JSON 数组 [{"title":"","url":"","style":{…}}]，或 <js> 动态生成
  并发率：1000（间隔 1 秒）或 20/60000（60 秒内 20 次）

调试 key
  关键字 | 名称::发现url | 详情url | ++目录url | --正文url | 留空 = 完整校验（与批量验证同一路径）

正则：^$()[]{}.?+*| 需要转义；(?s) 跨行、(?m) 逐行、(?i) 忽略大小写
"""

RULE_TEST_FIELDS = [
    path for specs in FIELD_SPEC.values() for (path, _label, _kind, _hint, testable) in specs if testable
]


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def nested_get(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def short(text: Any, limit: int = 80) -> str:
    value = " ".join(str(text if text is not None else "").split())
    return value if len(value) <= limit else value[:limit] + "…"


# ---------------------------------------------------------------------------
# RPC client (UI thread writes, reader threads only enqueue)
# ---------------------------------------------------------------------------

class RpcClient:
    def __init__(self, child_command: Callable[[], list[str]]) -> None:
        self.child_command = child_command
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[tuple[int, str, Any]] = queue.Queue()
        self.generation = 0
        self._next_id = 1
        self.handlers: dict[int, dict[str, Callable[..., None] | None]] = {}

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        command = list(self.child_command()) + ["rpc"]
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        self.generation += 1
        self.handlers = {}
        process = subprocess.Popen(
            command,
            cwd=str(application_dir()),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self.process = process
        generation = self.generation
        threading.Thread(target=self._read_stdout, args=(process, generation), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process, generation), daemon=True).start()

    def _read_stdout(self, process: subprocess.Popen[str], generation: int) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.messages.put((generation, "msg", json.loads(line)))
                except ValueError:
                    self.messages.put((generation, "stderr", line))
        self.messages.put((generation, "exit", process.wait()))

    def _read_stderr(self, process: subprocess.Popen[str], generation: int) -> None:
        if process.stderr is not None:
            for line in process.stderr:
                self.messages.put((generation, "stderr", line.rstrip("\r\n")))

    def send(self, method: str, params: dict[str, Any] | None = None, *,
             on_reply: Callable[[dict[str, Any]], None] | None = None,
             on_event: Callable[[dict[str, Any]], None] | None = None,
             on_done: Callable[[dict[str, Any]], None] | None = None) -> int:
        request_id = self._next_id
        self._next_id += 1
        self.handlers[request_id] = {"reply": on_reply, "event": on_event, "done": on_done}
        line = json.dumps({"id": request_id, "method": method, "params": params or {}}, ensure_ascii=False)
        try:
            if not self.alive or self.process is None or self.process.stdin is None:
                raise OSError("后台进程没有运行")
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            self.messages.put((self.generation, "msg", {"id": request_id, "error": f"后台进程不可用：{exc}"}))
        return request_id

    def kill(self) -> None:
        process, self.process = self.process, None
        self.generation += 1  # drop whatever the old process still queued
        self.handlers = {}
        if process is None:
            return
        try:
            process.kill()
        except OSError:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class _ScrollFrame(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class WorkbenchWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc | None = None, *, child_command: Callable[[], list[str]],
                 input_path: str = "", app_version: str = "1.1.1", timeouts: tuple[int, int] = (8, 30),
                 start_rpc: bool = True) -> None:
        super().__init__(master)
        self.title("Readori 书源工作台")
        self.geometry("1320x800")
        self.minsize(1100, 700)
        self.client = RpcClient(child_command)
        self.quick_timeout = max(1, int(timeouts[0] or 8))
        self.source_timeout = max(1, int(timeouts[1] or 30))
        self.rows: dict[str, dict[str, Any]] = {}
        self.row_verdict: dict[str, tuple[str, str]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.loaded_paths: list[str] = []
        self.current_key = ""
        self.working_view: dict[str, Any] = {}
        self.job: dict[str, Any] | None = None
        self.last_debug: dict[str, Any] = {}
        self.suggestions: dict[str, dict[str, Any]] = {}
        self.raw_rows: list[dict[str, Any]] = []
        self.pause_choices: list[dict[str, Any]] = []
        self._loading = False
        self.fields: dict[str, dict[str, Any]] = {}
        self.status_var = tk.StringVar(value="正在启动后台进程…")
        self.filter_var = tk.StringVar()
        self.key_var = tk.StringVar()
        version_label = next((label for label, value in APP_VERSION_LABELS.items() if value == app_version),
                             next(iter(APP_VERSION_LABELS)))
        self.version_var = tk.StringVar(value=version_label)
        self.keep_url_var = tk.BooleanVar(value=False)
        self.rt_field_var = tk.StringVar()
        self.rt_item_var = tk.IntVar(value=0)
        self.rt_online_var = tk.BooleanVar(value=False)
        self.rt_raw_var = tk.StringVar(value="自动")
        self.diff_mode_var = tk.StringVar(value="table")
        self._build()
        self.filter_var.trace_add("write", lambda *_a: self._refill_sources())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._drain_id = self.after(DRAIN_MS, self._drain)
        if start_rpc:
            self._start_rpc(input_path)

    # -- construction --------------------------------------------------
    def _build(self) -> None:
        self._build_topbar()
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        left = ttk.Frame(panes, width=250)
        middle = ttk.Frame(panes, width=520)
        right = ttk.Frame(panes, width=520)
        panes.add(left, weight=1)
        panes.add(middle, weight=3)
        panes.add(right, weight=3)
        self._build_sources(left)
        self._build_editor(middle)
        self._build_results(right)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="right")

    def _build_topbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="调试 key").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.key_var, width=34)
        entry.pack(side="left", padx=(6, 6))
        entry.bind("<Return>", lambda _e: self._debug(False))
        chips = ttk.Frame(bar)
        chips.pack(side="left")
        for label, command in (("校验关键字", self._chip_keyword), ("系统", self._chip_system),
                               ("详情", self._chip_info), ("++目录", self._chip_toc), ("--正文", self._chip_content)):
            ttk.Button(chips, text=label, width=8 if len(label) < 5 else 9, command=command).pack(side="left", padx=1)
        self.explore_button = ttk.Menubutton(chips, text="发现▾", width=7)
        self.explore_menu = tk.Menu(self.explore_button, tearoff=False, postcommand=self._fill_explore_menu)
        self.explore_button.configure(menu=self.explore_menu)
        self.explore_button.pack(side="left", padx=1)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.debug_button = ttk.Button(bar, text="▶ 调试", command=lambda: self._debug(False))
        self.debug_button.pack(side="left", padx=1)
        self.step_button = ttk.Button(bar, text="⏭ 单步", command=lambda: self._debug(True))
        self.step_button.pack(side="left", padx=1)
        self.stop_button = ttk.Button(bar, text="■ 停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=1)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.validate_button = ttk.Button(bar, text="校验此源", command=self._validate)
        self.validate_button.pack(side="left", padx=1)
        self.compare_button = ttk.Button(bar, text="修复前后对比", command=self._compare)
        self.compare_button.pack(side="left", padx=1)
        ttk.Combobox(bar, textvariable=self.version_var, values=list(APP_VERSION_LABELS), state="readonly",
                     width=16).pack(side="right")
        ttk.Label(bar, text="App 版本").pack(side="right", padx=(0, 6))

    def _build_sources(self, parent: ttk.Frame) -> None:
        ttk.Entry(parent, textvariable=self.filter_var).pack(fill="x", padx=2, pady=(0, 4))
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.source_tree = ttk.Treeview(frame, columns=RPC_FIELD_KEYS, show="headings", selectmode="browse")
        for name, title, width in (("name", "名称", 140), ("verdict", "上次结论", 80), ("ms", "耗时", 56)):
            self.source_tree.heading(name, text=title)
            self.source_tree.column(name, width=width, anchor="w" if name == "name" else "center")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.source_tree.yview)
        self.source_tree.configure(yscrollcommand=bar.set)
        self.source_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.source_tree.bind("<<TreeviewSelect>>", self._on_select_source)
        buttons = ttk.Frame(parent)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="导入…", command=self._import).pack(side="left")
        ttk.Button(buttons, text="导出已修改…", command=self._export).pack(side="right")

    def _build_editor(self, parent: ttk.Frame) -> None:
        tools = ttk.Frame(parent)
        tools.pack(fill="x", pady=(0, 4))
        self.undo_button = ttk.Button(tools, text="撤销", command=lambda: self._edit("undo"), state="disabled")
        self.undo_button.pack(side="left")
        self.redo_button = ttk.Button(tools, text="重做", command=lambda: self._edit("redo"), state="disabled")
        self.redo_button.pack(side="left", padx=4)
        self.reset_button = ttk.Button(tools, text="还原", command=self._reset, state="disabled")
        self.reset_button.pack(side="left")
        self.changed_label = ttk.Label(tools, text="")
        self.changed_label.pack(side="right")
        self.editor_tabs = ttk.Notebook(parent)
        self.editor_tabs.pack(fill="both", expand=True)
        for tab, specs in FIELD_SPEC.items():
            scroll = _ScrollFrame(self.editor_tabs)
            self.editor_tabs.add(scroll, text=tab)
            scroll.inner.columnconfigure(1, weight=1)
            for row, spec in enumerate(specs):
                self._build_field(scroll.inner, row, spec)
        json_page = ttk.Frame(self.editor_tabs)
        self.editor_tabs.add(json_page, text="JSON")
        buttons = ttk.Frame(json_page)
        buttons.pack(fill="x", pady=4)
        ttk.Button(buttons, text="应用 JSON", command=self._apply_json).pack(side="left", padx=(4, 0))
        ttk.Button(buttons, text="格式化", command=self._format_json).pack(side="left", padx=4)
        text_frame = ttk.Frame(json_page)
        text_frame.pack(fill="both", expand=True)
        self.json_text = tk.Text(text_frame, wrap="none", undo=True, font=("Consolas", 10))
        ybar = ttk.Scrollbar(text_frame, orient="vertical", command=self.json_text.yview)
        xbar = ttk.Scrollbar(json_page, orient="horizontal", command=self.json_text.xview)
        self.json_text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.json_text.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        xbar.pack(fill="x")

    def _build_field(self, parent: ttk.Frame, row: int, spec: tuple[str, str, str, str, bool]) -> None:
        path, label, kind, hint, testable = spec
        ttk.Label(parent, text=label, width=12, anchor="w").grid(row=row * 2, column=0, sticky="nw", padx=(6, 4), pady=(7, 0))
        var: Any
        if kind == "text":
            widget: Any = tk.Text(parent, height=3, wrap="word", undo=True, font=("Consolas", 10))
            var = None
            widget.bind("<FocusOut>", lambda _e, p=path: self._commit_field(p))
        elif kind == "bool":
            var = tk.BooleanVar(value=False)
            widget = ttk.Checkbutton(parent, variable=var, command=lambda p=path: self._commit_field(p))
        else:
            var = tk.StringVar()
            widget = ttk.Entry(parent, textvariable=var)
            widget.bind("<FocusOut>", lambda _e, p=path: self._commit_field(p))
            widget.bind("<Return>", lambda _e, p=path: self._commit_field(p))
        widget.grid(row=row * 2, column=1, sticky="ew" if kind != "bool" else "w", pady=(7, 0))
        if testable:
            ttk.Button(parent, text="▶", width=3, command=lambda p=path: self._test_field(p)).grid(
                row=row * 2, column=2, padx=(4, 6), pady=(7, 0), sticky="n")
        if hint:
            ttk.Label(parent, text=hint, foreground="#7a8590", wraplength=470, justify="left").grid(
                row=row * 2 + 1, column=1, sticky="w")
        self.fields[path] = {"kind": kind, "var": var, "widget": widget, "label": label}

    def _build_results(self, parent: ttk.Frame) -> None:
        self.result_tabs = ttk.Notebook(parent)
        self.result_tabs.pack(fill="both", expand=True)
        self._build_log_tab()
        self._build_raw_tab()
        self._build_rule_tab()
        self._build_suggest_tab()
        self._build_diff_tab()
        self._build_compare_tab()
        help_page = ttk.Frame(self.result_tabs)
        self.result_tabs.add(help_page, text="帮助")
        help_text = tk.Text(help_page, wrap="word", font=("Segoe UI", 10))
        help_text.insert("1.0", HELP_TEXT)
        help_text.configure(state="disabled")
        help_text.pack(fill="both", expand=True)

    @staticmethod
    def _text_with_scroll(parent: tk.Misc, **options: Any) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, **options)
        bar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=bar.set)
        text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return text

    def _build_log_tab(self) -> None:
        page = ttk.Frame(self.result_tabs)
        self.result_tabs.add(page, text="日志")
        self.pause_frame = ttk.LabelFrame(page, text="单步：请选择要继续的项", padding=6)
        self.pause_list = tk.Listbox(self.pause_frame, height=6, exportselection=False)
        self.pause_list.pack(side="left", fill="both", expand=True)
        self.pause_list.bind("<Double-Button-1>", lambda _e: self._resume())
        ttk.Button(self.pause_frame, text="继续", command=self._resume).pack(side="left", padx=6, anchor="n")
        self.log_text = self._text_with_scroll(page, wrap="word", state="disabled", background="#101418",
                                               foreground="#d9e2ea", font=("Consolas", 10))
        for tag, color in (("err", "#ff7b72"), ("ok", "#7ee787"), ("warn", "#e3b341"), ("pause", "#79c0ff"),
                           ("extra", "#8b98a5")):
            self.log_text.tag_configure(tag, foreground=color)
        buttons = ttk.Frame(page)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="清空", command=self._clear_log).pack(side="left")
        ttk.Button(buttons, text="复制全部", command=self._copy_log).pack(side="left", padx=4)
        ttk.Button(buttons, text="保存日志…", command=self._save_log).pack(side="left")

    def _build_raw_tab(self) -> None:
        page = ttk.Frame(self.result_tabs)
        self.result_tabs.add(page, text="原始响应")
        split = ttk.PanedWindow(page, orient="vertical")
        split.pack(fill="both", expand=True)
        top = ttk.Frame(split)
        split.add(top, weight=1)
        columns = ("id", "stage", "status", "size", "ms", "url")
        self.raw_tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="browse", height=6)
        for name, title, width in (("id", "#", 34), ("stage", "阶段", 56), ("status", "状态", 50), ("size", "大小", 64),
                                   ("ms", "耗时", 56), ("url", "地址", 300)):
            self.raw_tree.heading(name, text=title)
            self.raw_tree.column(name, width=width, anchor="w")
        self.raw_tree.pack(fill="both", expand=True)
        self.raw_tree.bind("<<TreeviewSelect>>", self._on_select_raw)
        bottom = ttk.Frame(split)
        split.add(bottom, weight=3)
        self.raw_text = self._text_with_scroll(bottom, wrap="none", state="disabled", font=("Consolas", 10))

    def _build_rule_tab(self) -> None:
        page = ttk.Frame(self.result_tabs, padding=6)
        self.result_tabs.add(page, text="规则测试")
        page.columnconfigure(1, weight=1)
        ttk.Label(page, text="字段").grid(row=0, column=0, sticky="w")
        self.rt_field_box = ttk.Combobox(page, textvariable=self.rt_field_var, values=RULE_TEST_FIELDS, width=30)
        self.rt_field_box.grid(row=0, column=1, sticky="ew", padx=4)
        self.rt_field_box.bind("<<ComboboxSelected>>", lambda _e: self._rule_load_current())
        ttk.Label(page, text="规则").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.rt_rule = tk.Text(page, height=4, wrap="word", font=("Consolas", 10))
        self.rt_rule.grid(row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        options = ttk.Frame(page)
        options.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(options, text="第").pack(side="left")
        ttk.Spinbox(options, from_=0, to=999, textvariable=self.rt_item_var, width=5).pack(side="left", padx=2)
        ttk.Label(options, text="项").pack(side="left")
        ttk.Label(options, text="  响应").pack(side="left")
        self.rt_raw_box = ttk.Combobox(options, textvariable=self.rt_raw_var, values=["自动"], width=8, state="readonly")
        self.rt_raw_box.pack(side="left", padx=4)
        ttk.Checkbutton(options, text="允许规则内 JS 联网", variable=self.rt_online_var).pack(side="left", padx=6)
        ttk.Button(options, text="测试", command=self._run_rule_test).pack(side="right")
        ttk.Button(options, text="用此规则替换书源", command=self._rule_apply).pack(side="right", padx=4)
        self.rt_status = ttk.Label(page, text="", wraplength=470, justify="left")
        self.rt_status.grid(row=3, column=0, columnspan=2, sticky="w")
        self.rt_list = tk.Listbox(page, font=("Consolas", 10), exportselection=False)
        self.rt_list.grid(row=4, column=0, columnspan=2, sticky="nsew")
        page.rowconfigure(4, weight=1)

    def _build_suggest_tab(self) -> None:
        page = ttk.Frame(self.result_tabs, padding=4)
        self.result_tabs.add(page, text="建议")
        tools = ttk.Frame(page)
        tools.pack(fill="x")
        ttk.Button(tools, text="重新生成建议", command=self._suggest).pack(side="left")
        ttk.Checkbutton(tools, text="域名迁移时保持书源地址不变", variable=self.keep_url_var).pack(side="left", padx=8)
        columns = ("code", "title", "conf", "preview", "apply")
        self.suggest_tree = ttk.Treeview(page, columns=columns, show="headings", selectmode="browse", height=7)
        for name, title, width in (("code", "代码", 44), ("title", "建议", 230), ("conf", "把握", 50),
                                   ("preview", "预览", 90), ("apply", "可应用", 56)):
            self.suggest_tree.heading(name, text=title)
            self.suggest_tree.column(name, width=width, anchor="w")
        self.suggest_tree.pack(fill="x", pady=4)
        self.suggest_tree.bind("<<TreeviewSelect>>", self._on_select_suggestion)
        buttons = ttk.Frame(page)
        buttons.pack(fill="x")
        self.preview_button = ttk.Button(buttons, text="离线预览", command=lambda: self._preview(False), state="disabled")
        self.preview_button.pack(side="left")
        self.online_preview_button = ttk.Button(buttons, text="在线预览（会请求站点）", command=lambda: self._preview(True),
                                                state="disabled")
        self.online_preview_button.pack(side="left", padx=4)
        self.apply_button = ttk.Button(buttons, text="应用", command=self._apply_suggestion, state="disabled")
        self.apply_button.pack(side="right")
        self.suggest_text = self._text_with_scroll(page, wrap="word", state="disabled", font=("Segoe UI", 10), height=10)

    def _build_diff_tab(self) -> None:
        page = ttk.Frame(self.result_tabs, padding=4)
        self.result_tabs.add(page, text="差异")
        tools = ttk.Frame(page)
        tools.pack(fill="x")
        ttk.Radiobutton(tools, text="结构化", value="table", variable=self.diff_mode_var,
                        command=self._show_diff_mode).pack(side="left")
        ttk.Radiobutton(tools, text="统一 diff", value="unified", variable=self.diff_mode_var,
                        command=self._show_diff_mode).pack(side="left", padx=8)
        self.diff_holder = ttk.Frame(page)
        self.diff_holder.pack(fill="both", expand=True, pady=(4, 0))
        self.diff_table_frame = ttk.Frame(self.diff_holder)
        self.diff_tree = ttk.Treeview(self.diff_table_frame, columns=("path", "old", "new"), show="headings")
        for name, title, width in (("path", "字段", 130), ("old", "修改前", 180), ("new", "修改后", 180)):
            self.diff_tree.heading(name, text=title)
            self.diff_tree.column(name, width=width, anchor="w")
        self.diff_tree.pack(fill="both", expand=True)
        self.diff_unified_frame = ttk.Frame(self.diff_holder)
        self.diff_text = self._text_with_scroll(self.diff_unified_frame, wrap="none", state="disabled",
                                                font=("Consolas", 10))
        self.diff_table_frame.pack(fill="both", expand=True)

    def _build_compare_tab(self) -> None:
        page = ttk.Frame(self.result_tabs, padding=4)
        self.result_tabs.add(page, text="对比")
        self.compare_summary = ttk.Label(page, text="点击「修复前后对比」：原始书源与当前书源按批量流水线各验证一次。",
                                         wraplength=470, justify="left")
        self.compare_summary.pack(fill="x", pady=(0, 4))
        self.compare_tree = ttk.Treeview(page, columns=("stage", "before", "after", "change"), show="headings", height=8)
        for name, title, width in (("stage", "阶段", 110), ("before", "修复前", 90), ("after", "修复后", 90), ("change", "变化", 90)):
            self.compare_tree.heading(name, text=title)
            self.compare_tree.column(name, width=width, anchor="center")
        self.compare_tree.pack(fill="x")

    # -- rpc plumbing ----------------------------------------------------
    def _start_rpc(self, input_path: str) -> None:
        try:
            self.client.start()
        except (OSError, FileNotFoundError) as exc:
            self.status_var.set(f"后台进程启动失败：{exc}")
            messagebox.showerror("启动失败", str(exc), parent=self)
            return
        self.status_var.set("后台进程已启动，请导入书源")
        if input_path and Path(input_path).exists():
            self._load_path(input_path, append=False)

    def _drain(self) -> None:
        handled = 0
        try:
            while handled < DRAIN_BATCH:
                generation, kind, payload = self.client.messages.get_nowait()
                handled += 1
                if generation != self.client.generation:
                    continue
                if kind == "msg":
                    self._dispatch(payload)
                elif kind == "stderr":
                    self._on_stderr(str(payload))
                elif kind == "exit":
                    self._on_exit(payload)
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self._drain_id = self.after(DRAIN_MS, self._drain)
        except tk.TclError:
            pass

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        handler = self.client.handlers.get(request_id) if isinstance(request_id, int) else None
        if "event" in message:
            callback = handler.get("event") if handler else None
            if callback:
                callback(message["event"])
        elif "done" in message:
            callback = handler.get("done") if handler else None
            self.client.handlers.pop(request_id, None)
            if callback:
                callback(message["done"])
        elif "result" in message or "error" in message:
            callback = handler.get("reply") if handler else None
            if handler and not (handler.get("done") or handler.get("event")) or "error" in message:
                self.client.handlers.pop(request_id, None)
            if callback:
                callback(message)

    def _on_stderr(self, line: str) -> None:
        if line.strip():
            self._log_line(line, "extra")

    def _on_exit(self, code: Any) -> None:
        if self.job is not None:
            self._finish_job()
        self.status_var.set(f"后台进程已退出（{code}）。点「导入…」会重新启动。")

    def _ensure_rpc(self) -> bool:
        if self.client.alive:
            return True
        try:
            self.client.start()
        except (OSError, FileNotFoundError) as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)
            return False
        self._resync()
        return True

    def _resync(self) -> None:
        """After a restart: load the files again and restore the edited sources."""
        for index, path in enumerate(self.loaded_paths):
            self.client.send("load", {"path": path, "append": index > 0})
        for key, state in self.states.items():
            if state.get("changed"):
                self.client.send("edit", {"sourceKey": key, "op": "restore", "working": state.get("working")},
                                 on_reply=self._on_edit_reply)

    # -- sources -----------------------------------------------------------
    def _import(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="导入书源 JSON",
                                          filetypes=[("书源 JSON", "*.json *.json5"), ("所有文件", "*.*")])
        if path and self._ensure_rpc():
            self._load_path(path, append=bool(self.rows))

    def _load_path(self, path: str, *, append: bool) -> None:
        self.status_var.set(f"正在读取 {path} …")

        def on_reply(message: dict[str, Any]) -> None:
            if "error" in message:
                self.status_var.set(f"导入失败：{message['error']}")
                messagebox.showerror("导入失败", str(message["error"]), parent=self)
                return
            result = message["result"]
            if not append:
                self.rows, self.states, self.row_verdict = {}, {}, {}
                self.loaded_paths = []
                self.current_key = ""
            if path not in self.loaded_paths:
                self.loaded_paths.append(path)
            for row in result["sources"]:
                self.rows[row["sourceKey"]] = row
            self._refill_sources()
            self.status_var.set(f"已导入 {result['added']} 条书源（共 {result['count']} 条）")
            if not self.current_key and self.rows:
                first = next(iter(self.rows))
                self.source_tree.selection_set(first)

        self.client.send("load", {"path": path, "append": append}, on_reply=on_reply)

    def _refill_sources(self) -> None:
        needle = self.filter_var.get().strip().lower()
        selected = self.current_key
        self.source_tree.delete(*self.source_tree.get_children())
        for key, row in self.rows.items():
            if needle and needle not in row["name"].lower() and needle not in row["url"].lower():
                continue
            self.source_tree.insert("", "end", iid=key, values=self._row_values(key))
        if selected and self.source_tree.exists(selected):
            self.source_tree.selection_set(selected)

    def _row_values(self, key: str) -> tuple[str, str, str]:
        row = self.rows[key]
        verdict, ms = self.row_verdict.get(key, ("", ""))
        changed = "（已改）" if self.states.get(key, {}).get("changed") else ""
        return (row["name"] + changed, verdict, ms)

    def _update_row(self, key: str) -> None:
        if self.source_tree.exists(key):
            self.source_tree.item(key, values=self._row_values(key))

    def _on_select_source(self, _event: Any = None) -> None:
        selection = self.source_tree.selection()
        if not selection or selection[0] == self.current_key:
            return
        if self.job is not None:
            self.status_var.set("正在运行，先停止或等待结束再切换书源")
            if self.current_key and self.source_tree.exists(self.current_key):
                self.source_tree.selection_set(self.current_key)
            return
        self.current_key = selection[0]
        self.last_debug = {}
        self._reset_results()
        self.client.send("edit", {"sourceKey": self.current_key, "op": "get"}, on_reply=self._on_edit_reply)

    def _reset_results(self) -> None:
        self._clear_log()
        self.raw_tree.delete(*self.raw_tree.get_children())
        self.raw_rows = []
        self.rt_raw_box.configure(values=["自动"])
        self.rt_raw_var.set("自动")
        self._set_text(self.raw_text, "")
        self.suggestions = {}
        self.suggest_tree.delete(*self.suggest_tree.get_children())
        self._set_text(self.suggest_text, "")
        self._update_suggest_buttons()
        self.compare_tree.delete(*self.compare_tree.get_children())
        self.rt_list.delete(0, "end")
        self.rt_status.configure(text="")
        self._hide_pause()

    # -- editing ---------------------------------------------------------------
    def _on_edit_reply(self, message: dict[str, Any]) -> None:
        if "error" in message:
            self.status_var.set(f"编辑失败：{message['error']}")
            self._refresh_fields()
            return
        state = message["result"]
        self.states[state["sourceKey"]] = state
        self._update_row(state["sourceKey"])
        if state["sourceKey"] == self.current_key:
            self._apply_state(state)

    def _apply_state(self, state: dict[str, Any]) -> None:
        self.working_view = state.get("view") or {}
        self._refresh_fields()
        self._loading = True
        try:
            self.json_text.delete("1.0", "end")
            self.json_text.insert("1.0", json.dumps(state.get("working"), ensure_ascii=False, indent=2))
        finally:
            self._loading = False
        changed = bool(state.get("changed"))
        self.undo_button.configure(state="normal" if state.get("canUndo") else "disabled")
        self.redo_button.configure(state="normal" if state.get("canRedo") else "disabled")
        self.reset_button.configure(state="normal" if changed else "disabled")
        self.changed_label.configure(text=f"已修改 {len(state.get('diff') or [])} 处" if changed else "未修改")
        self._fill_diff(state)
        row = self.rows.get(state["sourceKey"], {})
        self.title(f"Readori 书源工作台 - {row.get('name', '')}{' *' if changed else ''}")

    def _refresh_fields(self) -> None:
        self._loading = True
        try:
            for path, field in self.fields.items():
                value = nested_get(self.working_view, path)
                kind = field["kind"]
                if kind == "bool":
                    field["var"].set(bool(value) if value is not None else False)
                elif kind == "text":
                    widget = field["widget"]
                    widget.delete("1.0", "end")
                    widget.insert("1.0", display_value(value))
                else:
                    field["var"].set(display_value(value))
        finally:
            self._loading = False

    def _field_text(self, path: str) -> Any:
        field = self.fields[path]
        if field["kind"] == "bool":
            return bool(field["var"].get())
        if field["kind"] == "text":
            return field["widget"].get("1.0", "end-1c")
        return field["var"].get()

    def _commit_field(self, path: str) -> None:
        if self._loading or not self.current_key:
            return
        field = self.fields[path]
        old = nested_get(self.working_view, path)
        new = self._field_text(path)
        kind = field["kind"]
        value: Any = new
        if kind == "bool":
            if bool(old) == new:
                return
        elif kind == "int":
            text = str(new).strip()
            if text == display_value(old).strip() or (not text and old is None):
                return
            try:
                value = int(text)
            except ValueError:
                self.status_var.set(f"{field['label']} 需要整数")
                self._refresh_fields()
                return
        else:
            if new == display_value(old) or (old is None and new == ""):
                return
            if isinstance(old, (dict, list)):
                try:
                    value = json.loads(new)
                except ValueError:
                    self.status_var.set(f"{field['label']} 原来是 JSON 结构，请填写合法 JSON")
                    self._refresh_fields()
                    return
        self.client.send("edit", {"sourceKey": self.current_key, "op": "set", "path": path, "value": value,
                                  "reason": f"手动编辑：{field['label']}"}, on_reply=self._on_edit_reply)

    def _edit(self, op: str) -> None:
        if self.current_key:
            self.client.send("edit", {"sourceKey": self.current_key, "op": op}, on_reply=self._on_edit_reply)

    def _reset(self) -> None:
        if self.current_key and messagebox.askyesno("还原", "放弃这条书源的全部修改，回到导入时的样子？", parent=self):
            self._edit("reset")

    def _format_json(self) -> None:
        try:
            parsed = json.loads(self.json_text.get("1.0", "end"))
        except ValueError as exc:
            messagebox.showerror("JSON 格式错误", str(exc), parent=self)
            return
        self.json_text.delete("1.0", "end")
        self.json_text.insert("1.0", json.dumps(parsed, ensure_ascii=False, indent=2))

    def _apply_json(self) -> None:
        if not self.current_key:
            return
        text = self.json_text.get("1.0", "end")
        try:
            json.loads(text)
        except ValueError as exc:
            messagebox.showerror("JSON 格式错误", str(exc), parent=self)
            return
        self.client.send("edit", {"sourceKey": self.current_key, "op": "replace", "json": text}, on_reply=self._on_edit_reply)

    # -- diff ----------------------------------------------------------------------
    def _fill_diff(self, state: dict[str, Any]) -> None:
        self.diff_tree.delete(*self.diff_tree.get_children())
        for index, entry in enumerate(state.get("diff") or []):
            if "note" in entry:
                self.diff_tree.insert("", "end", iid=f"d{index}", values=(entry["path"], entry["note"], ""))
            else:
                self.diff_tree.insert("", "end", iid=f"d{index}", values=(
                    entry["path"], short(display_value(entry.get("old")), 120), short(display_value(entry.get("new")), 120)))
        self._set_text(self.diff_text, state.get("unified") or "（没有修改）")

    def _show_diff_mode(self) -> None:
        if self.diff_mode_var.get() == "table":
            self.diff_unified_frame.pack_forget()
            self.diff_table_frame.pack(fill="both", expand=True)
        else:
            self.diff_table_frame.pack_forget()
            self.diff_unified_frame.pack(fill="both", expand=True)

    # -- log -----------------------------------------------------------------------
    def _set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _log_line(self, text: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self._set_text(self.log_text, "")

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log_text.get("1.0", "end-1c"))
        self.status_var.set("日志已复制到剪贴板")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(parent=self, title="保存日志", defaultextension=".txt",
                                            filetypes=[("文本", "*.txt")])
        if path:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8", newline="\n")

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        text = event.get("text") or ""
        if text:
            tag = ""
            if event.get("state") == -1:
                tag = "err"
            elif event.get("state") == 1000:
                tag = "ok"
            elif kind == "warn":
                tag = "warn"
            elif kind == "pause":
                tag = "pause"
            elif event.get("extra"):
                tag = "extra"
            self._log_line(text, tag)
        if kind == "fetch" and event.get("raw_id") is not None:
            self._add_raw_row(event)
        elif kind == "pause":
            self._show_pause(event.get("data") or {})
        elif kind == "begin" and event.get("stage"):
            self.status_var.set(f"运行中：{event.get('stage')}")

    # -- raw responses ----------------------------------------------------------
    def _add_raw_row(self, event: dict[str, Any]) -> None:
        raw_id = int(event["raw_id"])
        iid = f"r{raw_id}"
        if self.raw_tree.exists(iid):
            return
        data = event.get("data") or {}
        size = data.get("size")
        self.raw_tree.insert("", "end", iid=iid, values=(
            raw_id, event.get("stage") or "", event.get("status") or "", f"{size}B" if size is not None else "",
            event.get("elapsed_ms") or "", event.get("url") or ""))
        ids = ["自动"] + [str(int(i[1:])) for i in self.raw_tree.get_children()]
        self.rt_raw_box.configure(values=ids)

    def _on_select_raw(self, _event: Any = None) -> None:
        selection = self.raw_tree.selection()
        if not selection or not self.current_key:
            return
        raw_id = int(selection[0][1:])

        def on_reply(message: dict[str, Any]) -> None:
            if "error" in message:
                self._set_text(self.raw_text, f"读取失败：{message['error']}")
                return
            entry = message["result"]
            lines = [f"{entry.get('method')} {entry.get('url')}",
                     f"HTTP {entry.get('status')} · {entry.get('charset') or '?'} · {entry.get('elapsed_ms')}ms · {entry.get('size')}B"
                     + ("（已截断）" if entry.get("truncated") else "")]
            if entry.get("final_url") and entry.get("final_url") != entry.get("url"):
                lines.append(f"最终地址：{entry['final_url']}")
            for status, url in entry.get("history") or []:
                lines.append(f"跳转：{status} → {url}")
            lines.append("--- 请求头 ---")
            lines += [f"{k}: {v}" for k, v in (entry.get("request_headers") or {}).items()]
            if entry.get("request_body"):
                lines += ["--- 请求体 ---", str(entry["request_body"])]
            lines.append("--- 响应头 ---")
            lines += [f"{k}: {v}" for k, v in (entry.get("response_headers") or {}).items()]
            lines += ["--- 响应正文 ---", entry.get("body") or ""]
            self._set_text(self.raw_text, "\n".join(lines))

        self.client.send("raw_get", {"sourceKey": self.current_key, "rawId": raw_id, "maxBytes": RAW_VIEW_BYTES},
                         on_reply=on_reply)

    # -- single step pause ---------------------------------------------------------
    def _show_pause(self, data: dict[str, Any]) -> None:
        self.pause_choices = list(data.get("choices") or [])
        self.pause_list.delete(0, "end")
        for choice in self.pause_choices:
            self.pause_list.insert("end", f"[{choice.get('index')}] {choice.get('name') or ''}  {choice.get('url') or ''}")
        if self.pause_choices:
            self.pause_list.selection_set(0)
        label = "书籍" if data.get("what") == "book" else "章节"
        self.pause_frame.configure(text=f"单步：请选择要继续调试的{label}（共 {data.get('total', len(self.pause_choices))} 项）")
        self.pause_frame.pack(fill="x", before=self.log_text.master)
        self.result_tabs.select(0)
        self.status_var.set(f"等待选择{label}…")

    def _hide_pause(self) -> None:
        self.pause_frame.pack_forget()

    def _resume(self) -> None:
        selection = self.pause_list.curselection()
        index = 0
        if selection and self.pause_choices:
            index = int(self.pause_choices[selection[0]].get("index", 0))
        self._hide_pause()
        self.client.send("resume", {"choice": index})
        self.status_var.set("运行中…")

    # -- running jobs --------------------------------------------------------------
    def _version(self, *, allow_both: bool = False) -> str:
        value = APP_VERSION_LABELS.get(self.version_var.get(), "1.1.1")
        return value if (value != "both" or allow_both) else "1.1.1"

    def _begin_job(self, kind: str, method: str, params: dict[str, Any], *, clear_log: bool = False) -> None:
        if not self.current_key:
            self.status_var.set("先在左侧选择一条书源")
            return
        if self.job is not None:
            self.status_var.set("已有任务在运行")
            return
        if not self._ensure_rpc():
            return
        self.job = {"kind": kind, "id": None}
        self._set_job_buttons(True)
        if clear_log:
            self._clear_log()
            self.raw_tree.delete(*self.raw_tree.get_children())
            self.rt_raw_box.configure(values=["自动"])
            self.rt_raw_var.set("自动")
        self.progress.start(12)
        self.status_var.set("运行中…")
        params = dict(params, sourceKey=self.current_key)
        request_id = self.client.send(method, params, on_reply=self._on_job_reply, on_event=self._on_event,
                                      on_done=lambda done, k=kind: self._on_done(k, done))
        self.job["id"] = request_id

    def _on_job_reply(self, message: dict[str, Any]) -> None:
        if "error" in message:
            self.status_var.set(f"无法开始：{message['error']}")
            self._finish_job()

    def _set_job_buttons(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for button in (self.debug_button, self.step_button, self.validate_button, self.compare_button):
            button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")

    def _finish_job(self) -> None:
        self.job = None
        self.progress.stop()
        self._set_job_buttons(False)
        self._hide_pause()

    def _debug(self, step: bool) -> None:
        self._begin_job("debug", "debug", {
            "key": self.key_var.get().strip(), "step": step, "appVersion": self._version(),
            "timeout": self.source_timeout, "pickBook": 0, "pickChapter": 0}, clear_log=True)

    def _validate(self) -> None:
        self._begin_job("validate", "validate", {
            "appVersion": self._version(allow_both=True), "quickTimeout": self.quick_timeout,
            "sourceTimeout": self.source_timeout, "rounds": 1, "trace": True}, clear_log=True)

    def _compare(self) -> None:
        if self.version_var.get() and APP_VERSION_LABELS.get(self.version_var.get()) == "both":
            self.status_var.set("对比按 1.1.1 进行（两版只用于「校验此源」）")
        self._begin_job("compare", "compare", {
            "appVersion": self._version(), "quickTimeout": self.quick_timeout, "sourceTimeout": self.source_timeout,
            "rounds": 1})
        self._log_line("▶ 修复前后对比：先验证原始书源，再验证当前书源…", "pause")

    def _stop(self) -> None:
        job = self.job
        if job is None:
            return
        self.client.send("cancel", {})
        self.status_var.set("正在停止…")
        self.after(STOP_GRACE_MS, lambda request_id=job.get("id"): self._stop_watchdog(request_id))

    def _stop_watchdog(self, request_id: Any) -> None:
        if self.job is not None and self.job.get("id") == request_id:
            self._restart_rpc("后台没有响应停止请求，已强制结束并重启")

    def _restart_rpc(self, message: str) -> None:
        self.client.kill()
        self._finish_job()
        try:
            self.client.start()
        except (OSError, FileNotFoundError) as exc:
            self.status_var.set(f"重启后台失败：{exc}")
            return
        self._resync()
        self._log_line(f"■ {message}", "err")
        self.status_var.set(message)

    def _on_done(self, kind: str, done: dict[str, Any]) -> None:
        self._finish_job()
        status = done.get("status")
        message = done.get("message") or ""
        self.status_var.set(message or ("已停止" if status == "cancelled" else "完成"))
        key = self.current_key
        if kind == "debug":
            self.last_debug = done
            verdict = done.get("verdict")
            if verdict:
                self.row_verdict[key] = (verdict.get("verdictZh") or "", str(verdict.get("responseTimeMs") or ""))
                self._update_row(key)
            for entry in done.get("raw") or []:
                self._add_raw_row({"raw_id": entry["raw_id"], "stage": entry["stage"], "status": entry["status"],
                                   "elapsed_ms": entry["elapsed_ms"], "url": entry["url"], "data": {"size": entry["size"]}})
            if status != "cancelled" and done.get("eventCount"):
                self._suggest()
        elif kind == "validate":
            results = done.get("results") or {}
            if len(results) == 1:
                result = next(iter(results.values()))
                self.row_verdict[key] = (result.get("verdictZh") or "", str(result.get("responseTimeMs") or ""))
            elif results:
                self.row_verdict[key] = (done.get("compatibility") or "", "")
            self._update_row(key)
        elif kind == "compare":
            self._fill_compare(done)
            self.result_tabs.select(5)  # 对比
            after = done.get("after") or {}
            self.row_verdict[key] = (after.get("verdictZh") or "", str(after.get("responseTimeMs") or ""))
            self._update_row(key)
        elif kind == "preview":
            self._on_preview_done(done)

    def _fill_compare(self, done: dict[str, Any]) -> None:
        self.compare_tree.delete(*self.compare_tree.get_children())
        for row in done.get("rows") or []:
            def mark(flag: bool) -> str:
                return "✓" if flag else "✗"
            change = {"improved": "✓ 修好了", "regressed": "✗ 变差了"}.get(row.get("change"), "—")
            self.compare_tree.insert("", "end", values=(row["label"], mark(row["before"]), mark(row["after"]), change))
        times = done.get("elapsedMs") or {}
        self.compare_summary.configure(
            text=f"{done.get('message') or ''}\n耗时：{times.get('before', '?')} ms → {times.get('after', '?')} ms")

    # -- chips ------------------------------------------------------------------------
    def _chip_keyword(self) -> None:
        self.key_var.set(display_value(nested_get(self.working_view, "ruleSearch.checkKeyWord")) or "我的")

    def _chip_system(self) -> None:
        self.key_var.set("")

    def _chip_info(self) -> None:
        url = self.last_debug.get("bookUrl")
        if url:
            self.key_var.set(url)
        else:
            self.status_var.set("先用关键词调试一次，才能知道详情页地址")

    def _chip_toc(self) -> None:
        url = self.last_debug.get("tocUrl") or self.last_debug.get("bookUrl")
        if url:
            self.key_var.set("++" + url)
        else:
            self.status_var.set("先用关键词调试一次，才能知道目录地址")

    def _chip_content(self) -> None:
        url = self.last_debug.get("chapterUrl")
        if url:
            self.key_var.set("--" + url)
        else:
            self.status_var.set("先用关键词调试一次，才能知道正文地址")

    def _fill_explore_menu(self) -> None:
        self.explore_menu.delete(0, "end")
        raw = display_value(nested_get(self.working_view, "exploreUrl"))
        entries: list[str] = []
        for line in raw.replace("&&", "\n").splitlines():
            line = line.strip()
            if "::" in line:
                entries.append(line)
        if not entries:
            self.explore_menu.add_command(label="（书源没有静态发现地址）", state="disabled")
        for entry in entries[:30]:
            self.explore_menu.add_command(label=short(entry, 60), command=lambda e=entry: self.key_var.set(e))

    # -- rule test ---------------------------------------------------------------------
    def _test_field(self, path: str) -> None:
        self.rt_field_var.set(path)
        self.rt_item_var.set(0)
        self._rule_load_current()
        self.result_tabs.select(2)
        self._run_rule_test()

    def _rule_load_current(self) -> None:
        path = self.rt_field_var.get().strip()
        self.rt_rule.delete("1.0", "end")
        if path in self.fields:
            self.rt_rule.insert("1.0", self._field_text(path) if self.fields[path]["kind"] != "bool" else "")
        else:
            self.rt_rule.insert("1.0", display_value(nested_get(self.working_view, path)))

    def _run_rule_test(self) -> None:
        if not self.current_key:
            self.status_var.set("先在左侧选择一条书源")
            return
        field = self.rt_field_var.get().strip()
        if not field:
            self.status_var.set("请选择要测试的字段")
            return
        raw = self.rt_raw_var.get()
        params: dict[str, Any] = {
            "sourceKey": self.current_key, "field": field, "rule": self.rt_rule.get("1.0", "end-1c").strip() or None,
            "itemIndex": int(self.rt_item_var.get() or 0), "appVersion": self._version(), "online": bool(self.rt_online_var.get()),
        }
        if raw.isdigit():
            params["rawId"] = int(raw)
        self.rt_status.configure(text="测试中…")
        self.rt_list.delete(0, "end")

        def on_reply(message: dict[str, Any]) -> None:
            if "error" in message:
                self.rt_status.configure(text=str(message["error"]))
                return
            result = message["result"]
            parts = [f"{'✓' if result.get('ok') else '✗'} {result.get('field')} · {result.get('kind')} · {result.get('elapsed_ms')} ms"]
            if result.get("items_count") is not None:
                parts.append(f"列表 {result['items_count']} 项")
            if result.get("error"):
                parts.append(str(result["error"]))
            parts += [f"⚠ {w}" for w in result.get("warnings") or []]
            self.rt_status.configure(text="\n".join(parts))
            for position, value in enumerate(result.get("values") or [], 1):
                self.rt_list.insert("end", f"[{position}] {short(value, 400)}")
            if not result.get("values"):
                self.rt_list.insert("end", "（没有结果）")

        self.client.send("test_rule", params, on_reply=on_reply)

    def _rule_apply(self) -> None:
        path = self.rt_field_var.get().strip()
        if not self.current_key or path not in self.fields:
            self.status_var.set("该字段不能写回书源")
            return
        rule = self.rt_rule.get("1.0", "end-1c")
        self.client.send("edit", {"sourceKey": self.current_key, "op": "set", "path": path, "value": rule,
                                  "reason": f"规则测试：{self.fields[path]['label']}"}, on_reply=self._on_edit_reply)

    # -- suggestions ---------------------------------------------------------------------
    def _suggest(self) -> None:
        if not self.current_key:
            return

        def on_reply(message: dict[str, Any]) -> None:
            self.suggestions = {}
            self.suggest_tree.delete(*self.suggest_tree.get_children())
            if "error" in message:
                self._set_text(self.suggest_text, str(message["error"]))
                self._update_suggest_buttons()
                return
            items = message["result"].get("suggestions") or []
            for item in items:
                self.suggestions[item["id"]] = item
                self.suggest_tree.insert("", "end", iid=item["id"], values=self._suggest_values(item))
            self._set_text(self.suggest_text, "" if items else "这次调试没有发现可以自动修复的问题。")
            self._update_suggest_buttons()
            if items:
                self.status_var.set(f"{self.status_var.get()} · 建议 {len(items)} 条（见「建议」页）")

        self.client.send("suggest", {"sourceKey": self.current_key, "appVersion": self._version(),
                                     "keepSourceUrl": bool(self.keep_url_var.get())}, on_reply=on_reply)

    @staticmethod
    def _suggest_values(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
        kind = item.get("preview_kind")
        preview = item.get("_preview")
        if not item.get("patch") or kind == "none":
            state = "仅提示"
        elif preview is None:
            state = "离线" if kind == "offline" else "需联网"
        else:
            state = "预览 ✓" if preview.get("ok") else "预览 ✗"
        return (item["code"], item["title"], item.get("confidence", ""), state, "是" if item.get("apply_allowed") else "否")

    def _selected_suggestion(self) -> dict[str, Any] | None:
        selection = self.suggest_tree.selection()
        return self.suggestions.get(selection[0]) if selection else None

    def _on_select_suggestion(self, _event: Any = None) -> None:
        item = self._selected_suggestion()
        self._update_suggest_buttons()
        if item is None:
            return
        lines = [f"{item['code']}  {item['title']}  （把握：{item.get('confidence')}）", "", item.get("reason") or ""]
        if item.get("patch"):
            lines += ["", "将修改："]
            for op in item["patch"]:
                lines.append(f"  {op.get('path')}")
                lines.append(f"    旧：{short(display_value(op.get('old')), 160)}")
                lines.append(f"    新：{short(display_value(op.get('new')), 160)}")
        preview = item.get("_preview")
        if preview:
            lines += ["", f"预览：{preview.get('summary') or ''}"]
        self._set_text(self.suggest_text, "\n".join(lines))

    def _update_suggest_buttons(self) -> None:
        item = self._selected_suggestion()
        has_patch = bool(item and item.get("patch") and item.get("preview_kind") != "none")
        self.preview_button.configure(state="normal" if has_patch and item.get("preview_kind") == "offline" else "disabled")
        self.online_preview_button.configure(
            state="normal" if has_patch and item.get("preview_kind") == "online" and self.job is None else "disabled")
        self.apply_button.configure(state="normal" if has_patch and item.get("apply_allowed") else "disabled")

    def _preview(self, online: bool) -> None:
        item = self._selected_suggestion()
        if item is None:
            return
        params = {"sourceKey": self.current_key, "suggestionId": item["id"], "online": online,
                  "appVersion": self._version()}
        if online:
            self._begin_job_for_preview(item, params)
            return

        def on_reply(message: dict[str, Any]) -> None:
            if "error" in message:
                self.status_var.set(f"预览失败：{message['error']}")
                return
            self._store_preview(item["id"], message["result"])

        self.client.send("preview", params, on_reply=on_reply)

    def _begin_job_for_preview(self, item: dict[str, Any], params: dict[str, Any]) -> None:
        if self.job is not None:
            self.status_var.set("已有任务在运行")
            return
        self.job = {"kind": "preview", "id": None}
        self._set_job_buttons(True)
        self.progress.start(12)
        self.status_var.set("在线预览中（会重新请求站点）…")
        request_id = self.client.send("preview", params, on_reply=self._on_job_reply,
                                      on_done=lambda done, sid=item["id"]: self._on_preview_job_done(sid, done))
        self.job["id"] = request_id

    def _on_preview_job_done(self, suggestion_id: str, done: dict[str, Any]) -> None:
        self._finish_job()
        outcome = done.get("preview")
        if outcome:
            self._store_preview(suggestion_id, outcome)
        self.status_var.set(done.get("message") or "在线预览结束")

    def _on_preview_done(self, done: dict[str, Any]) -> None:  # kept for the generic "preview" kind
        self.status_var.set(done.get("message") or "预览结束")

    def _store_preview(self, suggestion_id: str, outcome: dict[str, Any]) -> None:
        item = self.suggestions.get(suggestion_id)
        if item is None:
            return
        item["_preview"] = outcome
        if outcome.get("apply_allowed") is not None:
            item["apply_allowed"] = bool(outcome.get("apply_allowed"))
        if self.suggest_tree.exists(suggestion_id):
            self.suggest_tree.item(suggestion_id, values=self._suggest_values(item))
        self._on_select_suggestion()

    def _apply_suggestion(self) -> None:
        item = self._selected_suggestion()
        if item is None:
            return

        def on_reply(message: dict[str, Any]) -> None:
            if "error" in message:
                messagebox.showerror("应用失败", str(message["error"]), parent=self)
                return
            self._on_edit_reply(message)
            self.status_var.set(f"已应用 {item['code']}：{item['title']}（可用「撤销」恢复；建议基于修改前的书源，修改后请重新调试）")

        self.client.send("apply_suggestion", {"sourceKey": self.current_key, "suggestionId": item["id"]}, on_reply=on_reply)

    # -- export ----------------------------------------------------------------------------
    def _export(self) -> None:
        if not self.rows:
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出已修改的书源", defaultextension=".json",
                                            initialfile="fixed.json", filetypes=[("书源 JSON", "*.json")])
        if not path:
            return
        report = str(Path(path).with_name("fix-report.json"))

        def on_reply(message: dict[str, Any]) -> None:
            if "error" in message:
                messagebox.showerror("导出失败", str(message["error"]), parent=self)
                return
            result = message["result"]
            if not result.get("count"):
                messagebox.showinfo("没有可导出的书源", "还没有修改过的书源。先编辑或应用建议。", parent=self)
                return
            messagebox.showinfo("已导出", f"已导出 {result['count']} 条书源：\n{path}\n修复报告：\n{report}", parent=self)

        self.client.send("export", {"onlyChanged": True, "path": path, "reportPath": report, "stamp": True,
                                    "keepSourceUrl": bool(self.keep_url_var.get()), "appVersion": self._version()},
                         on_reply=on_reply)

    # -- lifecycle ---------------------------------------------------------------------------
    def _close(self) -> None:
        try:
            self.after_cancel(self._drain_id)
        except (tk.TclError, ValueError):
            pass
        self.client.kill()
        self.destroy()


def self_test() -> int:
    """Build the control tree without a visible window or a child process, then quit (exit code 0 = OK)."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"WORKBENCH_SELF_TEST_SKIPPED no display: {exc}")
        return 0
    root.withdraw()
    try:
        window = WorkbenchWindow(root, child_command=lambda: [sys.executable], start_rpc=False)
        window.update_idletasks()
        count = _count_widgets(window)
        missing = [path for specs in FIELD_SPEC.values() for (path, *_rest) in specs if path not in window.fields]
        window.destroy()
    finally:
        root.destroy()
    if missing:
        print(f"WORKBENCH_SELF_TEST_FAILED missing fields: {missing}")
        return 1
    print(f"WORKBENCH_SELF_TEST_OK widgets={count} fields={sum(len(v) for v in FIELD_SPEC.values())}")
    return 0


def _count_widgets(widget: tk.Misc) -> int:
    return 1 + sum(_count_widgets(child) for child in widget.winfo_children())


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return self_test()
    # Standalone start (development): borrow the batch GUI's child_command() so both windows launch the same CLI.
    from source_validator_gui import child_command

    root = tk.Tk()
    root.withdraw()
    window = WorkbenchWindow(root, child_command=child_command, input_path=args[0] if args else "")
    window.protocol("WM_DELETE_WINDOW", lambda: (window._close(), root.destroy()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
