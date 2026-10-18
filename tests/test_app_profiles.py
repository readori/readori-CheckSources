import base64
import hashlib
import hmac
import re
import subprocess
import threading
from pathlib import Path

import pytest

from validator import app_profiles as ap

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]

LEGACY_111_SHA256 = "ea8845cabe04d8ebbe1fc4d1300b7e138ae72afc41624d7045b631939c443317"
BUILTIN_112_SHA256 = "2e812c99be8f324c9f7030e2aa3ff80c324ac39a5ba61025ac9f5e89e9799b28"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def test_default_profile_is_public_111() -> None:
    assert ap.DEFAULT_APP_VERSION == "1.1.1"
    assert ap.current_app_version() == "1.1.1"
    with ap.app_profile_scope("1.1.2"):
        assert ap.current_app_version() == "1.1.2"
        assert ap.current_app_profile().version == "1.1.2"
    assert ap.current_app_version() == "1.1.1"
    with pytest.raises(ValueError):
        ap.normalize_app_version("both")
    assert ap.normalize_app_version(" 1.1.2 ") == "1.1.2"
    with pytest.raises(ValueError):
        with ap.app_profile_scope("1.0.0"):
            pass


def test_profile_does_not_leak_across_threads() -> None:
    seen: dict[str, str] = {}

    def read(name: str) -> None:
        seen[name] = ap.current_app_version()

    with ap.app_profile_scope("1.1.2"):
        bound = threading.Thread(target=ap.bind_current_profile(read), args=("bound",))
        copied = threading.Thread(target=__import__("contextvars").copy_context().run, args=(read, "copied"))
        bare = threading.Thread(target=read, args=("bare",))
        for thread in (bound, copied, bare):
            thread.start()
        for thread in (bound, copied, bare):
            thread.join()
        sync: list[str] = []
        ap.run_in_current_profile(lambda: sync.append(ap.current_app_version()))
        assert sync == ["1.1.2"]
    assert seen == {"bound": "1.1.2", "copied": "1.1.2", "bare": "1.1.1"}


def test_app111_literal_first_and_terminators() -> None:
    extract = ap.app111_extract_first_http_url
    assert extract("@js:\nvar host='https://a.example';\nhost+'/s?q='+key") == "https://a.example"
    assert extract("@js: var dead = 'https://dead.example/x'; 'https://live.example/s?q=' + key") == "https://dead.example/x"
    assert extract("@js: 'HTTP://b.example' + 'https://a.example'") == "HTTP://b.example"
    assert extract("@js: u='http://x.example/a(b)'") == "http://x.example/a(b"
    assert extract("@js: 'https://x.example/s,{\"method\":\"POST\"}'") == "https://x.example/s,{"
    assert extract("@js: x=https://x.example/a\r\nfoo'") == "https://x.example/a\r\nfoo"
    assert extract("@js: source.getKey()+'/s?q='+key") is None
    assert extract("") is None
    # no terminator: runs to the end; a lone \r or \n still terminates
    assert extract("go https://a.example/x") == "https://a.example/x"
    assert extract("https://a.example/x\nrest") == "https://a.example/x"
    assert extract("https://a.example/x\rrest") == "https://a.example/x"
    # https wins when it comes first, http when it comes first
    assert extract("a http://h.example b https://s.example") == "http://h.example"


def test_swift_trim_whitespaces_keeps_newlines() -> None:
    assert ap.swift_trim_whitespaces(" \t x 　") == "x"
    assert ap.swift_trim_whitespaces("\n x \n") == "\n x \n"


def test_app_search_url_template_per_profile() -> None:
    def tpl(raw: str, version: str) -> str:
        return ap.app_search_url_template({"searchUrl": raw}, version)

    cases = {
        " \t@js: 'https://a.example/s?q={{key}}'": ("https://a.example/s?q={{key}}", "@js: 'https://a.example/s?q={{key}}'"),
        "\n@js: 'https://a.example/s'": ("@js: 'https://a.example/s'", "@js: 'https://a.example/s'"),
        "<js>'https://a.example/s?q='+key</js>": ("<js>'https://a.example/s?q='+key</js>",) * 2,
        "  /s?q={{key}}  ": ("/s?q={{key}}",) * 2,
    }
    for raw, (expected111, expected112) in cases.items():
        assert tpl(raw, "1.1.1") == expected111, raw
        assert tpl(raw, "1.1.2") == expected112, raw
    # @js: without any literal keeps the script in both profiles; the scope decides when version is omitted
    assert tpl("@JS: source.getKey()+key", "1.1.1") == "@JS: source.getKey()+key"
    assert ap.app_search_url_template({}) == ""
    assert ap.app_search_url_template({"searchUrl": 5}) == ""
    with ap.app_profile_scope("1.1.2"):
        assert ap.app_search_url_template({"searchUrl": "@js: 'https://a.example/s'"}) == "@js: 'https://a.example/s'"
    assert ap.app_search_url_template({"searchUrl": "@js: 'https://a.example/s'"}) == "https://a.example/s"


def test_decode_view_explore_and_login_arrays() -> None:
    src = {
        "bookSourceUrl": "https://a.example",
        "exploreUrl": [{"title": "玄幻", "url": "/c/1"}, {"title": "分组"}],
        "loginUi": [{"name": "账号", "type": "text"}],
    }
    snapshot = repr(src)
    view111, err111 = ap.app_decode_view(src, "1.1.1")
    view112, err112 = ap.app_decode_view(src, "1.1.2")
    assert not err111 and not err112
    assert "exploreUrl" not in view111 and "loginUi" not in view111
    assert view112["exploreUrl"] == '[{"title":"玄幻","url":"/c/1"},{"title":"分组"}]'
    assert view112["loginUi"] == '[{"name":"账号","type":"text"}]'
    assert repr(src) == snapshot and isinstance(src["exploreUrl"], list)
    # sorted keys, unescaped slash
    assert ap.gson_json_text({"b": 1, "a": "x/y"}) == '{"a":"x/y","b":1}'
    assert ap.gson_json_text("scalar") is None


def test_decode_view_rule_subfield_and_scalars() -> None:
    src = {
        "bookSourceUrl": "https://a.example",
        "ruleContent": {"content": {"a": 1}},
        "bookSourceGroup": 12,
        "bookSourceComment": 2.0,
        "variableComment": 1.5,
        "customTag": True,
        "header": {"User-Agent": "UA"},
        "jsLib": None,
    }
    view111, _ = ap.app_decode_view(src, "1.1.1")
    view112, _ = ap.app_decode_view(src, "1.1.2")
    assert "content" not in view111["ruleContent"]
    assert view112["ruleContent"]["content"] == '{"a":1}'
    for view in (view111, view112):
        assert view["bookSourceGroup"] == "12"
        assert view["bookSourceComment"] == "2"
        assert view["variableComment"] == "1.5"
        assert view["customTag"] == "true"
        assert view["header"] == {"User-Agent": "UA"}
        assert "jsLib" not in view
    array_header = {"bookSourceUrl": "https://a.example", "header": ["x"]}
    assert "header" not in ap.app_decode_view(array_header, "1.1.1")[0]
    assert ap.app_decode_view(array_header, "1.1.2")[0]["header"] == '["x"]'
    assert src["ruleContent"]["content"] == {"a": 1}


def test_decode_view_depth_cap() -> None:
    nested: list = []
    for _ in range(70):
        nested = [nested]
    src = {"bookSourceUrl": "https://a.example", "exploreUrl": nested}
    view, err = ap.app_decode_view(src, "1.1.2")
    assert not err and "exploreUrl" not in view
    shallow: list = []
    for _ in range(9):
        shallow = [shallow]
    view, _ = ap.app_decode_view({"bookSourceUrl": "https://a.example", "exploreUrl": shallow}, "1.1.2")
    assert view["exploreUrl"] == "[" * 10 + "]" * 10


def test_decode_view_serialized_rule_object_and_shorthand() -> None:
    src = {
        "bookSourceUrl": "https://a.example",
        "ruleSearch": '{"bookList":".b","name":"a@text"}',
        "ruleToc": ".list li",
        "ruleBookInfo": "var x=1",
        "ruleContent": '"{\\"content\\":\\"#c\\"}"',
        "ruleExplore": "",
    }
    for version in ("1.1.1", "1.1.2"):
        view, err = ap.app_decode_view(src, version)
        assert not err
        assert view["ruleSearch"] == {"bookList": ".b", "name": "a@text"}
        assert view["ruleToc"] == {"chapterList": ".list li"}
        assert view["ruleBookInfo"] == {"init": "var x=1"}
        assert view["ruleContent"] == {"content": "#c"}
        assert "ruleExplore" not in view
    # image decrypt alias
    view, _ = ap.app_decode_view({"bookSourceUrl": "u", "ruleContent": {"imageDecrypt": "js"}}, "1.1.1")
    assert view["ruleContent"]["imageDecode"] == "js"


def test_decode_view_rejects_object_book_source_url_on_111() -> None:
    src = {"bookSourceUrl": {"x": 1}}
    view111, err111 = ap.app_decode_view(src, "1.1.1")
    view112, err112 = ap.app_decode_view(src, "1.1.2")
    assert err111.startswith("app import rejects source")
    assert not err112 and view112["bookSourceUrl"] == '{"x":1}'
    assert ap.app_decode_view({"bookSourceName": "n"}, "1.1.2")[1]


def test_cryptojs_self_define_detection() -> None:
    assert ap.cryptojs_self_defines("var CryptoJS=CryptoJS||function(){}()")
    assert ap.cryptojs_self_defines('root["CryptoJS"] = f')
    assert ap.cryptojs_self_defines("function CryptoJS(){}")
    assert not ap.cryptojs_self_defines("if (CryptoJS == null)")
    assert not ap.cryptojs_self_defines("x => CryptoJS")
    assert not ap.cryptojs_self_defines("no library here")
    assert ap.cryptojs_references("CryptoJS.MD5(x)") and not ap.cryptojs_references("md5(x)")


def test_js_setup_plan() -> None:
    plan111 = ap.app_js_setup_plan("CryptoJS.x", "CryptoJS.y", "1.1.1")
    assert plan111["legacyCryptoJS"] is True
    assert plan111["cryptoJSBeforeJsLib"] is False and plan111["cryptoJSBeforeCode"] is False
    assert plan111["appGlobals"] is True
    assert ap.app_js_setup_plan("", "", "1.1.1")["legacyCryptoJS"] is True

    def plan(js_lib: str, code: str) -> tuple[bool, bool]:
        result = ap.app_js_setup_plan(js_lib, code, "1.1.2")
        assert result["legacyCryptoJS"] is False and result["appGlobals"] is True
        return result["cryptoJSBeforeJsLib"], result["cryptoJSBeforeCode"]

    assert plan("var a = CryptoJS.MD5('x')", "1+1") == (True, False)
    assert plan("var CryptoJS=CryptoJS||function(){}()", "CryptoJS.MD5('x')") == (False, True)
    assert plan("", "") == (False, False)
    assert plan("var CryptoJS=CryptoJS||function(){}()", "var CryptoJS = {}; CryptoJS.x") == (False, False)


def test_builtin_and_legacy_texts_are_pinned() -> None:
    assert _sha(ap.APP_111_CRYPTOJS_POLYFILL) == LEGACY_111_SHA256 == ap.APP_111_CRYPTOJS_POLYFILL_SHA256
    assert _sha(ap.APP_112_CRYPTOJS_BUILTIN) == BUILTIN_112_SHA256 == ap.APP_112_CRYPTOJS_BUILTIN_SHA256
    assert len(ap.APP_111_CRYPTOJS_POLYFILL) == 906 and len(ap.APP_112_CRYPTOJS_BUILTIN) == 22387
    assert "\r" not in ap.APP_111_CRYPTOJS_POLYFILL + ap.APP_112_CRYPTOJS_BUILTIN

    # the file text (what a source guard sees) agrees with the imported constants
    source = (ROOT / "validator" / "app_profiles.py").read_text(encoding="utf-8")
    m112 = re.search(r'APP_112_CRYPTOJS_BUILTIN = r"""\n(.*?)\n"""', source, re.S)
    m111 = re.search(r'APP_111_CRYPTOJS_POLYFILL = r?"""\n(.*?)\n"""', source, re.S)
    assert m112 and m112.group(1) == ap.APP_112_CRYPTOJS_BUILTIN
    assert m111 and m111.group(1) == ap.APP_111_CRYPTOJS_POLYFILL

    swift = REPO_ROOT / "Readori" / "Core" / "Engine" / "JSEngineCryptoJS.swift"
    if swift.is_file():
        match = re.search(
            r'static let builtinSource: String = #"""\n(.*?)\n"""#',
            swift.read_text(encoding="utf-8").replace("\r\n", "\n"),
            re.S,
        )
        assert match is not None
        assert match.group(1) == ap.APP_112_CRYPTOJS_BUILTIN

    # 1.1.1 text is frozen at 2a2239e; recompute it when that commit is reachable
    try:
        shown = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", "2a2239e:Readori/Core/Engine/JSEngine.swift"],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError):
        return
    lines = shown.replace("\r\n", "\n").split("\n")
    start = lines.index('    private let cryptoJSPolyfill = """')
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == '    """')
    text = "\n".join(line[4:] if line.strip() else "" for line in lines[start + 1 : end])
    assert text == ap.APP_111_CRYPTOJS_POLYFILL


def test_python_crypto_natives_match_openssl_vectors() -> None:
    key = _hex("0123456789abcdef")
    iv = _hex("abcdef0123456789")
    padded = _hex("Message") + "09" * 9
    assert len(padded) == 32
    cbc = ap.cryptojs_cipher_hex("e", "AES", "CBC", key, iv, padded)
    assert base64.b64encode(bytes.fromhex(cbc)).decode() == "xjStDeDPpBHzGy+poB0qYA=="
    assert ap.cryptojs_cipher_hex("d", "AES", "CBC", key, iv, cbc) == padded
    ecb = ap.cryptojs_cipher_hex("e", "AES", "ECB", key, "", padded)
    assert base64.b64encode(bytes.fromhex(ecb)).decode() == "QJqzt7G2zKZoYw4+fOUUnA=="

    des_key = _hex("12345678")
    des = ap.cryptojs_cipher_hex("e", "DES", "CBC", des_key, des_key, _hex("Message") + "01")
    assert base64.b64encode(bytes.fromhex(des)).decode() == "dg70b6zDmjU="
    assert ap.cryptojs_cipher_hex("d", "DES", "CBC", des_key, des_key, des) == _hex("Message") + "01"
    assert ap.cryptojs_cipher_hex("e", "DES", "CTR", des_key, des_key, "00" * 8) == "!unsupported mode"
    tdes_key = _hex("012345678901234567890123")
    tdes = ap.cryptojs_cipher_hex("e", "3DES", "ECB", tdes_key, "", "00" * 8)
    assert len(tdes) == 16 and ap.cryptojs_cipher_hex("d", "3DES", "ECB", tdes_key, "", tdes) == "00" * 8

    # stream modes accept unaligned input and round-trip; short IV is zero padded
    for mode in ("CFB", "OFB", "CTR"):
        encrypted = ap.cryptojs_cipher_hex("e", "AES", mode, key, "aa", _hex("Message"))
        assert len(encrypted) == 14
        assert ap.cryptojs_cipher_hex("d", "AES", mode, key, "aa", encrypted) == _hex("Message")

    assert ap.cryptojs_cipher_hex("e", "AES", "CBC", "0102030405", iv, padded) == "!Invalid key length"
    assert ap.cryptojs_cipher_hex("e", "AES", "CBC", key, iv, _hex("Message")) == "!data not block aligned"
    assert ap.cryptojs_cipher_hex("e", "AES", "GCM", key, iv, padded) == "!unsupported mode"
    assert ap.cryptojs_cipher_hex("e", "RC4", "CBC", key, iv, padded) == "!unsupported algorithm"
    assert ap.cryptojs_cipher_hex("e", "AES", "CBC", "zz", iv, padded) == "!invalid hex"
    assert ap.cryptojs_cipher_hex("e", "AES", "CBC", key, iv, "") == ""

    assert ap.cryptojs_digest_hex("MD5", _hex("Message")) == "4c2a8fe7eaf24721cc7a9f0175115bd4"
    assert ap.cryptojs_digest_hex("sha-256", _hex("Message")) == hashlib.sha256(b"Message").hexdigest()
    assert ap.cryptojs_digest_hex("CRC32", "00") == "!unsupported algorithm"
    assert ap.cryptojs_digest_hex("MD5", "0") == "!invalid hex"
    mac = ap.cryptojs_hmac_hex("SHA256", _hex("key"), _hex("Message"))
    assert mac == hmac.new(b"key", b"Message", hashlib.sha256).hexdigest()
    assert mac.startswith("d3c76ce0") and mac.endswith("1b49")

    assert len(ap.cryptojs_random_hex(16)) == 32
    assert ap.cryptojs_random_hex(0) == "" and ap.cryptojs_random_hex(1025) == "!invalid length"
    assert ap.cryptojs_pbkdf2_hex("SHA256", _hex("pw"), _hex("salt"), 2, 20) == hashlib.pbkdf2_hmac(
        "sha256", b"pw", b"salt", 2, 20
    ).hex()
    assert ap.cryptojs_pbkdf2_hex("MD5", _hex("pw"), _hex("salt"), 2, 20) == "!unsupported hasher"
    assert ap.cryptojs_pbkdf2_hex("SHA1", _hex("pw"), _hex("salt"), 2, 0) == "!invalid key size"


def test_app_globals() -> None:
    assert ap.app_btoa("你") == "5L2g"
    assert ap.app_atob("5L2g") == "你"
    assert ap.app_atob("###") == "###"
    assert ap.app_atob("5L2") == "5L2"  # missing padding
    assert ap.app_atob("5L2g\n") == "5L2g\n"  # whitespace is rejected
    assert ap.app_atob("/w==") == "/w=="  # not valid UTF-8
    assert ap.app_atob("") == ""
    assert ap.app_atob("你") == "你"
    assert ap.app_md5("Message") == "4c2a8fe7eaf24721cc7a9f0175115bd4"
    sha = ap.app_sha256("Message")
    assert sha.startswith("2f77668a") and sha.endswith("2f91")


def test_sensitivity() -> None:
    base = {"bookSourceUrl": "https://a.example", "searchUrl": "/s?q={{key}}", "ruleSearch": {"bookList": ".b"}}
    assert ap.app_profile_sensitivity(base) == []
    assert ap.app_profile_sensitivity({**base, "searchUrl": "@js: 'https://a.example/s?q='+key"}) == ["searchUrlJsLiteral"]
    assert ap.app_profile_sensitivity({**base, "searchUrl": "@js: source.getKey()+key"}) == []
    assert ap.app_profile_sensitivity({**base, "jsLib": "var h = CryptoJS.MD5(x)"}) == ["cryptoJS"]
    assert ap.app_profile_sensitivity({**base, "jsLib": "eval(String(java.ajax(u)))"}) == ["cryptoJS"]
    assert ap.app_profile_sensitivity({**base, "jsLib": "java.importScript('https://a.example/x.js')"}) == ["cryptoJS"]
    assert ap.app_profile_sensitivity({**base, "exploreUrl": [{"title": "a", "url": "/c"}]}) == ["jsonStringField"]
    assert ap.app_profile_sensitivity({**base, "header": {"User-Agent": "UA"}}) == []
    assert ap.app_profile_sensitivity({**base, "header": ["x"]}) == ["jsonStringField"]
    assert ap.app_profile_sensitivity({**base, "ruleToc": {"chapterList": ["a"]}}) == ["jsonStringField"]
    content = {**base, "ruleContent": {"content": "<js>java.startBrowserAwait(baseUrl,'v');result</js>"}}
    assert ap.app_profile_sensitivity(content) == ["interaction"]
    assert ap.app_profile_sensitivity({**base, "jsLib": "java.getVerificationCode (u)"}) == ["interaction"]


def test_verdict_and_labels() -> None:
    assert ap.app_verdict({}, True, "1.1.1") == "pass"
    assert ap.app_verdict({"failureReason": "request timeout"}, False, "1.1.2") == "timeout"
    suppressed = {"failureReason": "js error", "suppressedInteractions": ["startBrowserAwait"], "failureBeforeInteraction": False}
    assert ap.app_verdict(suppressed, False, "1.1.1") == "invalid"
    assert ap.app_verdict(suppressed, False, "1.1.2") == "needsInteraction"
    assert ap.app_verdict({**suppressed, "failureBeforeInteraction": True}, False, "1.1.2") == "invalid"
    assert ap.app_verdict({**suppressed, "failureReason": "timeout"}, False, "1.1.2") == "timeout"
    static = {"failureReason": "interactive verification required"}
    code_src = {"ruleContent": {"content": "<js>java.getVerificationCode(u)</js>"}}
    open_src = {"ruleContent": {"content": "<js>java.openUrl(u)</js>"}}
    login_src = {"loginCheckJs": "java.startBrowserAwait(u,'t')"}
    assert ap.app_verdict(static, False, "1.1.2", src=code_src) == "needsInteraction"
    assert ap.app_verdict(static, False, "1.1.2", src=login_src) == "needsInteraction"
    assert ap.app_verdict(static, False, "1.1.2", src=open_src) == "invalid"
    assert ap.app_verdict(static, False, "1.1.1", src=code_src) == "invalid"
    assert ap.app_verdict({**static, "staticInteractionKinds": ["getVerificationCode"]}, False, "1.1.2") == "needsInteraction"
    assert ap.app_verdict({**static, "staticInteractionKinds": ["openUrl"]}, False, "1.1.2") == "invalid"
    assert ap.app_verdict(static, False, "1.1.2") == "invalid"
    assert ap.static_interaction_kinds({**code_src, **login_src}) == ["getVerificationCode", "startBrowserAwait"]
    assert ap.static_interaction_kinds({"ruleToc": {"chapterList": "x"}}) == []
    with ap.app_profile_scope("1.1.2"):
        assert ap.app_verdict(suppressed, False) == "needsInteraction"

    assert ap.compatibility_label("pass", "pass") == "两版均可用"
    assert ap.compatibility_label("invalid", "pass") == "仅 1.1.2 可用"
    assert ap.compatibility_label("pass", "invalid") == "1.1.1 可用、1.1.2 失效"
    assert ap.compatibility_label("invalid", "invalid") == "两版均失效"
    assert ap.compatibility_label("invalid", "needsInteraction") == "1.1.1 失效、1.1.2 需要登录/验证"
    assert ap.compatibility_label("timeout", "pass") == "1.1.1 超时 / 1.1.2 可用"
    assert ap.compatibility_label("pass", "needsInteraction") == "1.1.1 可用 / 1.1.2 需要登录/验证"


def test_pass_tag_and_group_tokens() -> None:
    assert ap.VALIDATED_PASS_TAG == "✅ 完整验证通过（书籍+详情+目录+正文）"
    assert "完整验证通过" in ap.VALIDATED_PASS_TAG
    assert ap.APP_112_ONLY_GROUP_TOKEN == "需1.1.2"
    day = "1018"
    assert ap.pass_group_tokens("1.1.1", True, None, day) == [day]
    assert ap.pass_group_tokens("1.1.2", False, None, day) == [day]
    assert ap.pass_group_tokens("1.1.2", True, None, day) == [day, "需1.1.2"]
    assert ap.pass_group_tokens("1.1.2", True, False, day) == [day, "需1.1.2"]
    assert ap.pass_group_tokens("1.1.2", True, True, day) == [day]
    default_day = ap.pass_group_tokens("1.1.1", False, None)[0]
    assert re.fullmatch(r"\d{4}", default_day)
