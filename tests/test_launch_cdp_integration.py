"""ブラウザの起動（普通に起動して CDP で接続）の実環境テスト.

人が操作するログイン用のブラウザが、サイトから自動操作に見えないこと（navigator.webdriver が false）を確かめる。
Playwright の launch で起動すると true になり、TVer のログインが reCAPTCHA で拒まれた（Mac と配布のコンテナで実測）。
実 Xvfb・Fluxbox・Playwright の Chromium・FFmpeg が揃った環境（本番と同じ Dockerfile 由来）でだけ実行する。
"""

import asyncio
import http.server
import os
import shutil
import threading

import pytest

import web_screen_stream.session as sess
from web_screen_stream.config import StreamConfig
from web_screen_stream.session import SessionManager
from web_screen_stream.xvfb import XvfbManager


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return os.path.exists(pw.chromium.executable_path)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("fluxbox") and shutil.which("ffmpeg") and _chromium_installed()),
    reason="Xvfb・Fluxbox・FFmpeg・Playwright の Chromium が揃った環境でだけ実行する",
)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"


@pytest.fixture
def server():
    """受け取った User-Agent ヘッダを記録し、ポップアップを開くページを返す."""
    seen: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path, "ua": self.headers.get("User-Agent"),
                         "platform": self.headers.get("Sec-CH-UA-Platform")})
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<a id=pop href='/popup' target=_blank>popup</a>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


async def test_launched_browser_does_not_look_automated_and_is_cleaned_up(server):
    url, seen = server
    mgr = SessionManager(xvfb_manager=XvfbManager(base_display=260, max_displays=1))
    await mgr.create("s", StreamConfig(width=800, height=600, user_agent=_UA), url=url + "/")
    try:
        tag = mgr._browser_tags["s"]
        _pw, browser, page = mgr._browsers["s"]
        assert await page.evaluate("navigator.webdriver") is False
        assert await page.evaluate("navigator.userAgent") == _UA
        assert await page.evaluate("[innerWidth, innerHeight]") == [800, 600]
        async with page.context.expect_page() as popup_info:
            await page.click("#pop")
        popup = await popup_info.value
        await popup.wait_for_load_state()
        assert await popup.evaluate("navigator.webdriver") is False
        pages = [s for s in seen if s["path"] in ("/", "/popup")]  # favicon などは除く
        assert [s["ua"] for s in pages] == [_UA, _UA]  # ポップアップも同じ User-Agent
        print("Sec-CH-UA-Platform:", [s["platform"] for s in pages])
        # ユーザー操作から時間が経った（一時的な操作の権限が切れた）後の window.open もブロックしない
        async with page.context.expect_page(timeout=15000) as late_info:
            await page.evaluate("setTimeout(() => window.open('/late'), 6000)")
        await (await late_info.value).wait_for_load_state()
        assert sess._tagged_pids(tag)
    finally:
        await mgr.stop("s")
    await asyncio.sleep(0.5)
    assert not sess._tagged_pids(tag)
    assert not os.path.exists(sess._profile_dir(tag))
