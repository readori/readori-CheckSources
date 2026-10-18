"""Local fixture website + matching book sources for the R146 workbench tests (offline, 127.0.0.1 only).

``start()`` returns ``(base, base_alt, stop)``: ``base`` is ``http://127.0.0.1:<port>``, ``base_alt`` the same
server reached as ``http://localhost:<port>`` (a *different host*, used to simulate a domain migration).
``sources(base)`` builds the matching book sources: a working one plus one source per fault.

Tests that talk to the site must set ``READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1`` (the SSRF guard otherwise
refuses loopback targets).

Routes (``<p>`` is an optional path prefix: ``/site2`` after a migration redirect, ``/ua`` for the UA-checking mirror)::

    /search?kw=           UTF-8 result list                 /book/1               detail page
    /gbk/search?kw=       GBK page, only GBK-encoded kw     /book/1/toc[?p=2]     table of contents (2 pages)
    /book/1/c1            chapter text (with ad lines)      /api/search?kw=       JSON result list
    /old/<path>           301 -> <alt host>/site2/<path>      /ua/<path>            403 unless the UA contains Mozilla
    /cf                   503 + Cloudflare "Just a moment"  /lazy/search?kw=      covers lazy-loaded (data-original)
    /slow/<path>          like <path>, 0.7 s late           /ref/<path>           403 for everything but /search unless a Referer is sent
"""

from __future__ import annotations

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote_to_bytes, urlsplit

REQUEST_LOG: list[dict[str, str]] = []  # one entry per request: method, path, ua
SLOW_SECONDS = 0.7  # delay of every request below /slow/

BOOKS = [("我的老婆是女帝", "天命书生"), ("夜无疆", "远方客")]
CHAPTERS_P1 = [("第一章 重生", "/book/1/c1"), ("第二章 起风", "/book/1/c2"), ("第三章 入局", "/book/1/c3")]
CHAPTERS_P2 = [("第四章 破局", "/book/1/c4"), ("第五章 余波", "/book/1/c5")]
CONTENT = (
    "他睁开眼睛，发现自己躺在一间陌生的屋子里，窗外的风声很轻，像是有人在低声说话。"
    "桌上的油灯还亮着，火光摇晃，把墙上的影子拉得很长。他试着坐起身，胸口传来一阵隐隐的闷痛。"
    "这是哪里？他在心里问自己，却没有人回答。门外传来脚步声，由远及近，在门前停了下来。"
)
AD_LINES = ["天才一秒记住本站地址：www.fixture-site.com", "本章未完，点击下一页继续阅读。", "请记住本书首发域名：m.fixture-site.com"]


def _page(title: str, body: str, charset: str = "utf-8") -> str:
    return (f'<!DOCTYPE html><html><head><meta charset="{charset}"><title>{title}</title></head>'
            f"<body>{body}</body></html>")


def _search_html(prefix: str, *, charset: str = "utf-8", lazy: bool = False) -> str:
    rows = []
    for index, (name, author) in enumerate(BOOKS, start=1):
        cover = (f'<img src="/static/loading.gif" data-original="/static/cover{index}.jpg">' if lazy
                 else f'<img src="/static/cover{index}.jpg">')
        rows.append(f'<li><h3>{name}</h3><a href="{prefix}/book/{index}">详情</a>'
                    f'<span class="author">{author}</span>{cover}</li>')
    return _page("搜索结果", f'<ul class="result-list">{"".join(rows)}</ul>', charset)


def _detail_html(prefix: str, number: int = 1) -> str:
    name, author = BOOKS[number - 1]
    return _page(name, f'<h1>{name}</h1><span class="author">{author}</span>'
                       f'<img src="/static/loading.gif" data-original="/static/cover1.jpg">'
                       f'<p class="intro">一个关于重生与权谋的故事。</p>'
                       f'<a class="toc" href="{prefix}/book/1/toc">章节目录</a>')


def _toc_html(prefix: str, page: int) -> str:
    chapters = CHAPTERS_P1 if page == 1 else CHAPTERS_P2
    rows = "".join(f'<li><a href="{prefix}{href}">{title}</a></li>' for title, href in chapters)
    nxt = f'<a class="next" href="{prefix}/book/1/toc?p=2">下一页</a>' if page == 1 else ""
    return _page("目录", f'<ul id="list">{rows}</ul>{nxt}')


def _content_html(prefix: str) -> str:
    ads = "".join(f"<p>{line}</p>" for line in AD_LINES)
    return _page("第一章 重生", f'<h1>第一章 重生</h1><div id="content"><p>{CONTENT}</p>{ads}</div>')


def _first_param(query: str, name: str) -> bytes | None:
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == name:
            return unquote_to_bytes(value.replace("+", " "))
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "FixtureSite/1.0"

    def log_message(self, *args: Any) -> None:  # silence
        return

    # -- helpers -------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _alt_origin(self) -> str:
        host = self.headers.get("Host", "")
        port = host.rsplit(":", 1)[-1] if ":" in host else str(self.server.server_address[1])
        return f"http://localhost:{port}" if host.startswith("127.0.0.1") else f"http://127.0.0.1:{port}"

    # -- routing -------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path, query = parts.path, parts.query
        REQUEST_LOG.append({"method": "GET", "path": self.path, "ua": self.headers.get("User-Agent", "")})
        if path.startswith("/old/"):
            target = f"{self._alt_origin()}/site2/{path[len('/old/'):]}" + (f"?{query}" if query else "")
            self._send(301, b"", headers={"Location": target})
            return
        prefix = ""
        for candidate in ("/site2", "/ua", "/slow", "/ref"):
            if path.startswith(candidate + "/"):
                prefix, path = candidate, path[len(candidate):]
                break
        if prefix == "/slow":
            time.sleep(SLOW_SECONDS)
        if prefix == "/ua" and "mozilla" not in self.headers.get("User-Agent", "").lower():
            self._send(403, _page("403", "<h1>Forbidden</h1><p>Bad client.</p>").encode("utf-8"))
            return
        if prefix == "/ref" and path != "/search" and not self.headers.get("Referer"):
            self._send(403, _page("403", "<h1>Forbidden</h1><p>Hotlinking is not allowed.</p>").encode("utf-8"))
            return
        self._route(prefix, path, query)

    def _route(self, prefix: str, path: str, query: str) -> None:
        if path == "/cf":
            body = _page("Just a moment...", '<div id="cf-challenge-running">Just a moment...</div>'
                                              '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>')
            self._send(503, body.encode("utf-8"), headers={"Server": "cloudflare", "CF-RAY": "8a1b2c3d4e5f-LAX"})
            return
        if path == "/search":
            keyword = _first_param(query, "kw") or b""
            if keyword == b"__none__":
                self._send(200, _page("搜索结果", '<ul class="result-list"></ul>').encode("utf-8"))
                return
            self._send(200, _search_html(prefix).encode("utf-8"))
            return
        if path == "/lazy/search":
            self._send(200, _search_html(prefix, lazy=True).encode("utf-8"))
            return
        if path == "/gbk/search":
            keyword = _first_param(query, "kw")
            if keyword == "我的".encode("gbk"):
                html = _search_html(prefix, charset="gbk")
            else:
                html = _page("搜索结果", '<ul class="result-list"></ul><p>没有找到相关书籍</p>', "gbk")
            self._send(200, html.encode("gbk", errors="replace"), "text/html")
            return
        if path == "/api/search":
            items = [{"name": name, "url": f"{prefix}/book/{index}", "author": author}
                     for index, (name, author) in enumerate(BOOKS, start=1)]
            self._send(200, json.dumps({"data": {"items": items}}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path in ("/book/1", "/book/2"):
            self._send(200, _detail_html(prefix, int(path[-1])).encode("utf-8"))
            return
        if path == "/book/1/toc":
            page = 2 if _first_param(query, "p") == b"2" else 1
            self._send(200, _toc_html(prefix, page).encode("utf-8"))
            return
        if path.startswith("/book/1/c"):
            self._send(200, _content_html(prefix).encode("utf-8"))
            return
        if path.startswith("/static/"):
            self._send(200, b"\x89PNG", "image/png")
            return
        if path == "/":
            self._send(200, _page("fixture", "<h1>fixture site</h1>").encode("utf-8"))
            return
        self._send(404, _page("404", "<h1>Not Found</h1>").encode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        REQUEST_LOG.append({"method": "POST", "path": self.path, "ua": self.headers.get("User-Agent", "")})
        parts = urlsplit(self.path)
        if parts.path == "/search":
            self._send(200, _search_html("").encode("utf-8"))
            return
        self._send(200, data or b"{}", "application/json")


def start() -> tuple[str, str, Callable[[], None]]:
    """Start the site on a free port; returns ``(base, base_alt, stop)``."""
    REQUEST_LOG.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    servers = [server]
    # `localhost` resolves to ::1 first on many machines; without an IPv6 listener every request to `base_alt`
    # waits for the connect fallback (~2 s).  Best effort: serve the same port on ::1 as well.
    try:
        import socket

        class _Server6(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server6 = _Server6(("::1", port), _Handler)
        server6.daemon_threads = True
        threading.Thread(target=server6.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(server6)
    except OSError:
        pass

    def stop() -> None:
        for item in servers:
            item.shutdown()
            item.server_close()
        thread.join(timeout=3)
        # Leave no trace in the validator's per-thread state (a recorded network error or a Session built while the
        # private-network switch was on would leak into the next test file).
        try:
            from validator import validate_source_packages as vsp

            vsp.begin_network_stage()
            getattr(vsp._thread_local, "__dict__", {}).pop("session", None)
        except Exception:  # noqa: BLE001
            pass

    return f"http://127.0.0.1:{port}", f"http://localhost:{port}", stop


# ---------------------------------------------------------------------------
# Book sources
# ---------------------------------------------------------------------------

def good_source(base: str) -> dict[str, Any]:
    """A fully working source (search -> detail -> two-page TOC -> content)."""
    return {
        "bookSourceUrl": base,
        "bookSourceName": "夹具站",
        "bookSourceType": 0,
        "enabled": True,
        "searchUrl": "/search?kw={{key}}",
        "ruleSearch": {
            "checkKeyWord": "我的",
            "bookList": "class.result-list@tag.li",
            "name": "tag.h3@text",
            "author": "class.author@text",
            "bookUrl": "tag.a@href",
            "coverUrl": "tag.img@src",
        },
        "ruleBookInfo": {
            "name": "tag.h1@text",
            "author": "class.author@text",
            "intro": "class.intro@text",
            "coverUrl": "tag.img@data-original",
            "tocUrl": "class.toc@href",
        },
        "ruleToc": {
            "chapterList": "id.list@tag.li",
            "chapterName": "tag.a@text",
            "chapterUrl": "tag.a@href",
            "nextTocUrl": "class.next@href",
        },
        "ruleContent": {"content": "id.content@html"},
    }


def sources(base: str, base_alt: str | None = None) -> dict[str, dict[str, Any]]:
    """The working source (``good``) plus one source per fault.  Fault names:

    ``search_broken`` bookList matches nothing; ``toc_broken`` chapterList matches nothing; ``content_broken``
    content matches nothing; ``moved`` search goes through ``/old/`` (301 to another host); ``gbk_missing_charset`` /
    ``gbk_ok`` GBK search without / with ``charset``; ``ua_missing`` a (lower-case keyed) UA the ``/ua`` mirror refuses - no header edit can fix it; ``referer_missing`` detail / toc /
    content pages answer 403 unless the request carries a Referer; ``cloudflare``
    search answers with a Cloudflare challenge; ``lazy_cover`` covers use ``src=loading.gif``; ``api`` JSON search; ``slow`` every request answers 0.7 s late;
    ``no_next_toc`` the TOC has a pager the source does not follow; ``disabled`` a disabled source.
    """
    good = good_source(base)
    out: dict[str, dict[str, Any]] = {"good": good}

    def variant(name: str, mutate: Callable[[dict[str, Any]], None], title: str | None = None) -> None:
        src = copy.deepcopy(good)
        src["bookSourceName"] = title or f"夹具站·{name}"
        mutate(src)
        out[name] = src

    variant("search_broken", lambda s: s["ruleSearch"].update(bookList="class.no-such-list@tag.li"))
    variant("toc_broken", lambda s: s["ruleToc"].update(chapterList="id.no-such-toc@tag.li"))
    variant("content_broken", lambda s: s["ruleContent"].update(content="id.no-such-content@html"))
    variant("moved", lambda s: s.update(searchUrl="/old/search?kw={{key}}"))
    variant("gbk_missing_charset", lambda s: s.update(searchUrl="/gbk/search?kw={{key}}"))
    variant("gbk_ok", lambda s: s.update(searchUrl='/gbk/search?kw={{key}},{"charset":"gbk"}'))
    variant("ua_missing", lambda s: s.update(
        searchUrl="/ua/search?kw={{key}}",
        header='{"user-agent":"BareBot/1.0"}',
        ruleSearch={**s["ruleSearch"]},
    ))
    variant("slow", lambda s: s.update(searchUrl="/slow/search?kw={{key}}"))
    variant("referer_missing", lambda s: (s.update(searchUrl="/ref/search?kw={{key}}"), s.pop("header", None)))
    variant("cloudflare", lambda s: s.update(searchUrl="/cf?kw={{key}}"))
    variant("lazy_cover", lambda s: s.update(searchUrl="/lazy/search?kw={{key}}"))
    variant("api", lambda s: s.update(
        searchUrl="/api/search?kw={{key}}",
        ruleSearch={"checkKeyWord": "我的", "bookList": "$.data.items[*]", "name": "$.name", "author": "$.author",
                    "bookUrl": "$.url"},
    ))
    variant("no_next_toc", lambda s: s["ruleToc"].pop("nextTocUrl"))
    variant("disabled", lambda s: s.update(enabled=False))
    return out
