"""Readori App 版本档案（R143）。

验证器要回答的问题是「这条书源在某个 App 版本里能不能用」。App 1.1.1（当前公开版）与 1.1.2（下一版）
在四个地方行为不同，这些差异集中在本模块，由 `contextvars` 保存当前档案：

* A1 `@js:` searchUrl 的字面量捷径（1.1.1 会请求第一个 http(s) 字面量并丢弃脚本）；
* A2 String 字段为 JSON 数组 / 对象（1.1.1 整个字段丢弃，1.1.2 存成紧凑 JSON 文本）；
* A3 CryptoJS（1.1.1 无条件安装一个会遮挡书源自带库的假对象，1.1.2 按需安装 crypto-js 4.1 兼容实现）；
* A4 批量验证里被拦下的登录 / 验证交互（1.1.1 记失效，1.1.2 记「需要登录/验证」）；
* A5 列表规则尾部 `@put:{…}`（R155 C：1.1.1 把整条规则当一条普通规则，得到一个条目；1.1.2 先对整份内容执行 put，
  再把去掉 put 的基础规则当列表求值，对应 Legado AnalyzeRule.getElements 的 putRule）；
* A6 以单个 `.` 开头、不以 `$` / `@` 开头的 JSON 路径（R155 N：1.1.1 去掉前导点在根上求值；1.1.2 与 Jayway 一致，
  补成 `$..`，即深度扫描）。

本模块只有纯函数和常量，不联网、不读文件；`cryptography` 只在原生桥里按需导入。
两段 CryptoJS 脚本由 R143 的提取脚本从 App 源码原样生成，不能手改：
  * APP_111_CRYPTOJS_POLYFILL  取自 `git show 2a2239e:Readori/Core/Engine/JSEngine.swift` 的 `cryptoJSPolyfill`
    （去掉 Swift 多行字符串的 4 格缩进）；
  * APP_112_CRYPTOJS_BUILTIN   取自 `Readori/Core/Engine/JSEngineCryptoJS.swift` 的 `builtinSource`。
"""
from __future__ import annotations

import base64
import contextvars
import copy
import hashlib
import hmac
import json
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

SUPPORTED_APP_VERSIONS: tuple[str, ...] = ("1.1.1", "1.1.2")
DEFAULT_APP_VERSION = "1.1.1"  # 用户决定：当前公开版为默认；1.1.2 正式上架当天再改

VALIDATION_TIMEZONE_KEY = "America/Los_Angeles"

# R143 S3：App 的 searchValidationTier / healthStatus 只认含「完整验证通过」的标签。
VALIDATED_PASS_TAG = "✅ 完整验证通过（书籍+详情+目录+正文）"
# R143 S2：1.1.2 专属通过源在 MMDD 后追加的分组（不含「失效」，不影响健康判定）。
APP_112_ONLY_GROUP_TOKEN = "需1.1.2"

APP_IMPORT_REJECTED_REASON = "app import rejects source"
INTERACTIVE_VERIFICATION_REASON = "interactive verification required"

GSON_MAX_DEPTH = 64  # BookSource.swift LegadoGsonJSONText.maxDepth


@dataclass(frozen=True)
class AppProfile:
    version: str
    search_url_js_literal_shortcut: bool  # A1
    string_container_mode: str  # A2: "drop" | "gson-text"
    cryptojs_mode: str  # A3: "legacy-global" | "on-demand-builtin"
    interaction_verdict: str  # A4: "invalid" | "needsInteraction"
    list_put_on_whole_content: bool = False  # A5 (R155 C): list rule `base@put:{…}` runs the put on the whole content
    json_leading_dot_deep_scan: bool = False  # A6 (R155 N): `.key[*]` is `$..key[*]` (Jayway), not a root-level key
    # A7 (R155b J): an error-shaped JSON detail body fails the detail stage only when the rules / fallbacks extracted
    # neither a book name nor a tocUrl distinct from the book URL (Legado BookInfo never rejects a body); 1.1.1 fails it
    # before any field rule runs.
    detail_error_json_requires_empty_info: bool = False
    # A8 (R155b): the toc JSON fallback for `isVolume` no longer reads the key `volume` (a volume NUMBER in many APIs).
    toc_volume_key_excluded: bool = False
    # A9 (R156 E1, mirrors the Swift engine package): rule-engine parity gaps closed in 1.1.2 only.
    # A: the `.txt` bookUrl single-chapter shortcut applies only when the toc chapterList rule is empty
    #    (BookSourceEngine.swift getChapterList; Legado BookChapterList always evaluates ruleToc).
    txt_shortcut_only_without_toc_rule: bool = False
    # D: a JSON / CSS segment that matches nothing still runs the following JS segments with an empty input
    #    (Legado AnalyzeRule.getString / getStringList loop only `continue`s on a null result).
    json_miss_runs_following_js: bool = False
    # J: search / explore list item fields are evaluated in Legado BookList.getSearchItem order
    #    (name, author, kind, wordCount, lastChapter, intro, coverUrl, bookUrl last, BookList.kt:220-281).
    list_item_legado_field_order: bool = False
    # G: `java.getElement` returns the whole element list outside JSON mode (Legado AnalyzeRule.getElement).
    get_element_returns_list: bool = False
    # E: an empty chapterUrl rule makes the chapter URL the toc page URL (Legado BookChapterList.kt:230-241).
    empty_chapter_url_rule_uses_base_url: bool = False
    # U: Unicode format characters (Cf, e.g. U+200B) in source JS are ignored outside string literals (Rhino lexer).
    js_strip_format_chars: bool = False
    # G2: the body of a non-2xx response reaches the rules (Legado AnalyzeUrl never checks the status):
    #     search / explore 404 and 410, and any non-2xx for a pure-JS rule; 403 / 429 / 5xx / Cloudflare stay errors.
    http_error_body_to_rules: bool = False
    # Q: a `<first,other>` page segment inside the POST body of a search / explore URL option is chosen by page too
    #    (Legado AnalyzeUrl.replaceKeyPageJs covers the whole `url,{options}`); only comma segments, so XML body tags
    #    such as `<search>` survive (BookSourceEngine.swift buildUrlWithOptions applyLegadoPageSegments(requiresComma:)).
    page_segment_in_post_body: bool = False
    # H: when the request of the "all chapters" jump page fails (HTTP / transport error) the first page's chapters, backed up before
    #    the jump, are restored (BookSourceEngine getChapterList catch block); 1.1.1 leaves the cleared list and the TOC fails.
    all_chapters_jump_failure_restores: bool = False
    # A10 (R157 E1, mirrors the Swift engine package): rule-engine parity gaps closed in 1.1.2 only.
    # H: every search / explore item field evaluated before bookUrl (kind, wordCount, lastChapter, intro, coverUrl) is visible to
    #    the bookUrl rule as `book.*` (Legado BookList.kt:220-281 sets each field on the SearchBook first).
    search_item_fields_visible_to_book_url: bool = False
    # B: a chapter-list `@put` / `java.put` is stored on the chapter and reaches the content stage (Legado AnalyzeRule.put
    #    :740-749 writes BookChapter.variable; BookContent reads it back).
    chapter_put_reaches_content: bool = False
    # J: a chapterUrl rule with a JS segment that returns "" does not fall back to the literal template (that injected the
    #    script source as the URL); the chapter URL stays empty -> BookChapterList.kt:230-241.
    js_chapter_url_empty_no_literal_fallback: bool = False
    # G1: the JS Jsoup shim has Element.hasClass / first / last / parents / contains and the `org.jsoup` /
    #     `Packages.org.jsoup` aliases (Rhino exposes the whole Java package tree).
    jsoup_shim_extras: bool = False
    # G4: list helpers (`toArray`, `size`, `get`...) are not enumerable, so `for (i in list)` only sees the indices
    #     (Rhino NativeJavaList).
    js_list_helpers_non_enumerable: bool = False
    # G4: `java.getElements` in JSON mode returns the matched objects (Legado AnalyzeRule.getElements), not strings.
    js_get_elements_json_objects: bool = False
    # G5: a `##regex##replacement` suffix on an `@js:` / `<js>` rule is cut off and applied to the script result
    #     (Legado AnalyzeRule.SourceRule.init :545-620 cuts it for every mode).
    js_rule_replace_suffix: bool = False
    # 7: the validation content sample skips volume chapters and accepts an empty volume body (CheckSourceService.kt:211-215,
    #    BookContent.kt:149).
    validation_content_sample_skips_volumes: bool = False
    # I (R157 tocContent): no static "needs interaction" pre-verdict from rule text. The App decides at run time: the bulk
    #   validation suppresses the interaction (BookSourceInteractionContext.allowsUserInteraction == false), records it, and the
    #   verdict becomes needsInteraction only when the executed path actually hit it (applySuppressedInteraction). A script that
    #   merely mentions getVerificationCode / startBrowserAwait on a branch that is not taken (danmeirouwen, lenglengbb) passes.
    interaction_runtime_verdict_only: bool = False
    # R157 REV: the validation content sample dedups the first 16 chapters by URL BEFORE the volume filter (App
    #   BookSourceValidator.keepLastChapterPerURL == Legado BookChapterList.kt:119-128: keep only the LAST chapter per url, reversed
    #   toc keeps the first), then takes the first remaining chapter. 1.1.1 App takes chapters.first without dedup (a baseUrl
    #   placeholder row, BookChapterList.kt:230-241, is then the sample).
    content_sample_dedups_by_url: bool = False
    # R157 REV: a trailing JS segment of a list rule (`selector<js>..</js>`, `<js>..</js>selector`) runs ONCE on the whole previous
    #   list / string like Legado AnalyzeRule.getElements. 1.1.1 App: RuleType `.combine` evaluates every segment per item of the
    #   previous segment (`result` = the item string).
    list_rule_js_tail_whole_list: bool = False
    # R157 REV: `java.getString(rule, bool)` honours the second argument as the unescape flag (AnalyzeRule.kt:256). 1.1.1 App
    #   ignores it and always unescapes.
    get_string_unescape_flag: bool = False
    # A11 (R158 E1, mirrors the Swift engine package): rule-engine parity gaps closed in 1.1.2 only.
    # G: the validation content sample sees duplicates in the WHOLE first toc page, not only in the 16-chapter window
    #    (Legado BookChapterList.kt:114-125 dedups the whole list, keeping the last occurrence): rows beyond the window evaluate only
    #    their chapterUrl rule (no JS rule, no extra request, at most 5000 rows; App BookSourceEngine firstPage tail URLs).
    content_sample_dedups_whole_toc_page: bool = False
    # A: public-key decrypt with the bare transformation "RSA" retries a failed PKCS1 block as NoPadding and drops the leading zero bytes
    #    (JsEncodeUtils.kt:77 hands "RSA" to hutool AsymmetricCrypto -> Cipher.getInstance("RSA"), raw RSA on Android).
    rsa_bare_public_decrypt_no_padding: bool = False
    # D1: a jsLib function whose body `let` / `const`-redeclares one of its parameters loads (Rhino assigns; JavaScriptCore / QuickJS
    #     reject it as an early SyntaxError) and `Packages.cn.hutool.core.codec.Base64` exists (hutool is on Legado's classpath).
    js_param_redeclare_relaxed: bool = False
    js_hutool_base64_shim: bool = False
    # 4: `Packages.io.legado.app.help.http.StrResponse(url, body)` builds a response (StrResponse.kt) and a JSON-object `result` has
    #    the Java Map `get(key)` (null when missing).
    js_str_response_shim: bool = False
    js_json_object_result_get: bool = False
    # 5: `java.setContent(x)` replaces only AnalyzeRule.content; the `result` / `src` bindings stay (AnalyzeRule.kt:84-96, evalJS :774-788).
    set_content_keeps_result_binding: bool = False
    # 6: JSONPath `$..[?(@.x == 'y')]` deep-scans and returns every matching object (Jayway pre-order walk).
    jsonpath_deep_scan_filter: bool = False
    # 7 (G2): a search response whose URL differs from the request only by the URL fragment (`https://host#我的` vs `https://host/#%E6...`) is
    #    not a redirect to a landing page (a fragment is never sent; the App's finalUrl != searchUrl guard compares fragment-less URLs).
    redirect_guard_ignores_url_fragment: bool = False
    # G6: `java.getElement("$.x")` in JSON mode answers the WHOLE JSONPath value (Legado AnalyzeRule.getElement -> getObject = ctx.read(rule); App
    #    JSEngine getJSONValue_raw, `JSONPathEngine.selectWholeValueJSON`). 1.1.1 App: getElementRawFn = resolveElementsRule(...).first (the first row's text).
    js_get_element_json_whole_value: bool = False
    # R158d (jjwxc explore junk row): a script whose completion value is a DEFINED empty string (`if(c){java.getString("@@tag.a.1@text",false)}` on a row
    #    without a link) is "" (Legado / Rhino); the App's `s.isEmpty` -> global `result` fallback (JSEngine.evaluate / evaluateToArray) must only apply to
    #    an `undefined` / `null` completion. 1.1.1 App: any empty script value falls back to `result` (the whole list item).
    js_empty_completion_keeps_empty: bool = False


APP_PROFILES: dict[str, AppProfile] = {
    "1.1.1": AppProfile("1.1.1", True, "drop", "legacy-global", "invalid"),
    "1.1.2": AppProfile(
        "1.1.2", False, "gson-text", "on-demand-builtin", "needsInteraction",
        list_put_on_whole_content=True, json_leading_dot_deep_scan=True,
        detail_error_json_requires_empty_info=True, toc_volume_key_excluded=True,
        txt_shortcut_only_without_toc_rule=True, json_miss_runs_following_js=True,
        list_item_legado_field_order=True, get_element_returns_list=True,
        empty_chapter_url_rule_uses_base_url=True, js_strip_format_chars=True,
        http_error_body_to_rules=True, page_segment_in_post_body=True,
        all_chapters_jump_failure_restores=True,
        search_item_fields_visible_to_book_url=True, chapter_put_reaches_content=True,
        js_chapter_url_empty_no_literal_fallback=True, jsoup_shim_extras=True,
        js_list_helpers_non_enumerable=True, js_get_elements_json_objects=True,
        js_rule_replace_suffix=True, validation_content_sample_skips_volumes=True,
        interaction_runtime_verdict_only=True, content_sample_dedups_by_url=True,
        list_rule_js_tail_whole_list=True, get_string_unescape_flag=True,
        content_sample_dedups_whole_toc_page=True, rsa_bare_public_decrypt_no_padding=True,
        js_param_redeclare_relaxed=True, js_hutool_base64_shim=True,
        js_str_response_shim=True, js_json_object_result_get=True,
        set_content_keeps_result_binding=True, jsonpath_deep_scan_filter=True,
        redirect_guard_ignores_url_fragment=True, js_get_element_json_whole_value=True,
        js_empty_completion_keeps_empty=True,
    ),
}

_ACTIVE_APP_VERSION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "readori_app_version", default=DEFAULT_APP_VERSION
)


def normalize_app_version(value: object) -> str:
    """去空白后必须是受支持的版本，否则抛 ValueError（CLI 与 server 用；`both` 不是档案）。"""
    text = str(value if value is not None else "").strip()
    if text not in APP_PROFILES:
        raise ValueError(f"unsupported App version {text!r}; expected one of {', '.join(SUPPORTED_APP_VERSIONS)}")
    return text


def current_app_version() -> str:
    return _ACTIVE_APP_VERSION.get()


def current_app_profile() -> AppProfile:
    return APP_PROFILES[current_app_version()]


def _profile_for(version: str | None) -> AppProfile:
    if version is None:
        return current_app_profile()
    return APP_PROFILES[normalize_app_version(version)]


@contextmanager
def app_profile_scope(version: str) -> Iterator[AppProfile]:
    normalized = normalize_app_version(version)
    token = _ACTIVE_APP_VERSION.set(normalized)
    try:
        yield APP_PROFILES[normalized]
    finally:
        _ACTIVE_APP_VERSION.reset(token)


def run_in_current_profile(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在当前上下文的一份拷贝里同步执行 fn（拷贝里的档案与调用方一致，fn 内部改动不会外泄）。"""
    return contextvars.copy_context().run(fn, *args, **kwargs)


def bind_current_profile(fn: Callable[..., Any]) -> Callable[..., Any]:
    """现在就捕获当前上下文，返回可以交给 `threading.Thread(target=...)` 的包装函数。

    线程不会继承 contextvar；必须在「创建线程的那一侧」调用本函数，包装函数在线程里运行时用的是捕获时的档案。
    每个线程各调用一次：同一个 Context 对象不能被多个线程同时进入。
    """
    ctx = contextvars.copy_context()

    def runner(*args: Any, **kwargs: Any) -> Any:
        return ctx.run(fn, *args, **kwargs)

    return runner


# ---------------------------------------------------------------------------
# A1：@js: searchUrl 字面量捷径（1.1.1）
# ---------------------------------------------------------------------------

_SWIFT_URL_TERMINATORS = frozenset("\"' \n\r>)`")
_HTTPS_RE = re.compile(r"https://", re.IGNORECASE | re.ASCII)
_HTTP_RE = re.compile(r"http://", re.IGNORECASE | re.ASCII)


def swift_trim_whitespaces(text: str) -> str:
    """等价于 Swift `trimmingCharacters(in: .whitespaces)`：只去 Unicode Zs 和制表符，不去换行。"""

    def is_ws(ch: str) -> bool:
        return ch == "\t" or unicodedata.category(ch) == "Zs"

    start = 0
    end = len(text)
    while start < end and is_ws(text[start]):
        start += 1
    while end > start and is_ws(text[end - 1]):
        end -= 1
    return text[start:end]


def _swift_cluster_is_extended(text: str, index: int) -> bool:
    """text[index] 之后是否紧跟组合记号（同一个 Swift Character 不再等于终止符本身）。"""
    nxt = index + 1
    if nxt >= len(text):
        return False
    ch = text[nxt]
    return ch == "‍" or unicodedata.category(ch) in ("Mn", "Me", "Mc")


def app111_extract_first_http_url(text: str) -> str | None:
    """逐字移植 1.1.1 `BookSource.extractFirstHttpUrl`（2a2239e Models/BookSource.swift:816-845）。

    - 不区分大小写，取 `https://` 与 `http://` 中先出现的一个（同位置不可能，平局取 https）；
    - 前缀之后逐个 Swift Character 扫描，遇到 `"` `'` 空格 `\\n` `\\r` `>` `)` 反引号停止，扫到末尾就取到末尾；
    - `\\r\\n` 在 Swift 里是一个 Character，既不等于 `\\n` 也不等于 `\\r`，所以不会结束扫描（两字符一起跳过）；
      终止字符后面紧跟组合记号时同理（`\\n` / `\\r` 是控制字符，不会与后续记号合并）。
    """
    if not text:
        return None
    https = _HTTPS_RE.search(text)
    http = _HTTP_RE.search(text)
    if https is not None and (http is None or https.start() <= http.start()):
        start, prefix_len = https.start(), 8
    elif http is not None:
        start, prefix_len = http.start(), 7
    else:
        return None
    i = start + prefix_len
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r" and text[i : i + 2] == "\r\n":
            i += 2
            continue
        if ch in _SWIFT_URL_TERMINATORS:
            if ch not in "\r\n" and _swift_cluster_is_extended(text, i):
                i += 2
                while i < n and (text[i] == "‍" or unicodedata.category(text[i]) in ("Mn", "Me", "Mc")):
                    i += 1
                continue
            url = text[start:i]
            return url or None
        i += 1
    url = text[start:]
    return url or None


def app_search_url_template(src: dict[str, Any], version: str | None = None) -> str:
    """App 实际拿去请求的 searchUrl 模板（交给 build_url_with_options 之前）。

    1.1.1：`.whitespaces` 修剪后以 `@js:`（不区分大小写）开头，就用字面量当模板；取不到字面量则保留原文执行脚本。
    1.1.2：`searchUrl.strip()` 原样交给管线（脚本返回值才是搜索地址）。
    """
    profile = _profile_for(version)
    raw = src.get("searchUrl") if isinstance(src, dict) else None
    if not isinstance(raw, str):
        return ""
    if profile.search_url_js_literal_shortcut:
        trimmed = swift_trim_whitespaces(raw)
        if trimmed.lower().startswith("@js:"):
            return (app111_extract_first_http_url(trimmed) or raw).strip()
        return trimmed.strip()
    return raw.strip()


# ---------------------------------------------------------------------------
# A2：String 字段的 JSON 数组 / 对象（App 解码视图）
# ---------------------------------------------------------------------------

BOOK_SOURCE_STRING_FIELDS: tuple[str, ...] = (
    "bookSourceUrl",
    "bookSourceName",
    "bookSourceGroup",
    "bookSourceComment",
    "variableComment",
    "bookUrlPattern",
    "loginUrl",
    "loginUi",
    "loginCheckJs",
    "coverDecodeJs",
    "concurrentRate",
    "jsLib",
    "searchUrl",
    "exploreUrl",
    "exploreScreen",
    "audioSearchUrl",
    "ruleAudioUrl",
    "customTag",
)

RULE_STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "ruleSearch": (
        "init", "bookList", "name", "author", "intro", "kind", "lastChapter",
        "updateTime", "bookUrl", "coverUrl", "wordCount", "checkKeyWord",
    ),
    "ruleExplore": (
        "bookList", "name", "author", "intro", "kind", "lastChapter",
        "updateTime", "bookUrl", "coverUrl", "wordCount",
    ),
    "ruleBookInfo": (
        "init", "name", "author", "intro", "kind", "lastChapter", "updateTime",
        "coverUrl", "tocUrl", "wordCount", "isEnd", "canReToc", "canReName", "downloadUrls",
    ),
    "ruleToc": (
        "chapterList", "chapterName", "chapterUrl", "isVolume", "isVip", "isPay",
        "updateTime", "formatJs", "preUpdateJs", "nextTocUrl",
    ),
    "ruleContent": (
        "content", "title", "nextContentUrl", "webJs", "sourceRegex", "replaceRegex",
        "imageStyle", "imageDecode", "imageDecrypt", "payAction",
    ),
    "ruleReview": (
        "reviewUrl", "avatarRule", "contentRule", "postTimeRule", "reviewQuoteUrl",
        "voteUpUrl", "voteDownUrl", "postReviewUrl", "postQuoteUrl", "deleteUrl",
    ),
}

# decodeSerializedRuleObject 失败后的简写：整段字符串当作哪个字段（ruleReview 的简写在验证器里用不到，丢弃）。
RULE_SHORTHAND_FIELD: dict[str, str | None] = {
    "ruleSearch": "bookList",
    "ruleExplore": "bookList",
    "ruleBookInfo": "init",
    "ruleToc": "chapterList",
    "ruleContent": "content",
    "ruleReview": None,
}


class _GsonUnrepresentable(Exception):
    pass


def _gson_norm(value: Any, depth: int) -> Any:
    if depth > GSON_MAX_DEPTH:
        raise _GsonUnrepresentable("too deep")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _GsonUnrepresentable("non-finite")
        if value == int(value) and abs(value) < 1e15:
            return int(value)
        return value
    if isinstance(value, list):
        return [_gson_norm(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _gson_norm(item, depth + 1) for key, item in value.items()}
    raise _GsonUnrepresentable(type(value).__name__)


def gson_json_text(value: Any, depth: int = 0) -> str | None:
    """Legado `JsonElement.toString()` 的等价物（BookSource.swift LegadoGsonJSONText）。

    只接受 list / dict；嵌套超过 64 层、含非有限数字时返回 None（整个字段视为缺失）。
    紧凑输出，键名排序，不转义斜杠和中文，整数值的浮点数写成整数。
    """
    if depth > GSON_MAX_DEPTH or not isinstance(value, (list, dict)):
        return None
    try:
        normalized = _gson_norm(value, depth)
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (_GsonUnrepresentable, ValueError, TypeError, RecursionError):
        return None


def _lenient_string(value: Any, mode: str) -> str | None:
    """`decodeLenientStringIfPresent`：String / 数字 / 布尔 → String；容器按档案处理；null 与其它类型 → None（缺失）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if -(2**63) <= value < 2**63:
            return str(value)
        value = float(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, (list, dict)):
        if mode == "gson-text":
            return gson_json_text(value)
        return None
    return None


def _decode_serialized_rule_object(raw: str) -> dict[str, Any] | None:
    """`decodeSerializedRuleObject`：规则对象被存成 JSON 字符串（最多再套一层）时解开，失败返回 None。"""
    candidate = raw.strip()
    if not candidate:
        return None
    for _ in range(2):
        try:
            parsed = json.loads(candidate)
        except (ValueError, RecursionError):
            return None
        if isinstance(parsed, dict):
            return parsed
        if not isinstance(parsed, str):
            return None
        unwrapped = parsed.strip()
        if not unwrapped or unwrapped == candidate:
            return None
        candidate = unwrapped
    return None


def _decode_rule_value(rule_key: str, value: Any) -> dict[str, Any] | None:
    """规则字段在 App 里的解码结果（dict）；缺失 / 类型不对返回 None。字符串值先解开再套简写。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value != "":
        parsed = _decode_serialized_rule_object(value)
        if parsed is not None:
            return parsed
        field = RULE_SHORTHAND_FIELD.get(rule_key)
        if field:
            return {field: value}
    return None


def _apply_string_fields(target: dict[str, Any], fields: tuple[str, ...], mode: str) -> None:
    for field in fields:
        if field not in target:
            continue
        text = _lenient_string(target[field], mode)
        if text is None:
            del target[field]
        else:
            target[field] = text


def app_decode_view(src: dict[str, Any], version: str | None = None) -> tuple[dict[str, Any], str]:
    """按档案生成「App 解码视图」，在规则求值之前使用；不修改入参。返回 `(view, error)`，error 为空表示导入成功。

    - String 字段是 list / dict：1.1.1 删除该键，1.1.2 变成紧凑 JSON 文本；
    - 数字 / 布尔按 App 转字符串（两档案相同）；null 删除；
    - header 是对象时两档案都保持对象不变，是数组时按 String 字段处理；
    - 规则字段写成 JSON 字符串时解开（两档案相同），仍不是对象时按简写（bookList / init / chapterList / content）；
    - bookSourceUrl 处理后不是字符串（1.1.1 的对象 / 数组、缺失）：`app import rejects source: bookSourceUrl`。
    """
    profile = _profile_for(version)
    mode = profile.string_container_mode
    view: dict[str, Any] = copy.deepcopy(src) if isinstance(src, dict) else {}

    _apply_string_fields(view, BOOK_SOURCE_STRING_FIELDS, mode)

    if "header" in view:
        header = view["header"]
        if not isinstance(header, dict):
            text = _lenient_string(header, mode)
            if text is None:
                del view["header"]
            else:
                view["header"] = text

    for rule_key, fields in RULE_STRING_FIELDS.items():
        if rule_key not in view:
            continue
        rule = _decode_rule_value(rule_key, view[rule_key])
        if rule is None:
            del view[rule_key]
            continue
        rule = dict(rule)
        _apply_string_fields(rule, fields, mode)
        if rule_key == "ruleContent" and "imageDecode" not in rule and "imageDecrypt" in rule:
            rule["imageDecode"] = rule["imageDecrypt"]
        view[rule_key] = rule

    if not isinstance(view.get("bookSourceUrl"), str):
        return view, f"{APP_IMPORT_REJECTED_REASON}: bookSourceUrl"
    return view, ""


# ---------------------------------------------------------------------------
# A3：CryptoJS（脚本文本由提取脚本生成，禁止手改）
# ---------------------------------------------------------------------------

APP_111_CRYPTOJS_POLYFILL = r"""
var CryptoJS=(function(){
    var B64={
        encode:function(s){try{return btoa(unescape(encodeURIComponent(s)));}catch(e){return btoa(s);}},
        decode:function(s){try{return decodeURIComponent(escape(atob(s)));}catch(e){return atob(s);}}
    };
    var _AES={
        encrypt:function(msg,key){return B64.encode(typeof msg==='string'?msg:String(msg));},
        decrypt:function(enc,key){var s=(typeof enc==='object'&&enc.toString)?enc.toString():String(enc);try{return B64.decode(s);}catch(e){return s;}}
    };
    var enc={Utf8:{stringify:function(w){return w;},parse:function(s){return s;}},Base64:B64};
    return {
        MD5:function(s){return md5(s);},
        SHA256:function(s){return sha256(s);},
        Base64:B64,
        AES:{Pkcs7:_AES,CBC:_AES,ECB:_AES},
        enc:enc,
        lib:{CipherParams:function(o){this.ciphertext=o;this.toString=function(){return o;};}}
    };
})();
"""

APP_112_CRYPTOJS_BUILTIN = r"""
(function (root) {
    if (typeof root.CryptoJS !== 'undefined') { return; }

    // Native bridge (hex strings only). Failures come back as "!" + reason.
    var N = {
        cipher: root.__readoriCryptoJSCipher,
        digest: root.__readoriCryptoJSDigest,
        hmac: root.__readoriCryptoJSHmac,
        random: root.__readoriCryptoJSRandom,
        pbkdf2: root.__readoriCryptoJSPBKDF2
    };

    function nativeResult(value, what) {
        if (typeof value !== 'string' || value.charAt(0) === '!') {
            throw new Error(what + ' failed' + (typeof value === 'string' && value.length > 1 ? ': ' + value.substring(1) : ''));
        }
        return value;
    }

    var C = {};
    var lib = C.lib = {};
    var enc = C.enc = {};
    var algo = C.algo = {};
    var mode = C.mode = {};
    var pad = C.pad = {};
    var format = C.format = {};

    // ---------------------------------------------------------------- WordArray
    function WordArray(words, sigBytes) {
        this.words = words || [];
        this.sigBytes = (sigBytes === undefined) ? this.words.length * 4 : sigBytes;
    }
    WordArray.create = function (words, sigBytes) {
        if (words && typeof ArrayBuffer !== 'undefined') {
            var view = null;
            if (words instanceof ArrayBuffer) {
                view = new Uint8Array(words);
            } else if (ArrayBuffer.isView && ArrayBuffer.isView(words)) {
                view = new Uint8Array(words.buffer, words.byteOffset, words.byteLength);
            }
            if (view) {
                var fromBytes = [];
                for (var i = 0; i < view.length; i++) {
                    fromBytes[i >>> 2] |= view[i] << (24 - (i % 4) * 8);
                }
                return new WordArray(fromBytes, view.length);
            }
        }
        return new WordArray(words, sigBytes);
    };
    WordArray.random = function (nBytes) {
        var hex = '';
        var left = nBytes;
        while (left > 0) {
            var chunk = left > 1024 ? 1024 : left;
            hex += nativeResult(N.random(chunk), 'random');
            left -= chunk;
        }
        return Hex.parse(hex);
    };
    WordArray.prototype.toString = function (encoder) {
        return (encoder || Hex).stringify(this);
    };
    WordArray.prototype.clamp = function () {
        var words = this.words;
        var sigBytes = this.sigBytes;
        words[sigBytes >>> 2] &= 0xffffffff << (32 - (sigBytes % 4) * 8);
        words.length = Math.ceil(sigBytes / 4);
        return this;
    };
    WordArray.prototype.clone = function () {
        return new WordArray(this.words.slice(0), this.sigBytes);
    };
    WordArray.prototype.concat = function (wordArray) {
        var thisWords = this.words;
        var thatWords = wordArray.words;
        var thisSigBytes = this.sigBytes;
        var thatSigBytes = wordArray.sigBytes;
        this.clamp();
        if (thisSigBytes % 4) {
            for (var i = 0; i < thatSigBytes; i++) {
                var thatByte = (thatWords[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
                thisWords[(thisSigBytes + i) >>> 2] |= thatByte << (24 - ((thisSigBytes + i) % 4) * 8);
            }
        } else {
            for (var j = 0; j < thatSigBytes; j += 4) {
                thisWords[(thisSigBytes + j) >>> 2] = thatWords[j >>> 2];
            }
        }
        this.sigBytes += thatSigBytes;
        return this;
    };
    lib.WordArray = WordArray;

    function toWordArray(value) {
        if (value === null || value === undefined) { return new WordArray([], 0); }
        if (typeof value === 'string') { return Utf8.parse(value); }
        if (typeof value === 'object' && typeof value.sigBytes === 'number' && value.words) { return value; }
        if (typeof ArrayBuffer !== 'undefined' &&
            (value instanceof ArrayBuffer || (ArrayBuffer.isView && ArrayBuffer.isView(value)))) {
            return WordArray.create(value);
        }
        return Utf8.parse(String(value));
    }
    function hexOf(value) {
        return Hex.stringify(toWordArray(value));
    }

    // ----------------------------------------------------------------- encoders
    var Hex = enc.Hex = {
        stringify: function (wordArray) {
            var words = wordArray.words;
            var sigBytes = wordArray.sigBytes;
            var out = [];
            for (var i = 0; i < sigBytes; i++) {
                var bite = (words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
                out.push((bite >>> 4).toString(16));
                out.push((bite & 0x0f).toString(16));
            }
            return out.join('');
        },
        parse: function (hexStr) {
            hexStr = String(hexStr);
            var len = hexStr.length;
            var words = [];
            for (var i = 0; i < len; i += 2) {
                words[i >>> 3] |= parseInt(hexStr.substr(i, 2), 16) << (24 - (i % 8) * 4);
            }
            return new WordArray(words, Math.ceil(len / 2));
        }
    };

    var Latin1 = enc.Latin1 = {
        stringify: function (wordArray) {
            var words = wordArray.words;
            var sigBytes = wordArray.sigBytes;
            var out = [];
            for (var i = 0; i < sigBytes; i++) {
                out.push(String.fromCharCode((words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff));
            }
            return out.join('');
        },
        parse: function (latin1Str) {
            latin1Str = String(latin1Str);
            var len = latin1Str.length;
            var words = [];
            for (var i = 0; i < len; i++) {
                words[i >>> 2] |= (latin1Str.charCodeAt(i) & 0xff) << (24 - (i % 4) * 8);
            }
            return new WordArray(words, len);
        }
    };

    var Utf8 = enc.Utf8 = {
        stringify: function (wordArray) {
            try {
                return decodeURIComponent(escape(Latin1.stringify(wordArray)));
            } catch (e) {
                throw new Error('Malformed UTF-8 data');
            }
        },
        parse: function (utf8Str) {
            return Latin1.parse(unescape(encodeURIComponent(String(utf8Str))));
        }
    };

    var B64_MAP = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=';
    var B64URL_MAP = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

    function base64Stringify(wordArray, map) {
        var words = wordArray.words;
        var sigBytes = wordArray.sigBytes;
        var out = [];
        wordArray.clamp();
        for (var i = 0; i < sigBytes; i += 3) {
            var b1 = (words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff;
            var b2 = (words[(i + 1) >>> 2] >>> (24 - ((i + 1) % 4) * 8)) & 0xff;
            var b3 = (words[(i + 2) >>> 2] >>> (24 - ((i + 2) % 4) * 8)) & 0xff;
            var triplet = (b1 << 16) | (b2 << 8) | b3;
            for (var j = 0; j < 4 && i + j * 0.75 < sigBytes; j++) {
                out.push(map.charAt((triplet >>> (6 * (3 - j))) & 0x3f));
            }
        }
        var padChar = map.charAt(64);
        if (padChar) {
            while (out.length % 4) { out.push(padChar); }
        }
        return out.join('');
    }

    function base64Parse(str, map) {
        str = String(str).replace(/\s+/g, '');
        var len = str.length;
        var padChar = map.charAt(64);
        if (padChar) {
            var padIndex = str.indexOf(padChar);
            if (padIndex !== -1) { len = padIndex; }
        }
        var reverse = {};
        for (var r = 0; r < 64; r++) { reverse[map.charCodeAt(r)] = r; }
        var words = [];
        var nBytes = 0;
        for (var i = 0; i < len; i++) {
            if (i % 4) {
                var bits1 = reverse[str.charCodeAt(i - 1)] << ((i % 4) * 2);
                var bits2 = reverse[str.charCodeAt(i)] >>> (6 - (i % 4) * 2);
                words[nBytes >>> 2] |= (bits1 | bits2) << (24 - (nBytes % 4) * 8);
                nBytes++;
            }
        }
        return new WordArray(words, nBytes);
    }

    var Base64 = enc.Base64 = {
        stringify: function (wordArray) { return base64Stringify(wordArray, B64_MAP); },
        parse: function (str) { return base64Parse(str, B64_MAP); }
    };
    enc.Base64url = {
        stringify: function (wordArray) { return base64Stringify(wordArray, B64URL_MAP); },
        parse: function (str) { return base64Parse(str, B64URL_MAP); }
    };

    // Readori legacy (non-standard) helpers: plain string in, plain string out.
    function legacyBase64Encode(text) {
        return base64Stringify(Utf8.parse(String(text)), B64_MAP);
    }
    function legacyBase64Decode(text) {
        var parsed = base64Parse(text, B64_MAP);
        try {
            return Utf8.stringify(parsed);
        } catch (e) {
            return Latin1.stringify(parsed);
        }
    }
    Base64.encode = legacyBase64Encode;
    Base64.decode = legacyBase64Decode;
    C.Base64 = { encode: legacyBase64Encode, decode: legacyBase64Decode };

    // ------------------------------------------------------------------ hashing
    var HASH_NAMES = ['MD5', 'SHA1', 'SHA224', 'SHA256', 'SHA384', 'SHA512'];

    function digestWordArray(name, message) {
        return Hex.parse(nativeResult(N.digest(name, hexOf(message)), name));
    }
    function hmacWordArray(name, message, key) {
        return Hex.parse(nativeResult(N.hmac(name, hexOf(key), hexOf(message)), 'Hmac' + name));
    }
    function algoName(hasher, fallback) {
        if (!hasher) { return fallback; }
        if (typeof hasher === 'string') { return hasher.toUpperCase().replace('-', ''); }
        return hasher.__readoriAlgo || fallback;
    }

    function Hasher(name) {
        this.__name = name;
        this.__hex = '';
    }
    Hasher.prototype.reset = function () { this.__hex = ''; return this; };
    Hasher.prototype.update = function (message) { this.__hex += hexOf(message); return this; };
    Hasher.prototype.finalize = function (message) {
        if (message) { this.update(message); }
        var result = Hex.parse(nativeResult(N.digest(this.__name, this.__hex), this.__name));
        this.__hex = '';
        return result;
    };

    function Hmac(name, key) {
        this.__name = name;
        this.__key = hexOf(key);
        this.__hex = '';
    }
    Hmac.prototype.reset = function () { this.__hex = ''; return this; };
    Hmac.prototype.update = function (message) { this.__hex += hexOf(message); return this; };
    Hmac.prototype.finalize = function (message) {
        if (message) { this.update(message); }
        var result = Hex.parse(nativeResult(N.hmac(this.__name, this.__key, this.__hex), 'Hmac' + this.__name));
        this.__hex = '';
        return result;
    };

    HASH_NAMES.forEach(function (name) {
        C[name] = function (message) { return digestWordArray(name, message); };
        C['Hmac' + name] = function (message, key) { return hmacWordArray(name, message, key); };
        algo[name] = {
            __readoriAlgo: name,
            create: function () { return new Hasher(name); }
        };
    });
    algo.HMAC = {
        create: function (hasher, key) { return new Hmac(algoName(hasher, 'MD5'), key); }
    };

    // ----------------------------------------------------------------------- KDF
    // OpenSSL EVP_BytesToKey: block = H(block || password || salt), repeated.
    function evpDerive(password, salt, keySizeWords, ivSizeWords, hasherName, iterations) {
        var passwordHex = hexOf(password);
        var saltHex = salt ? hexOf(salt) : '';
        var totalWords = keySizeWords + ivSizeWords;
        var derivedHex = '';
        var blockHex = '';
        var guard = 0;
        while (derivedHex.length < totalWords * 8) {
            blockHex = nativeResult(N.digest(hasherName, blockHex + passwordHex + saltHex), 'EvpKDF');
            for (var i = 1; i < iterations; i++) {
                blockHex = nativeResult(N.digest(hasherName, blockHex), 'EvpKDF');
            }
            derivedHex += blockHex;
            if (++guard > 64) { break; }
        }
        return {
            key: Hex.parse(derivedHex.substring(0, keySizeWords * 8)),
            iv: Hex.parse(derivedHex.substring(keySizeWords * 8, totalWords * 8))
        };
    }

    C.EvpKDF = function (password, salt, cfg) {
        cfg = cfg || {};
        var keySize = cfg.keySize === undefined ? 4 : cfg.keySize;
        var iterations = cfg.iterations === undefined ? 1 : cfg.iterations;
        return evpDerive(password, salt, keySize, 0, algoName(cfg.hasher, 'MD5'), iterations).key;
    };

    // crypto-js 4.1 defaults: SHA1, 1 iteration, 128-bit key (4.2 changed them).
    C.PBKDF2 = function (password, salt, cfg) {
        cfg = cfg || {};
        var keySize = cfg.keySize === undefined ? 4 : cfg.keySize;
        var iterations = cfg.iterations === undefined ? 1 : cfg.iterations;
        var hasher = algoName(cfg.hasher, 'SHA1');
        var hex = nativeResult(
            N.pbkdf2(hasher, hexOf(password), hexOf(salt), iterations, keySize * 4),
            'PBKDF2'
        );
        return Hex.parse(hex);
    };

    // -------------------------------------------------------------- cipher bits
    mode.CBC = { __readoriMode: 'CBC' };
    mode.ECB = { __readoriMode: 'ECB' };
    mode.CFB = { __readoriMode: 'CFB' };
    mode.OFB = { __readoriMode: 'OFB' };
    mode.CTR = { __readoriMode: 'CTR' };

    function padBlockBytes(blockSizeWords) { return blockSizeWords * 4; }

    pad.Pkcs7 = {
        pad: function (data, blockSize) {
            var blockSizeBytes = padBlockBytes(blockSize);
            var nPaddingBytes = blockSizeBytes - data.sigBytes % blockSizeBytes;
            var paddingWord = (nPaddingBytes << 24) | (nPaddingBytes << 16) | (nPaddingBytes << 8) | nPaddingBytes;
            var paddingWords = [];
            for (var i = 0; i < nPaddingBytes; i += 4) { paddingWords.push(paddingWord); }
            data.concat(new WordArray(paddingWords, nPaddingBytes));
        },
        unpad: function (data) {
            var nPaddingBytes = data.words[(data.sigBytes - 1) >>> 2] & 0xff;
            data.sigBytes -= nPaddingBytes;
            if (data.sigBytes < 0) { data.sigBytes = 0; }
        }
    };
    pad.ZeroPadding = {
        pad: function (data, blockSize) {
            var blockSizeBytes = padBlockBytes(blockSize);
            data.clamp();
            data.sigBytes += blockSizeBytes - ((data.sigBytes % blockSizeBytes) || blockSizeBytes);
        },
        unpad: function (data) {
            var words = data.words;
            var i = data.sigBytes - 1;
            while (i >= 0 && !((words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff)) { i--; }
            data.sigBytes = i + 1;
        }
    };
    pad.NoPadding = {
        pad: function () {},
        unpad: function () {}
    };
    pad.AnsiX923 = {
        pad: function (data, blockSize) {
            var blockSizeBytes = padBlockBytes(blockSize);
            var nPaddingBytes = blockSizeBytes - data.sigBytes % blockSizeBytes;
            var lastBytePos = data.sigBytes + nPaddingBytes - 1;
            data.clamp();
            data.words[lastBytePos >>> 2] |= nPaddingBytes << (24 - (lastBytePos % 4) * 8);
            data.sigBytes += nPaddingBytes;
        },
        unpad: function (data) {
            var nPaddingBytes = data.words[(data.sigBytes - 1) >>> 2] & 0xff;
            data.sigBytes -= nPaddingBytes;
            if (data.sigBytes < 0) { data.sigBytes = 0; }
        }
    };
    pad.Iso10126 = {
        pad: function (data, blockSize) {
            var blockSizeBytes = padBlockBytes(blockSize);
            var nPaddingBytes = blockSizeBytes - data.sigBytes % blockSizeBytes;
            data.concat(WordArray.random(nPaddingBytes - 1)).concat(new WordArray([nPaddingBytes << 24], 1));
        },
        unpad: function (data) {
            var nPaddingBytes = data.words[(data.sigBytes - 1) >>> 2] & 0xff;
            data.sigBytes -= nPaddingBytes;
            if (data.sigBytes < 0) { data.sigBytes = 0; }
        }
    };
    pad.Iso97971 = {
        pad: function (data, blockSize) {
            data.concat(new WordArray([0x80000000], 1));
            pad.ZeroPadding.pad(data, blockSize);
        },
        unpad: function (data) {
            pad.ZeroPadding.unpad(data);
            data.sigBytes--;
            if (data.sigBytes < 0) { data.sigBytes = 0; }
        }
    };

    function CipherParams(cfg) {
        if (cfg) {
            for (var key in cfg) {
                if (Object.prototype.hasOwnProperty.call(cfg, key)) { this[key] = cfg[key]; }
            }
        }
    }
    CipherParams.create = function (cfg) { return new CipherParams(cfg); };
    CipherParams.prototype.toString = function (formatter) {
        return (formatter || this.formatter || format.OpenSSL).stringify(this);
    };
    lib.CipherParams = CipherParams;

    // OpenSSL format: Base64("Salted__" + 8-byte salt + ciphertext); 0x53616c74 0x65645f5f is "Salted__".
    format.OpenSSL = {
        stringify: function (cipherParams) {
            var ciphertext = cipherParams.ciphertext;
            var salt = cipherParams.salt;
            var wordArray;
            if (salt) {
                wordArray = new WordArray([0x53616c74, 0x65645f5f]).concat(salt).concat(ciphertext);
            } else {
                wordArray = ciphertext;
            }
            return wordArray.toString(Base64);
        },
        parse: function (openSSLStr) {
            var ciphertext = Base64.parse(openSSLStr);
            var words = ciphertext.words;
            var salt;
            if (words[0] === 0x53616c74 && words[1] === 0x65645f5f) {
                salt = new WordArray(words.slice(2, 4));
                words.splice(0, 4);
                ciphertext.sigBytes -= 16;
            }
            return CipherParams.create({ ciphertext: ciphertext, salt: salt });
        }
    };
    format.Hex = {
        stringify: function (cipherParams) {
            return cipherParams.ciphertext.toString(Hex);
        },
        parse: function (input) {
            return CipherParams.create({ ciphertext: Hex.parse(input) });
        }
    };

    var SPECS = {
        AES: { keySize: 8, ivSize: 4, blockSize: 4 },
        DES: { keySize: 2, ivSize: 2, blockSize: 2 },
        TripleDES: { keySize: 6, ivSize: 2, blockSize: 2 }
    };

    // Returns { name, keyHex } for the native cipher; throws on unusable key lengths.
    function nativeKey(specName, keyWordArray) {
        var keyHex = Hex.stringify(keyWordArray);
        var bytes = keyHex.length / 2;
        if (specName === 'AES') {
            if (bytes !== 16 && bytes !== 24 && bytes !== 32) { throw new Error('Invalid key length'); }
            return { name: 'AES', keyHex: keyHex };
        }
        if (bytes < 8) { throw new Error('Invalid key length'); }
        var k1 = keyHex.substr(0, 16);
        if (specName === 'DES') {
            return { name: 'DES', keyHex: k1 };
        }
        if (bytes < 16) {
            // K1 = K2 = K3: EDE collapses to single DES.
            return { name: 'DES', keyHex: k1 };
        }
        var k2 = keyHex.substr(16, 16);
        var k3 = bytes >= 24 ? keyHex.substr(32, 16) : k1;
        return { name: '3DES', keyHex: k1 + k2 + k3 };
    }

    function runCipher(encrypt, specName, input, key, cfg) {
        cfg = cfg || {};
        var spec = SPECS[specName];
        var modeObject = cfg.mode || mode.CBC;
        var modeName = (modeObject && modeObject.__readoriMode) || 'CBC';
        var padding = cfg.padding || pad.Pkcs7;
        var fmt = cfg.format || format.OpenSSL;
        var ivWordArray = cfg.iv ? toWordArray(cfg.iv) : null;
        var salt;
        var parsed = null;

        if (!encrypt) {
            if (typeof input === 'string') {
                parsed = fmt.parse(input);
            } else if (input && input.ciphertext) {
                parsed = input;
            } else {
                parsed = { ciphertext: toWordArray(input) };
            }
        }

        var keyWordArray;
        if (typeof key === 'string') {
            if (encrypt) {
                salt = cfg.salt ? toWordArray(cfg.salt) : WordArray.random(8);
            } else {
                salt = parsed.salt;
            }
            var derived = evpDerive(key, salt, spec.keySize, spec.ivSize, 'MD5', 1);
            keyWordArray = derived.key;
            ivWordArray = derived.iv;
        } else {
            keyWordArray = toWordArray(key);
        }
        var nativeKeyInfo = nativeKey(specName, keyWordArray);
        var ivHex = ivWordArray ? Hex.stringify(ivWordArray) : '';

        if (encrypt) {
            var data = toWordArray(input).clone();
            padding.pad(data, spec.blockSize);
            var encrypted = nativeResult(
                N.cipher('e', nativeKeyInfo.name, modeName, nativeKeyInfo.keyHex, ivHex, Hex.stringify(data)),
                specName + ' encrypt'
            );
            return CipherParams.create({
                ciphertext: Hex.parse(encrypted),
                key: keyWordArray,
                iv: ivWordArray,
                salt: salt,
                algorithm: specName,
                mode: modeObject,
                padding: padding,
                blockSize: spec.blockSize,
                formatter: fmt
            });
        }

        var ciphertextHex = Hex.stringify(toWordArray(parsed.ciphertext));
        var decrypted = N.cipher('d', nativeKeyInfo.name, modeName, nativeKeyInfo.keyHex, ivHex, ciphertextHex);
        if (typeof decrypted !== 'string' || decrypted.charAt(0) === '!') {
            return new WordArray([], 0);
        }
        var plain = Hex.parse(decrypted);
        padding.unpad(plain);
        return plain;
    }

    function cipherApi(specName) {
        var spec = SPECS[specName];
        return {
            keySize: spec.keySize,
            ivSize: spec.ivSize,
            blockSize: spec.blockSize,
            encrypt: function (message, key, cfg) { return runCipher(true, specName, message, key, cfg); },
            decrypt: function (ciphertext, key, cfg) { return runCipher(false, specName, ciphertext, key, cfg); }
        };
    }
    C.AES = cipherApi('AES');
    C.DES = cipherApi('DES');
    C.TripleDES = cipherApi('TripleDES');

    root.CryptoJS = C;
})(this);
"""

# 提取时已核对的摘要（UTF-8，LF；`""" + 换行` 之间的内容）。守卫 / 测试会重新计算。
APP_111_CRYPTOJS_POLYFILL_SHA256 = "ea8845cabe04d8ebbe1fc4d1300b7e138ae72afc41624d7045b631939c443317"
APP_112_CRYPTOJS_BUILTIN_SHA256 = "2e812c99be8f324c9f7030e2aa3ff80c324ac39a5ba61025ac9f5e89e9799b28"


# 上面两个 r""" 字面量按文件格式首尾各带一个换行，真正的脚本去掉它们（与 Swift 多行字符串的内容一致）。
APP_111_CRYPTOJS_POLYFILL = APP_111_CRYPTOJS_POLYFILL[1:-1]
APP_112_CRYPTOJS_BUILTIN = APP_112_CRYPTOJS_BUILTIN[1:-1]

_CRYPTOJS_NAME = "CryptoJS"
_CRYPTOJS_SELF_DEFINITION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\w$])CryptoJS\s*=(?![=>])"),
    re.compile(r"[\"']CryptoJS[\"']\s*\]\s*=(?![=>])"),
    re.compile(r"\bfunction\s+CryptoJS\b"),
)


def cryptojs_references(text: str) -> bool:
    """文本里出现 CryptoJS 才考虑安装（ReadoriCryptoJS.referencesCryptoJS）。"""
    return _CRYPTOJS_NAME in (text or "")


def cryptojs_self_defines(text: str) -> bool:
    """书源自己定义 CryptoJS 时不安装内置对象（ReadoriCryptoJS.selfDefinesCryptoJS，3 条正则）。"""
    if not cryptojs_references(text):
        return False
    return any(pattern.search(text) is not None for pattern in _CRYPTOJS_SELF_DEFINITION_PATTERNS)


def app_js_setup_plan(js_lib: str, code: str, version: str | None = None) -> dict[str, bool]:
    """JS 运行时在 java prelude 之后要装什么。

    1.1.1：假 CryptoJS 无条件装在 jsLib 之前（`legacyCryptoJS`），builtin 两处都不装；
    1.1.2：jsLib / 规则脚本文本引用 CryptoJS 且没有自定义时才装 builtin（全局已存在就不装由 builtin 自身保证）。
    """
    profile = _profile_for(version)
    if profile.cryptojs_mode == "legacy-global":
        return {
            "appGlobals": True,
            "legacyCryptoJS": True,
            "cryptoJSBeforeJsLib": False,
            "cryptoJSBeforeCode": False,
        }
    js_lib = js_lib or ""
    code = code or ""
    return {
        "appGlobals": True,
        "legacyCryptoJS": False,
        "cryptoJSBeforeJsLib": cryptojs_references(js_lib) and not cryptojs_self_defines(js_lib),
        "cryptoJSBeforeCode": cryptojs_references(code) and not cryptojs_self_defines(code),
    }


# ---- 原生桥（语义照 JSEngineCryptoJS.swift：十六进制进出，失败一律返回 "!" + 原因） ----

_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2})*")


def _hex_bytes(text: str) -> bytes | None:
    if not isinstance(text, str) or _HEX_RE.fullmatch(text) is None:
        return None
    return bytes.fromhex(text)


def _normalize_hash_name(name: str) -> str:
    return str(name).upper().replace("-", "")


_HASHLIB_NAMES = {
    "MD5": "md5",
    "SHA1": "sha1",
    "SHA224": "sha224",
    "SHA256": "sha256",
    "SHA384": "sha384",
    "SHA512": "sha512",
}
_PBKDF2_HASHES = frozenset(_HASHLIB_NAMES) - {"MD5"}


def cryptojs_cipher_hex(
    operation: str, algorithm: str, mode: str, key_hex: str, iv_hex: str, data_hex: str
) -> str:
    """operation "e" 加密 / 其它解密；AES 16/24/32 字节密钥，DES 8，3DES 24；CBC/ECB/CFB(=CFB128)/OFB/CTR；固定 NoPadding。"""
    key = _hex_bytes(key_hex)
    iv_raw = _hex_bytes(iv_hex)
    data = _hex_bytes(data_hex)
    if key is None or iv_raw is None or data is None:
        return "!invalid hex"
    alg = str(algorithm).upper()
    if alg == "AES":
        if len(key) not in (16, 24, 32):
            return "!Invalid key length"
        block_size = 16
    elif alg == "DES":
        if len(key) != 8:
            return "!Invalid key length"
        block_size = 8
        key = key * 3  # OpenSSL 3 默认 provider 不含单 DES：K‖K‖K 的 3DES 与单 DES 等价
    elif alg == "3DES":
        if len(key) != 24:
            return "!Invalid key length"
        block_size = 8
    else:
        return "!unsupported algorithm"

    mode_name = str(mode).upper()
    needs_aligned = True
    if mode_name in ("CBC", "ECB"):
        pass
    elif mode_name in ("CFB", "OFB", "CTR"):
        needs_aligned = False
    else:
        return "!unsupported mode"
    if alg != "AES" and mode_name == "CTR":
        return "!unsupported mode"  # 已知偏差：CommonCrypto 支持 DES/3DES CTR，cryptography 不支持
    if needs_aligned and len(data) % block_size != 0:
        return "!data not block aligned"
    if not data:
        return ""

    iv = iv_raw[:block_size]
    if len(iv) < block_size:
        iv = iv + bytes(block_size - len(iv))

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, modes

        if alg == "AES":
            from cryptography.hazmat.primitives.ciphers.algorithms import AES

            cipher_algorithm: Any = AES(key)
        else:
            try:
                from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
            except ImportError:
                from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES  # type: ignore[attr-defined,no-redef]
            cipher_algorithm = TripleDES(key)

        mode_object: Any
        if mode_name == "CBC":
            mode_object = modes.CBC(iv)
        elif mode_name == "ECB":
            mode_object = modes.ECB()
        elif mode_name in ("CFB", "OFB"):
            try:  # cryptography >= 48 moved the legacy stream modes to decrepit
                from cryptography.hazmat.decrepit.ciphers import modes as legacy_modes
            except ImportError:
                legacy_modes = modes
            mode_object = (legacy_modes.CFB if mode_name == "CFB" else legacy_modes.OFB)(iv)
        else:
            mode_object = modes.CTR(iv)
        cipher = Cipher(cipher_algorithm, mode_object)
        worker = cipher.encryptor() if operation == "e" else cipher.decryptor()
        return (worker.update(data) + worker.finalize()).hex()
    except Exception:
        return "!cipher init failed"


def cryptojs_digest_hex(algorithm: str, data_hex: str) -> str:
    data = _hex_bytes(data_hex)
    if data is None:
        return "!invalid hex"
    name = _HASHLIB_NAMES.get(_normalize_hash_name(algorithm))
    if name is None:
        return "!unsupported algorithm"
    return hashlib.new(name, data).hexdigest()


def cryptojs_hmac_hex(algorithm: str, key_hex: str, data_hex: str) -> str:
    key = _hex_bytes(key_hex)
    data = _hex_bytes(data_hex)
    if key is None or data is None:
        return "!invalid hex"
    name = _HASHLIB_NAMES.get(_normalize_hash_name(algorithm))
    if name is None:
        return "!unsupported algorithm"
    return hmac.new(key, data, name).hexdigest()


def cryptojs_random_hex(byte_count: int | float) -> str:
    try:
        count = int(byte_count)
    except (TypeError, ValueError, OverflowError):
        return "!invalid length"
    if count < 0 or count > 1024:
        return "!invalid length"
    if count == 0:
        return ""
    import os

    return os.urandom(count).hex()


def cryptojs_pbkdf2_hex(
    hasher: str, password_hex: str, salt_hex: str, iterations: int | float, key_bytes: int | float
) -> str:
    password = _hex_bytes(password_hex)
    salt = _hex_bytes(salt_hex)
    if password is None or salt is None:
        return "!invalid hex"
    name = _normalize_hash_name(hasher)
    if name not in _PBKDF2_HASHES:
        return "!unsupported hasher"  # CommonCrypto 的 PBKDF2 不支持 MD5 作 PRF
    try:
        size = int(key_bytes)
        rounds = max(1, min(int(iterations), 2**31 - 1))
    except (TypeError, ValueError, OverflowError):
        return "!invalid key size"
    if size <= 0 or size > 1024:
        return "!invalid key size"
    try:
        return hashlib.pbkdf2_hmac(_HASHLIB_NAMES[name], password, salt, rounds, size).hex()
    except Exception:
        return "!pbkdf2 failed"


# ---- App 全局函数（两档案都有：btoa / atob / md5 / sha256） ----

def app_btoa(text: str) -> str:
    """UTF-8 字节的 Base64。"""
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


def app_atob(text: str) -> str:
    """严格 Base64（必须补齐 `=`，不允许空白）→ 严格 UTF-8；任何失败都返回原文。"""
    raw = str(text)
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return raw


def app_md5(text: str) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def app_sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()



# ---- QuickJS 路径：纯 JS 版全局函数与原生桥 ----
# python-quickjs 在设了执行时限的 Context 里禁止回调 Python（"Can not call into Python with a time limit set."），
# 而验证器的 QuickJS 一直带时限，所以 btoa / atob / md5 / sha256 与 __readoriCryptoJS* 在 QuickJS 里必须是 JS 自己算。
# 语义与上面的 Python 版（以及 Node 路径的 __readoriAppNatives）逐字节一致：十六进制进出，失败返回 "!" + 原因。
# 同时提供拦截交互的 JS 侧记录（__readoriSuppressedInteractionLog），由验证器在每次求值后取走。
QUICKJS_APP_NATIVES_JS = r"""
(function () {
  'use strict';
  var root = globalThis;

  // ---- bytes / text helpers ------------------------------------------------------------------------------------
  function utf8Bytes(text) {
    var s = String(text), out = [];
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c >= 0xD800 && c <= 0xDBFF && i + 1 < s.length) {
        var d = s.charCodeAt(i + 1);
        if (d >= 0xDC00 && d <= 0xDFFF) { c = 0x10000 + ((c - 0xD800) << 10) + (d - 0xDC00); i++; } else { c = 0xFFFD; }
      } else if (c >= 0xD800 && c <= 0xDFFF) {
        c = 0xFFFD;
      }
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xC0 | (c >> 6), 0x80 | (c & 63));
      else if (c < 0x10000) out.push(0xE0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
      else out.push(0xF0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    }
    return out;
  }

  // Strict UTF-8 (no overlongs, no surrogates, nothing above U+10FFFF); null when the bytes are not valid UTF-8.
  function utf8Decode(bytes) {
    var parts = [], units = [], i = 0, n = bytes.length;
    function cont(index) { return index < n && (bytes[index] & 0xC0) === 0x80; }
    while (i < n) {
      var b = bytes[i++], cp;
      if (b < 0x80) {
        cp = b;
      } else if (b >= 0xC2 && b <= 0xDF) {
        if (!cont(i)) return null;
        cp = ((b & 0x1F) << 6) | (bytes[i++] & 63);
      } else if (b >= 0xE0 && b <= 0xEF) {
        if (!cont(i) || !cont(i + 1)) return null;
        cp = ((b & 0x0F) << 12) | ((bytes[i] & 63) << 6) | (bytes[i + 1] & 63);
        i += 2;
        if (cp < 0x800 || (cp >= 0xD800 && cp <= 0xDFFF)) return null;
      } else if (b >= 0xF0 && b <= 0xF4) {
        if (!cont(i) || !cont(i + 1) || !cont(i + 2)) return null;
        cp = ((b & 0x07) << 18) | ((bytes[i] & 63) << 12) | ((bytes[i + 1] & 63) << 6) | (bytes[i + 2] & 63);
        i += 3;
        if (cp < 0x10000 || cp > 0x10FFFF) return null;
      } else {
        return null;
      }
      if (cp >= 0x10000) { cp -= 0x10000; units.push(0xD800 + (cp >> 10), 0xDC00 + (cp & 1023)); } else { units.push(cp); }
      if (units.length >= 4096) { parts.push(String.fromCharCode.apply(null, units)); units = []; }
    }
    if (units.length) parts.push(String.fromCharCode.apply(null, units));
    return parts.join('');
  }

  var B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  function b64Encode(bytes) {
    var out = [];
    for (var i = 0; i < bytes.length; i += 3) {
      var hasB = i + 1 < bytes.length, hasC = i + 2 < bytes.length;
      var n = (bytes[i] << 16) | ((hasB ? bytes[i + 1] : 0) << 8) | (hasC ? bytes[i + 2] : 0);
      out.push(B64.charAt((n >> 18) & 63), B64.charAt((n >> 12) & 63), hasB ? B64.charAt((n >> 6) & 63) : '=', hasC ? B64.charAt(n & 63) : '=');
    }
    return out.join('');
  }
  var STRICT_B64 = /^(?:[A-Za-z0-9+\/]{4})*(?:[A-Za-z0-9+\/]{2}==|[A-Za-z0-9+\/]{3}=)?$/;
  function b64DecodeStrict(text) {
    if (!STRICT_B64.test(text)) return null;
    var out = [], acc = 0, bits = 0;
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (ch === '=') break;
      acc = (acc << 6) | B64.indexOf(ch);
      bits += 6;
      if (bits >= 8) { bits -= 8; out.push((acc >> bits) & 255); acc &= (1 << bits) - 1; }
    }
    return out;
  }

  var HEX_RE = /^(?:[0-9a-fA-F]{2})*$/;
  function hexBytes(value) {
    if (typeof value !== 'string' || !HEX_RE.test(value)) return null;
    var out = new Array(value.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(value.substr(i * 2, 2), 16);
    return out;
  }
  function toHex(bytes) {
    var out = [];
    for (var i = 0; i < bytes.length; i++) out.push((bytes[i] < 16 ? '0' : '') + bytes[i].toString(16));
    return out.join('');
  }
  function textOf(value) { return (value === undefined || value === null) ? '' : String(value); }

  // ---- hashes (byte array in, byte array out) ----------------------------------------------------------------------
  var md5Table = null;
  var MD5_SHIFTS = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21];
  function md5(bytes) {
    if (!md5Table) {
      md5Table = new Int32Array(64);
      for (var t = 0; t < 64; t++) md5Table[t] = Math.floor(Math.abs(Math.sin(t + 1)) * 4294967296) | 0;
    }
    var n = bytes.length, blocks = ((n + 8) >> 6) + 1, words = new Int32Array(blocks * 16);
    for (var i = 0; i < n; i++) words[i >> 2] |= bytes[i] << ((i & 3) * 8);
    words[n >> 2] |= 0x80 << ((n & 3) * 8);
    words[blocks * 16 - 2] = (n * 8) | 0;
    words[blocks * 16 - 1] = Math.floor(n / 536870912) | 0;
    var a0 = 0x67452301, b0 = 0xefcdab89 | 0, c0 = 0x98badcfe | 0, d0 = 0x10325476;
    for (var blk = 0; blk < blocks; blk++) {
      var o = blk * 16, a = a0, b = b0, c = c0, d = d0;
      for (var r = 0; r < 64; r++) {
        var f, g;
        if (r < 16) { f = (b & c) | (~b & d); g = r; }
        else if (r < 32) { f = (d & b) | (~d & c); g = (5 * r + 1) & 15; }
        else if (r < 48) { f = b ^ c ^ d; g = (3 * r + 5) & 15; }
        else { f = c ^ (b | ~d); g = (7 * r) & 15; }
        var x = (a + f + md5Table[r] + words[o + g]) | 0, s = MD5_SHIFTS[r];
        a = d; d = c; c = b;
        b = (b + ((x << s) | (x >>> (32 - s)))) | 0;
      }
      a0 = (a0 + a) | 0; b0 = (b0 + b) | 0; c0 = (c0 + c) | 0; d0 = (d0 + d) | 0;
    }
    var out = [], state = [a0, b0, c0, d0];
    for (var k = 0; k < 4; k++) for (var j = 0; j < 4; j++) out.push((state[k] >>> (j * 8)) & 255);
    return out;
  }

  // Big-endian padded message (block = 64 or 128 bytes, length field = 8 or 16 bytes).
  function padBE(bytes, block, lengthBytes, prefix) {
    var n = bytes.length, total = Math.ceil((n + 1 + lengthBytes) / block) * block, out = new Uint8Array(total);
    for (var i = 0; i < n; i++) out[i] = bytes[i];
    out[n] = 0x80;
    var allBytes = n + (prefix || 0), bitsLow = (allBytes * 8) >>> 0, bitsHigh = Math.floor(allBytes / 536870912);
    out[total - 4] = bitsLow >>> 24; out[total - 3] = (bitsLow >>> 16) & 255; out[total - 2] = (bitsLow >>> 8) & 255; out[total - 1] = bitsLow & 255;
    out[total - 8] = bitsHigh >>> 24; out[total - 7] = (bitsHigh >>> 16) & 255; out[total - 6] = (bitsHigh >>> 8) & 255; out[total - 5] = bitsHigh & 255;
    return out;
  }
  function wordsToBytes(words, count) {
    var out = [];
    for (var i = 0; i < count; i++) out.push((words[i] >>> 24) & 255, (words[i] >>> 16) & 255, (words[i] >>> 8) & 255, words[i] & 255);
    return out;
  }

  var SHA1_INIT = [0x67452301, 0xefcdab89 | 0, 0x98badcfe | 0, 0x10325476, 0xc3d2e1f0 | 0];
  function sha1Blocks(hh, p) {
    var h0 = hh[0], h1 = hh[1], h2 = hh[2], h3 = hh[3], h4 = hh[4], w = new Int32Array(80);
    for (var o = 0; o < p.length; o += 64) {
      var t;
      for (t = 0; t < 16; t++) w[t] = (p[o + t * 4] << 24) | (p[o + t * 4 + 1] << 16) | (p[o + t * 4 + 2] << 8) | p[o + t * 4 + 3];
      for (t = 16; t < 80; t++) { var v = w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16]; w[t] = (v << 1) | (v >>> 31); }
      var a = h0, b = h1, c = h2, d = h3, e = h4;
      for (t = 0; t < 80; t++) {
        var f, k;
        if (t < 20) { f = (b & c) | (~b & d); k = 0x5a827999; }
        else if (t < 40) { f = b ^ c ^ d; k = 0x6ed9eba1; }
        else if (t < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc | 0; }
        else { f = b ^ c ^ d; k = 0xca62c1d6 | 0; }
        var tmp = (((a << 5) | (a >>> 27)) + f + e + k + w[t]) | 0;
        e = d; d = c; c = (b << 30) | (b >>> 2); b = a; a = tmp;
      }
      h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0; h4 = (h4 + e) | 0;
    }
    hh[0] = h0; hh[1] = h1; hh[2] = h2; hh[3] = h3; hh[4] = h4;
  }
  function sha1(bytes) {
    var hh = Int32Array.from(SHA1_INIT);
    sha1Blocks(hh, padBE(bytes, 64, 8));
    return wordsToBytes(hh, 5);
  }

  // SHA-2 constants are derived exactly (BigInt integer roots of the first primes) the first time they are needed.
  var sha2Constants = null;
  function isqrt(n) {
    var x = BigInt(Math.floor(Math.sqrt(Number(n)))), y;
    for (;;) { y = (x + n / x) >> 1n; if (y === x || y === x + 1n || y === x - 1n) break; x = y; }
    while (x * x > n) x -= 1n;
    while ((x + 1n) * (x + 1n) <= n) x += 1n;
    return x;
  }
  function icbrt(n) {
    var x = BigInt(Math.floor(Math.cbrt(Number(n)))), y;
    for (var i = 0; i < 200; i++) { y = (2n * x + n / (x * x)) / 3n; if (y === x) break; x = y; }
    while (x * x * x > n) x -= 1n;
    while ((x + 1n) * (x + 1n) * (x + 1n) <= n) x += 1n;
    return x;
  }
  function loadSha2Constants() {
    if (sha2Constants) return sha2Constants;
    var primes = [], candidate = 2;
    while (primes.length < 80) {
      var isPrime = true;
      for (var i = 0; i < primes.length && primes[i] * primes[i] <= candidate; i++) if (candidate % primes[i] === 0) { isPrime = false; break; }
      if (isPrime) primes.push(candidate);
      candidate++;
    }
    var mask64 = (1n << 64n) - 1n;
    var sqrtFrac = function (p) { return isqrt(BigInt(p) << 128n) & mask64; };
    var cbrtFrac = function (p) { return icbrt(BigInt(p) << 192n) & mask64; };
    var split = function (v) { return [Number(v >> 32n) | 0, Number(v & 0xffffffffn) | 0]; };
    var k512 = new Int32Array(160), init512 = new Int32Array(16), init384 = new Int32Array(16);
    var k256 = new Int32Array(64), init256 = new Int32Array(8), init224 = new Int32Array(8);
    for (var t = 0; t < 80; t++) {
      var kk = cbrtFrac(primes[t]), pair = split(kk);
      k512[t * 2] = pair[0]; k512[t * 2 + 1] = pair[1];
      if (t < 64) k256[t] = pair[0];
    }
    for (var h = 0; h < 8; h++) {
      var s1 = sqrtFrac(primes[h]), s2 = sqrtFrac(primes[h + 8]), p1 = split(s1), p2 = split(s2);
      init512[h * 2] = p1[0]; init512[h * 2 + 1] = p1[1];
      init384[h * 2] = p2[0]; init384[h * 2 + 1] = p2[1];
      init256[h] = p1[0];
      init224[h] = p2[1];
    }
    sha2Constants = { k256: k256, k512: k512, init256: init256, init224: init224, init512: init512, init384: init384 };
    return sha2Constants;
  }

  function sha256Blocks(hh, p) {
    var kt = loadSha2Constants().k256, w = new Int32Array(64);
    for (var o = 0; o < p.length; o += 64) {
      var t;
      for (t = 0; t < 16; t++) w[t] = (p[o + t * 4] << 24) | (p[o + t * 4 + 1] << 16) | (p[o + t * 4 + 2] << 8) | p[o + t * 4 + 3];
      for (t = 16; t < 64; t++) {
        var x15 = w[t - 15], x2 = w[t - 2];
        var s0 = ((x15 >>> 7) | (x15 << 25)) ^ ((x15 >>> 18) | (x15 << 14)) ^ (x15 >>> 3);
        var s1 = ((x2 >>> 17) | (x2 << 15)) ^ ((x2 >>> 19) | (x2 << 13)) ^ (x2 >>> 10);
        w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
      }
      var a = hh[0], b = hh[1], c = hh[2], d = hh[3], e = hh[4], f = hh[5], g = hh[6], h = hh[7];
      for (t = 0; t < 64; t++) {
        var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var t1 = (h + S1 + ((e & f) ^ (~e & g)) + kt[t] + w[t]) | 0;
        var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      hh[0] = (hh[0] + a) | 0; hh[1] = (hh[1] + b) | 0; hh[2] = (hh[2] + c) | 0; hh[3] = (hh[3] + d) | 0;
      hh[4] = (hh[4] + e) | 0; hh[5] = (hh[5] + f) | 0; hh[6] = (hh[6] + g) | 0; hh[7] = (hh[7] + h) | 0;
    }
  }
  function sha256Family(bytes, initial, outWords) {
    var hh = Int32Array.from(initial);
    sha256Blocks(hh, padBE(bytes, 64, 8));
    return wordsToBytes(hh, outWords);
  }
  function sha256(bytes) { return sha256Family(bytes, loadSha2Constants().init256, 8); }
  function sha224(bytes) { return sha256Family(bytes, loadSha2Constants().init224, 7); }

  // 64-bit lanes are (hi, lo) pairs of int32.
  function sha512Blocks(hh, p) {
    var kt = loadSha2Constants().k512, w = new Int32Array(160);
    for (var o = 0; o < p.length; o += 128) {
      var t;
      for (t = 0; t < 32; t++) w[t] = (p[o + t * 4] << 24) | (p[o + t * 4 + 1] << 16) | (p[o + t * 4 + 2] << 8) | p[o + t * 4 + 3];
      for (t = 16; t < 80; t++) {
        var h15 = w[(t - 15) * 2], l15 = w[(t - 15) * 2 + 1], h2 = w[(t - 2) * 2], l2 = w[(t - 2) * 2 + 1];
        var s0h = ((h15 >>> 1) | (l15 << 31)) ^ ((h15 >>> 8) | (l15 << 24)) ^ (h15 >>> 7);
        var s0l = ((l15 >>> 1) | (h15 << 31)) ^ ((l15 >>> 8) | (h15 << 24)) ^ ((l15 >>> 7) | (h15 << 25));
        var s1h = ((h2 >>> 19) | (l2 << 13)) ^ ((l2 >>> 29) | (h2 << 3)) ^ (h2 >>> 6);
        var s1l = ((l2 >>> 19) | (h2 << 13)) ^ ((h2 >>> 29) | (l2 << 3)) ^ ((l2 >>> 6) | (h2 << 26));
        var lo = (w[(t - 16) * 2 + 1] >>> 0) + (s0l >>> 0) + (w[(t - 7) * 2 + 1] >>> 0) + (s1l >>> 0);
        w[t * 2 + 1] = lo | 0;
        w[t * 2] = (w[(t - 16) * 2] + s0h + w[(t - 7) * 2] + s1h + Math.floor(lo / 4294967296)) | 0;
      }
      var ah = hh[0], al = hh[1], bh = hh[2], bl = hh[3], ch = hh[4], cl = hh[5], dh = hh[6], dl = hh[7];
      var eh = hh[8], el = hh[9], fh = hh[10], fl = hh[11], gh = hh[12], gl = hh[13], hhh = hh[14], hhl = hh[15];
      for (t = 0; t < 80; t++) {
        var S1h = ((eh >>> 14) | (el << 18)) ^ ((eh >>> 18) | (el << 14)) ^ ((el >>> 9) | (eh << 23));
        var S1l = ((el >>> 14) | (eh << 18)) ^ ((el >>> 18) | (eh << 14)) ^ ((eh >>> 9) | (el << 23));
        var chH = (eh & fh) ^ (~eh & gh), chL = (el & fl) ^ (~el & gl);
        var l1 = (hhl >>> 0) + (S1l >>> 0) + (chL >>> 0) + (kt[t * 2 + 1] >>> 0) + (w[t * 2 + 1] >>> 0);
        var t1l = l1 | 0;
        var t1h = (hhh + S1h + chH + kt[t * 2] + w[t * 2] + Math.floor(l1 / 4294967296)) | 0;
        var S0h = ((ah >>> 28) | (al << 4)) ^ ((al >>> 2) | (ah << 30)) ^ ((al >>> 7) | (ah << 25));
        var S0l = ((al >>> 28) | (ah << 4)) ^ ((ah >>> 2) | (al << 30)) ^ ((ah >>> 7) | (al << 25));
        var majH = (ah & bh) ^ (ah & ch) ^ (bh & ch), majL = (al & bl) ^ (al & cl) ^ (bl & cl);
        var l2s = (S0l >>> 0) + (majL >>> 0);
        var t2l = l2s | 0;
        var t2h = (S0h + majH + Math.floor(l2s / 4294967296)) | 0;
        hhh = gh; hhl = gl; gh = fh; gl = fl; fh = eh; fl = el;
        var ne = (dl >>> 0) + (t1l >>> 0);
        el = ne | 0; eh = (dh + t1h + Math.floor(ne / 4294967296)) | 0;
        dh = ch; dl = cl; ch = bh; cl = bl; bh = ah; bl = al;
        var na = (t1l >>> 0) + (t2l >>> 0);
        al = na | 0; ah = (t1h + t2h + Math.floor(na / 4294967296)) | 0;
      }
      var vars = [ah, al, bh, bl, ch, cl, dh, dl, eh, el, fh, fl, gh, gl, hhh, hhl];
      for (var i = 0; i < 16; i += 2) {
        var sum = (hh[i + 1] >>> 0) + (vars[i + 1] >>> 0);
        hh[i + 1] = sum | 0;
        hh[i] = (hh[i] + vars[i] + Math.floor(sum / 4294967296)) | 0;
      }
    }
  }
  function sha512Family(bytes, initial, outBytes) {
    var hh = Int32Array.from(initial);
    sha512Blocks(hh, padBE(bytes, 128, 16));
    return wordsToBytes(hh, 16).slice(0, outBytes);
  }
  function sha512(bytes) { return sha512Family(bytes, loadSha2Constants().init512, 64); }
  function sha384(bytes) { return sha512Family(bytes, loadSha2Constants().init384, 48); }

  // Big-endian Merkle-Damgard hashes also expose their block function so HMAC / PBKDF2 can reuse the key blocks.
  var HASHES = {
    MD5: { fn: md5, block: 64, size: 16 },
    SHA1: { fn: sha1, block: 64, size: 20, lengthBytes: 8, init: function () { return Int32Array.from(SHA1_INIT); }, blocks: sha1Blocks },
    SHA224: { fn: sha224, block: 64, size: 28, lengthBytes: 8, init: function () { return Int32Array.from(loadSha2Constants().init224); }, blocks: sha256Blocks },
    SHA256: { fn: sha256, block: 64, size: 32, lengthBytes: 8, init: function () { return Int32Array.from(loadSha2Constants().init256); }, blocks: sha256Blocks },
    SHA384: { fn: sha384, block: 128, size: 48, lengthBytes: 16, init: function () { return Int32Array.from(loadSha2Constants().init384); }, blocks: sha512Blocks },
    SHA512: { fn: sha512, block: 128, size: 64, lengthBytes: 16, init: function () { return Int32Array.from(loadSha2Constants().init512); }, blocks: sha512Blocks }
  };
  function hashKey(name) { return String(name).toUpperCase().replace(/-/g, ''); }
  function lookupHash(name) {
    var key = hashKey(name);
    return Object.prototype.hasOwnProperty.call(HASHES, key) ? HASHES[key] : null;
  }

  function hmacFactory(hash, keyBytes) {
    var key = keyBytes.length > hash.block ? hash.fn(keyBytes) : keyBytes.slice();
    while (key.length < hash.block) key.push(0);
    var ipad = [], opad = [];
    for (var i = 0; i < hash.block; i++) { ipad.push(key[i] ^ 0x36); opad.push(key[i] ^ 0x5c); }
    if (!hash.blocks) return function (message) { return hash.fn(opad.concat(hash.fn(ipad.concat(message)))); };
    var innerState = hash.init(), outerState = hash.init();
    hash.blocks(innerState, Uint8Array.from(ipad));
    hash.blocks(outerState, Uint8Array.from(opad));
    var finish = function (state, message) {
      var st = Int32Array.from(state);
      hash.blocks(st, padBE(message, hash.block, hash.lengthBytes, hash.block));
      return st;
    };
    var wordCount = hash.block === 64 ? 8 : 16;
    return function (message) {
      var inner = finish(innerState, message), digest = wordsToBytes(inner, wordCount).slice(0, hash.size);
      return wordsToBytes(finish(outerState, digest), wordCount).slice(0, hash.size);
    };
  }

  // ---- AES ---------------------------------------------------------------------------------------------------
  var aes = null;
  function aesTables() {
    if (aes) return aes;
    var sbox = new Uint8Array(256), inv = new Uint8Array(256), p = 1, q = 1, i;
    do {
      p = (p ^ (p << 1) ^ ((p & 0x80) ? 0x1b : 0)) & 0xff;
      q = (q ^ (q << 1)) & 0xff; q = (q ^ (q << 2)) & 0xff; q = (q ^ (q << 4)) & 0xff;
      if (q & 0x80) q ^= 0x09;
      var x = q ^ (((q << 1) | (q >> 7)) & 0xff) ^ (((q << 2) | (q >> 6)) & 0xff) ^ (((q << 3) | (q >> 5)) & 0xff) ^ (((q << 4) | (q >> 4)) & 0xff);
      sbox[p] = (x ^ 0x63) & 0xff;
    } while (p !== 1);
    sbox[0] = 0x63;
    for (i = 0; i < 256; i++) inv[sbox[i]] = i;
    var xtime = function (v) { return ((v << 1) ^ ((v & 0x80) ? 0x1b : 0)) & 0xff; };
    var mul = function (a, b) {
      var r = 0;
      while (b) { if (b & 1) r ^= a; a = xtime(a); b >>= 1; }
      return r;
    };
    var m = {};
    [1, 2, 3, 9, 11, 13, 14].forEach(function (c) {
      var table = new Uint8Array(256);
      for (var v = 0; v < 256; v++) table[v] = mul(v, c);
      m[c] = table;
    });
    aes = { sbox: sbox, inv: inv, m: m };
    return aes;
  }
  function aesCipher(key) {
    var T = aesTables(), sbox = T.sbox, m = T.m, nk = key.length / 4, rounds = nk + 6, total = 4 * (rounds + 1);
    var rk = new Uint8Array(total * 4), i, j, rcon = 1;
    for (i = 0; i < key.length; i++) rk[i] = key[i];
    for (i = nk; i < total; i++) {
      var t0 = rk[4 * (i - 1)], t1 = rk[4 * (i - 1) + 1], t2 = rk[4 * (i - 1) + 2], t3 = rk[4 * (i - 1) + 3];
      if (i % nk === 0) {
        var r = t0;
        t0 = sbox[t1] ^ rcon; t1 = sbox[t2]; t2 = sbox[t3]; t3 = sbox[r];
        rcon = ((rcon << 1) ^ ((rcon & 0x80) ? 0x1b : 0)) & 0xff;
      } else if (nk > 6 && i % nk === 4) {
        t0 = sbox[t0]; t1 = sbox[t1]; t2 = sbox[t2]; t3 = sbox[t3];
      }
      rk[4 * i] = rk[4 * (i - nk)] ^ t0; rk[4 * i + 1] = rk[4 * (i - nk) + 1] ^ t1;
      rk[4 * i + 2] = rk[4 * (i - nk) + 2] ^ t2; rk[4 * i + 3] = rk[4 * (i - nk) + 3] ^ t3;
    }
    function addKey(s, round) { for (var n = 0; n < 16; n++) s[n] ^= rk[round * 16 + n]; }
    function shiftRows(s) {
      var t;
      t = s[1]; s[1] = s[5]; s[5] = s[9]; s[9] = s[13]; s[13] = t;
      t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
      t = s[15]; s[15] = s[11]; s[11] = s[7]; s[7] = s[3]; s[3] = t;
    }
    function invShiftRows(s) {
      var t;
      t = s[13]; s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;
      t = s[2]; s[2] = s[10]; s[10] = t; t = s[6]; s[6] = s[14]; s[14] = t;
      t = s[3]; s[3] = s[7]; s[7] = s[11]; s[11] = s[15]; s[15] = t;
    }
    function mix(s, c0, c1, c2, c3) {
      var m0 = m[c0], m1 = m[c1], m2 = m[c2], m3 = m[c3];
      for (var col = 0; col < 16; col += 4) {
        var a = s[col], b = s[col + 1], c = s[col + 2], d = s[col + 3];
        s[col] = m0[a] ^ m1[b] ^ m2[c] ^ m3[d];
        s[col + 1] = m3[a] ^ m0[b] ^ m1[c] ^ m2[d];
        s[col + 2] = m2[a] ^ m3[b] ^ m0[c] ^ m1[d];
        s[col + 3] = m1[a] ^ m2[b] ^ m3[c] ^ m0[d];
      }
    }
    return {
      size: 16,
      encrypt: function (block) {
        var s = Uint8Array.from(block), round, n;
        addKey(s, 0);
        for (round = 1; round < rounds; round++) {
          for (n = 0; n < 16; n++) s[n] = sbox[s[n]];
          shiftRows(s);
          mix(s, 2, 3, 1, 1);
          addKey(s, round);
        }
        for (n = 0; n < 16; n++) s[n] = sbox[s[n]];
        shiftRows(s);
        addKey(s, rounds);
        return Array.from(s);
      },
      decrypt: function (block) {
        var s = Uint8Array.from(block), round, n;
        addKey(s, rounds);
        for (round = rounds - 1; round >= 1; round--) {
          invShiftRows(s);
          for (n = 0; n < 16; n++) s[n] = T.inv[s[n]];
          addKey(s, round);
          mix(s, 14, 11, 13, 9);
        }
        invShiftRows(s);
        for (n = 0; n < 16; n++) s[n] = T.inv[s[n]];
        addKey(s, 0);
        return Array.from(s);
      }
    };
  }

  // ---- DES / 3DES (EDE, three keys; a single DES key runs as K||K||K) ----------------------------------------------
  var IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7];
  var DES_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1];
  var DES_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25];
  var PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4];
  var PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32];
  var DES_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1];
  var SBOXES = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
      4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
      0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
      13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
      10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
      4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
      9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
      1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
      7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11]
  ];
  var FP = null;
  function toBits(bytes) {
    var bits = new Uint8Array(bytes.length * 8);
    for (var i = 0; i < bytes.length; i++) for (var j = 0; j < 8; j++) bits[i * 8 + j] = (bytes[i] >> (7 - j)) & 1;
    return bits;
  }
  function fromBits(bits) {
    var out = new Array(bits.length / 8);
    for (var i = 0; i < out.length; i++) {
      var v = 0;
      for (var j = 0; j < 8; j++) v = (v << 1) | bits[i * 8 + j];
      out[i] = v;
    }
    return out;
  }
  function permute(bits, table) {
    var out = new Uint8Array(table.length);
    for (var i = 0; i < table.length; i++) out[i] = bits[table[i] - 1];
    return out;
  }
  function desSubkeys(key8) {
    var cd = permute(toBits(key8), PC1), keys = [];
    var c = Array.prototype.slice.call(cd, 0, 28), d = Array.prototype.slice.call(cd, 28, 56);
    for (var round = 0; round < 16; round++) {
      for (var s = 0; s < DES_SHIFTS[round]; s++) { c.push(c.shift()); d.push(d.shift()); }
      keys.push(permute(c.concat(d), PC2));
    }
    return keys;
  }
  function desBlock(block, subkeys, decrypt) {
    if (!FP) { FP = new Array(64); for (var n = 0; n < 64; n++) FP[IP[n] - 1] = n + 1; }
    var bits = permute(toBits(block), IP), l = bits.subarray(0, 32), r = bits.subarray(32, 64);
    for (var round = 0; round < 16; round++) {
      var k = subkeys[decrypt ? 15 - round : round], e = permute(r, DES_E), f = new Uint8Array(32), i, j;
      for (i = 0; i < 48; i++) e[i] ^= k[i];
      for (i = 0; i < 8; i++) {
        var o = i * 6, row = (e[o] << 1) | e[o + 5], col = (e[o + 1] << 3) | (e[o + 2] << 2) | (e[o + 3] << 1) | e[o + 4];
        var v = SBOXES[i][row * 16 + col];
        for (j = 0; j < 4; j++) f[i * 4 + j] = (v >> (3 - j)) & 1;
      }
      var pf = permute(f, DES_P), next = new Uint8Array(32);
      for (i = 0; i < 32; i++) next[i] = l[i] ^ pf[i];
      l = r; r = next;
    }
    var pre = new Uint8Array(64);
    pre.set(r, 0); pre.set(l, 32);
    return fromBits(permute(pre, FP));
  }
  function tripleDesCipher(key) {
    var k1 = desSubkeys(key.slice(0, 8)), k2 = desSubkeys(key.slice(8, 16)), k3 = desSubkeys(key.slice(16, 24));
    return {
      size: 8,
      encrypt: function (block) { return desBlock(desBlock(desBlock(block, k1, false), k2, true), k3, false); },
      decrypt: function (block) { return desBlock(desBlock(desBlock(block, k3, true), k2, false), k1, true); }
    };
  }

  // ---- modes (padding is the caller's business, like the App bridge: NoPadding) ------------------------------------
  function xorInto(data, offset, stream, length) {
    var out = [];
    for (var i = 0; i < length; i++) out.push(data[offset + i] ^ stream[i]);
    return out;
  }
  function runMode(cipher, mode, encrypt, iv, data) {
    var size = cipher.size, out = [], i, block;
    if (mode === 'ECB' || mode === 'CBC') {
      var prev = iv;
      for (i = 0; i < data.length; i += size) {
        block = data.slice(i, i + size);
        if (mode === 'ECB') {
          out = out.concat(encrypt ? cipher.encrypt(block) : cipher.decrypt(block));
        } else if (encrypt) {
          prev = cipher.encrypt(xorInto(block, 0, prev, size));
          out = out.concat(prev);
        } else {
          out = out.concat(xorInto(cipher.decrypt(block), 0, prev, size));
          prev = block;
        }
      }
      return out;
    }
    var feedback = iv.slice();
    if (mode === 'CFB') {
      for (i = 0; i < data.length; i += size) {
        var n = Math.min(size, data.length - i), keystream = cipher.encrypt(feedback), chunk = xorInto(data, i, keystream, n);
        out = out.concat(chunk);
        if (n === size) feedback = encrypt ? chunk : data.slice(i, i + size);
      }
      return out;
    }
    if (mode === 'OFB') {
      for (i = 0; i < data.length; i += size) {
        feedback = cipher.encrypt(feedback);
        out = out.concat(xorInto(data, i, feedback, Math.min(size, data.length - i)));
      }
      return out;
    }
    var counter = iv.slice(); // CTR: big-endian increment over the whole block
    for (i = 0; i < data.length; i += size) {
      out = out.concat(xorInto(data, i, cipher.encrypt(counter), Math.min(size, data.length - i)));
      for (var pos = size - 1; pos >= 0; pos--) { counter[pos] = (counter[pos] + 1) & 255; if (counter[pos] !== 0) break; }
    }
    return out;
  }

  // ---- natives: hex in, hex out, failures come back as "!" + reason (same contract as the Node / Python bridges) ----
  function cipherHex(operation, algorithm, mode, keyHex, ivHex, dataHex) {
    var key = hexBytes(keyHex), ivRaw = hexBytes(ivHex), data = hexBytes(dataHex);
    if (!key || !ivRaw || !data) return '!invalid hex';
    var alg = String(algorithm).toUpperCase(), size, makeCipher;
    if (alg === 'AES') {
      if (key.length !== 16 && key.length !== 24 && key.length !== 32) return '!Invalid key length';
      size = 16;
      makeCipher = function () { return aesCipher(key); };
    } else if (alg === 'DES') {
      if (key.length !== 8) return '!Invalid key length';
      size = 8;
      makeCipher = function () { return tripleDesCipher(key.concat(key, key)); };
    } else if (alg === '3DES') {
      if (key.length !== 24) return '!Invalid key length';
      size = 8;
      makeCipher = function () { return tripleDesCipher(key); };
    } else {
      return '!unsupported algorithm';
    }
    var modeName = String(mode).toUpperCase(), needsAligned = true;
    if (modeName === 'CBC' || modeName === 'ECB') needsAligned = true;
    else if (modeName === 'CFB' || modeName === 'OFB' || modeName === 'CTR') needsAligned = false;
    else return '!unsupported mode';
    if (alg !== 'AES' && modeName === 'CTR') return '!unsupported mode';
    if (needsAligned && data.length % size !== 0) return '!data not block aligned';
    if (data.length === 0) return '';
    var iv = ivRaw.slice(0, size);
    while (iv.length < size) iv.push(0);
    try {
      return toHex(runMode(makeCipher(), modeName, operation === 'e', iv, data));
    } catch (error) {
      return '!cipher init failed';
    }
  }
  function digestHex(algorithm, dataHex) {
    var data = hexBytes(dataHex);
    if (!data) return '!invalid hex';
    var hash = lookupHash(algorithm);
    if (!hash) return '!unsupported algorithm';
    return toHex(hash.fn(data));
  }
  function hmacHex(algorithm, keyHex, dataHex) {
    var key = hexBytes(keyHex), data = hexBytes(dataHex);
    if (!key || !data) return '!invalid hex';
    var hash = lookupHash(algorithm);
    if (!hash) return '!unsupported algorithm';
    return toHex(hmacFactory(hash, key)(data));
  }
  function randomHex(byteCount) {
    var count = Math.trunc(Number(byteCount));
    if (!isFinite(count) || count < 0 || count > 1024) return '!invalid length';
    var out = [];
    for (var i = 0; i < count; i++) out.push(Math.floor(Math.random() * 256));
    return toHex(out);
  }
  function pbkdf2Hex(hasher, passwordHex, saltHex, iterations, keyBytes) {
    var password = hexBytes(passwordHex), salt = hexBytes(saltHex);
    if (!password || !salt) return '!invalid hex';
    var key = hashKey(hasher), hash = lookupHash(hasher);
    if (!hash || key === 'MD5') return '!unsupported hasher';
    var size = Math.trunc(Number(keyBytes)), rounds = Math.max(1, Math.min(Math.trunc(Number(iterations)), 2147483647));
    if (!isFinite(size) || !isFinite(rounds)) return '!invalid key size';
    if (size <= 0 || size > 1024) return '!invalid key size';
    try {
      var prf = hmacFactory(hash, password), out = [];
      for (var block = 1; out.length < size; block++) {
        var u = prf(salt.concat([(block >>> 24) & 255, (block >>> 16) & 255, (block >>> 8) & 255, block & 255]));
        var acc = u.slice();
        for (var round = 1; round < rounds; round++) {
          u = prf(u);
          for (var i = 0; i < acc.length; i++) acc[i] ^= u[i];
        }
        out = out.concat(acc);
      }
      return toHex(out.slice(0, size));
    } catch (error) {
      return '!pbkdf2 failed';
    }
  }

  // ---- App globals: btoa / atob / md5 / sha256 (UTF-8 semantics) ---------------------------------------------------
  root.btoa = function (value) { return b64Encode(utf8Bytes(textOf(value))); };
  root.atob = function (value) {
    var raw = textOf(value), bytes = b64DecodeStrict(raw);
    if (bytes === null) return raw;
    var decoded = utf8Decode(bytes);
    return decoded === null ? raw : decoded;
  };
  root.md5 = function (value) { return toHex(md5(utf8Bytes(textOf(value)))); };
  root.sha256 = function (value) { return toHex(sha256(utf8Bytes(textOf(value)))); };
  root.__readoriCryptoJSCipher = cipherHex;
  root.__readoriCryptoJSDigest = digestHex;
  root.__readoriCryptoJSHmac = hmacHex;
  root.__readoriCryptoJSRandom = randomHex;
  root.__readoriCryptoJSPBKDF2 = pbkdf2Hex;

  // ---- blocked-interaction log: QuickJS cannot call back into Python while a time limit is set, so the runtime
  // collects the kinds here and the validator drains them after each evaluation.
  root.__readoriSuppressedInteractionLog = [];
  root.__readoriRecordSuppressedInteraction = function (kind) {
    var name = String(kind);
    if (root.__readoriSuppressedInteractionLog.indexOf(name) < 0) root.__readoriSuppressedInteractionLog.push(name);
    return '';
  };
})();
"""

# ---------------------------------------------------------------------------
# 档案敏感度、结论、对比标签、分组 token
# ---------------------------------------------------------------------------

_CRYPTOJS_REMOTE_LOADER_RE = re.compile(r"eval\s*\([^;]{0,120}?java\.(?:ajax|get|cacheFile)\b|java\.importScript\s*\(")
_INTERACTION_CALL_RE = re.compile(
    r"\b(?:startBrowser|startBrowserAwait|startBrowserDp|showBrowser|showReadingBrowser|getVerificationCode)\s*\("
)


def _all_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _all_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_strings(nested)


def _has_json_container_string_field(src: dict[str, Any]) -> bool:
    for field in BOOK_SOURCE_STRING_FIELDS:
        if isinstance(src.get(field), (list, dict)):
            return True
    if isinstance(src.get("header"), list):
        return True
    for rule_key, fields in RULE_STRING_FIELDS.items():
        if rule_key not in src:
            continue
        rule = _decode_rule_value(rule_key, src[rule_key])
        if rule is None:
            continue
        if any(isinstance(rule.get(field), (list, dict)) for field in fields):
            return True
    return False


def app_profile_sensitivity(src: dict[str, Any]) -> list[str]:
    """静态特征：两版 App 在这条书源上可能行为不同的原因。空列表 = 确定性代码路径上两版完全相同。"""
    if not isinstance(src, dict):
        return []
    features: list[str] = []

    raw_search = src.get("searchUrl")
    if isinstance(raw_search, str):
        trimmed = swift_trim_whitespaces(raw_search)
        if trimmed.lower().startswith("@js:") and app111_extract_first_http_url(trimmed) is not None:
            features.append("searchUrlJsLiteral")

    texts = list(_all_strings(src))
    if any(_CRYPTOJS_NAME in text or _CRYPTOJS_REMOTE_LOADER_RE.search(text) for text in texts):
        features.append("cryptoJS")

    if _has_json_container_string_field(src):
        features.append("jsonStringField")

    if any(_INTERACTION_CALL_RE.search(text) for text in texts):
        features.append("interaction")
    return features


_STATIC_INTERACTION_FIELDS = (
    "searchUrl", "exploreUrl", "loginCheckJs", "ruleSearch", "ruleExplore",
    "ruleBookInfo", "ruleToc", "ruleContent",
)


def static_interaction_kinds(src: dict[str, Any]) -> list[str]:
    """`source_requires_interactive_verification` 命中的原因：getVerificationCode / openUrl / startBrowserAwait(loginCheckJs)。"""
    if not isinstance(src, dict):
        return []
    script = "\n".join(text for field in _STATIC_INTERACTION_FIELDS for text in _all_strings(src.get(field))).lower()
    kinds: list[str] = []
    if re.search(r"\bgetverificationcode\s*\(", script):
        kinds.append("getVerificationCode")
    if re.search(r"\bopenurl\s*\(", script):
        kinds.append("openUrl")
    login_check = "\n".join(_all_strings(src.get("loginCheckJs"))).lower()
    if re.search(r"\bstartbrowserawait\s*\(", login_check):
        kinds.append("startBrowserAwait")
    return kinds


def app_verdict(
    detail: dict[str, Any],
    passed: bool,
    version: str | None = None,
    src: dict[str, Any] | None = None,
) -> str:
    """`"pass" | "invalid" | "needsInteraction" | "timeout"`。

    规则：passed → pass；failureReason 含 timeout → timeout；运行时记录到被拦下的交互、拦截之前没有真实失败、
    且档案是 needsInteraction → needsInteraction；静态预判（interactive verification required）在 1.1.2 下，
    原因是 getVerificationCode 或 loginCheckJs 里的 startBrowserAwait 时同样是 needsInteraction（openUrl App 不记录，仍是 invalid）。
    静态原因取自 `detail["staticInteractionKinds"]`，缺省时由 `src` 重新推导。
    """
    if passed:
        return "pass"
    profile = _profile_for(version)
    detail = detail or {}
    reason = str(detail.get("failureReason") or "")
    if "timeout" in reason.lower():
        return "timeout"
    if profile.interaction_verdict == "needsInteraction":
        if detail.get("suppressedInteractions") and not detail.get("failureBeforeInteraction"):
            return "needsInteraction"
        if reason == INTERACTIVE_VERIFICATION_REASON:
            kinds = detail.get("staticInteractionKinds")
            if kinds is None and src is not None:
                kinds = static_interaction_kinds(src)
            if any(kind in ("getVerificationCode", "startBrowserAwait") for kind in (kinds or [])):
                return "needsInteraction"
    return "invalid"


_VERDICT_ZH = {
    "pass": "可用",
    "invalid": "失效",
    "needsInteraction": "需要登录/验证",
    "timeout": "超时",
}


def compatibility_label(v111: str, v112: str) -> str:
    if v111 == "pass" and v112 == "pass":
        return "两版均可用"
    if v111 == "invalid" and v112 == "pass":
        return "仅 1.1.2 可用"
    if v111 == "pass" and v112 == "invalid":
        return "1.1.1 可用、1.1.2 失效"
    if v111 == "invalid" and v112 == "invalid":
        return "两版均失效"
    if v111 == "invalid" and v112 == "needsInteraction":
        return "1.1.1 失效、1.1.2 需要登录/验证"
    return f"1.1.1 {_VERDICT_ZH.get(v111, v111)} / 1.1.2 {_VERDICT_ZH.get(v112, v112)}"


def current_validation_mmdd() -> str:
    """与 validate_source_packages.current_validation_group_tag 相同：洛杉矶时区的 MMDD（DST 由系统时区库处理）。"""
    try:
        return datetime.now(ZoneInfo(VALIDATION_TIMEZONE_KEY)).strftime("%m%d")
    except ZoneInfoNotFoundError:
        return datetime.now().strftime("%m%d")


def pass_group_tokens(
    version: str, sensitive: bool, proven_on_111: bool | None, mmdd: str | None = None
) -> list[str]:
    """通过源写进 bookSourceGroup 的 token（调用方用 `",".join(...)`）。

    1.1.1：`[MMDD]`。1.1.2：已在 1.1.1 验证通过或不是档案敏感源 → `[MMDD]`；否则 `[MMDD, "需1.1.2"]`。
    mmdd 缺省时按洛杉矶时区取当天；VSP 传入自己的 `current_validation_group_tag()` 即可。
    """
    day = mmdd if mmdd is not None else current_validation_mmdd()
    if normalize_app_version(version) == "1.1.1":
        return [day]
    if proven_on_111 is True or not sensitive:
        return [day]
    return [day, APP_112_ONLY_GROUP_TOKEN]
