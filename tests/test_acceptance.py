import functools
import struct
import threading
import unittest
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def make_tone_wav():
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(struct.pack("<h", 0) * 800)
    return output.getvalue()


class AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(QuietHandler, directory=str(ROOT)),
        )
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.base_url)

    def tearDown(self):
        self.page.close()

    def load_csv(self, contents):
        self.page.locator("#csvFile").set_input_files(
            {
                "name": "schedule.csv",
                "mimeType": "text/csv",
                "buffer": contents.encode("utf-8"),
            }
        )

    def start_timer(self):
        self.page.get_by_role("button", name="開始").click()

    def test_headers_ignore_case_and_surrounding_whitespace(self):
        self.load_csv(" TiMe , MeSsAgE \n00:00,読み込み成功")
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("読み込み成功")

    def test_optional_columns_can_be_reordered(self):
        self.load_csv(
            "message,duration,time,candidate\n並び替え,0.3,00:00,追加情報"
        )
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("並び替え")
        expect(self.page.locator("#candidate")).to_have_text("追加情報")
        expect(self.page.locator("#message")).to_have_text("", timeout=2000)
        expect(self.page.locator("#candidate")).to_be_visible()

    def test_zero_time_and_unsorted_events_run_in_time_order(self):
        self.load_csv("time,message\n00:02,後のイベント\n00:00,開始イベント")
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("開始イベント")
        expect(self.page.locator("#message")).to_have_text(
            "後のイベント", timeout=4000
        )

    def test_invalid_csv_is_rejected(self):
        invalid_csvs = (
            "time\n00:00,メッセージ列がない",
            "time,message\n不正な時刻,イベント",
            "time,message\n00:60,範囲外の秒",
            "time,message\n00:01,最初\n00:01,重複",
            "time,message,duration\n00:00,不正なduration,0",
            "time,message,duration\n00:00,負のduration,-1",
            "time,message,duration\n00:00,文字のduration,abc",
        )
        for contents in invalid_csvs:
            with self.subTest(contents=contents):
                self.page.reload()
                self.load_csv(contents)
                expect(self.page.get_by_role("alert")).to_be_visible()
                expect(self.page.get_by_role("button", name="開始")).to_be_disabled()

    def test_quoted_commas_duration_and_candidate_lifetime(self):
        self.load_csv(
            "time,message,audio,candidate,duration\n"
            '00:00,"前半,終了です",,受験番号 001,0.3\n'
            "00:01,次のイベント,,,\n"
        )
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("前半,終了です")
        expect(self.page.locator("#candidate")).to_have_text("受験番号 001")
        expect(self.page.locator("#message")).to_have_text("", timeout=2000)
        expect(self.page.locator("#candidate")).to_be_visible()
        expect(self.page.locator("#message")).to_have_text(
            "次のイベント", timeout=3000
        )
        expect(self.page.locator("#candidate")).to_be_hidden()

    def test_pause_freezes_elapsed_time_and_resume_continues(self):
        self.load_csv("time,message\n00:00,開始\n00:02,再開後")
        self.start_timer()
        expect(self.page.locator("#timer")).not_to_have_text(
            "00:00:00", timeout=2500
        )
        self.page.get_by_role("button", name="一時停止").click()
        paused_time = self.page.locator("#timer").inner_text()
        self.page.wait_for_timeout(1200)
        expect(self.page.locator("#timer")).to_have_text(paused_time)

        self.page.get_by_role("button", name="再開").click()
        expect(self.page.locator("#message")).to_have_text("再開後", timeout=3500)

    def test_message_duration_does_not_advance_while_paused(self):
        self.load_csv("time,message,duration\n00:00,一時表示,0.5")
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("一時表示")
        self.page.wait_for_timeout(150)
        self.page.get_by_role("button", name="一時停止").click()
        self.page.wait_for_timeout(800)
        expect(self.page.locator("#message")).to_have_text("一時表示")

        self.page.get_by_role("button", name="再開").click()
        expect(self.page.locator("#message")).to_have_text("", timeout=2000)

    def test_stop_resets_timer_and_last_event_does_not_stop_it(self):
        self.load_csv("time,message\n00:00,最初のイベント")
        self.start_timer()
        expect(self.page.locator("#timer")).to_have_text("00:00:01", timeout=3000)
        self.page.wait_for_timeout(1100)
        expect(self.page.locator("#timer")).to_have_text("00:00:02", timeout=2500)

        self.page.get_by_role("button", name="停止", exact=True).click()
        expect(self.page.locator("#timer")).to_have_text("00:00:00")
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("最初のイベント")

    def test_csv_loading_is_only_available_while_stopped(self):
        self.load_csv("time,message\n00:00,開始")
        self.start_timer()
        expect(self.page.get_by_role("button", name="CSV読込")).to_be_disabled()

        self.page.get_by_role("button", name="停止", exact=True).click()
        expect(self.page.get_by_role("button", name="CSV読込")).to_be_enabled()

    def test_missing_audio_warns_but_keeps_timer_usable(self):
        self.load_csv(
            "time,message,audio\n00:00,音声なしでも表示,not-found.wav"
        )
        expect(self.page.get_by_text("not-found.wav")).to_be_visible()
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("音声なしでも表示")

    def test_audio_event_requests_file_from_audio_folder(self):
        self.page.route(
            "**/audio/tone.wav",
            lambda route: route.fulfill(
                status=200,
                content_type="audio/wav",
                body=make_tone_wav(),
            ),
        )
        self.load_csv("time,message,audio\n00:00,音声イベント,tone.wav")
        with self.page.expect_request("**/audio/tone.wav", timeout=5000):
            self.start_timer()

    def test_fullscreen_can_be_toggled(self):
        self.page.get_by_role("button", name="全画面表示").click()
        self.page.wait_for_function("document.fullscreenElement !== null")
        self.page.get_by_role("button", name="全画面表示").click()
        self.page.wait_for_function("document.fullscreenElement === null")


if __name__ == "__main__":
    unittest.main()
