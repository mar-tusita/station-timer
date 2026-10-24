import functools
import struct
import threading
import unittest
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def make_tone_wav(frames=800):
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(struct.pack("<h", 0) * frames)
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
        # 音声再生の呼び出しを記録する(unlock用のミュート再生と区別するため muted も保存)
        self.page.add_init_script(
            """
            window.__plays = [];
            const originalPlay = HTMLMediaElement.prototype.play;
            HTMLMediaElement.prototype.play = function () {
                window.__plays.push({ src: this.src, muted: this.muted });
                return originalPlay.apply(this, arguments);
            };
            """
        )
        self.page.goto(self.base_url)

    def tearDown(self):
        self.page.close()

    def route_tone(self, pattern="**/audio/tone.wav", frames=800):
        self.page.route(
            pattern,
            lambda route: route.fulfill(
                status=200,
                content_type="audio/wav",
                body=make_tone_wav(frames),
            ),
        )

    def audible_play_count(self):
        return self.page.evaluate(
            "window.__plays.filter((p) => !p.muted).length"
        )

    def clip_paused(self, name="tone.wav"):
        return self.page.evaluate(
            "(name) => window.app.audio.clips.get(name).paused", name
        )

    def parse_csv(self, text):
        return self.page.evaluate("(t) => CsvLoader.parse(t)", text)

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

    def test_no_audio_warning_when_audio_loads_or_is_blank(self):
        self.page.route(
            "**/audio/tone.wav",
            lambda route: route.fulfill(
                status=200,
                content_type="audio/wav",
                body=make_tone_wav(),
            ),
        )
        self.load_csv(
            "time,message,audio\n"
            "00:00,音声あり,tone.wav\n"
            "00:01,音声なし,\n"
        )
        expect(self.page.locator("#status")).to_contain_text("2件")
        expect(self.page.locator("#errors")).to_be_hidden()
        expect(self.page.locator("#errors")).not_to_contain_text("null")

    def test_audio_event_requests_file_from_audio_folder(self):
        self.page.route(
            "**/audio/tone.wav",
            lambda route: route.fulfill(
                status=200,
                content_type="audio/wav",
                body=make_tone_wav(),
            ),
        )
        with self.page.expect_request("**/audio/tone.wav", timeout=5000):
            self.load_csv("time,message,audio\n00:00,音声イベント,tone.wav")

    def test_audio_plays_from_the_first_event_and_keeps_playing(self):
        self.route_tone(frames=8000 * 3)
        self.load_csv("time,message,audio\n00:00,最初のイベント,tone.wav")
        expect(self.page.locator("#status")).to_contain_text("1件")
        self.start_timer()
        self.page.wait_for_function(
            "window.__plays.some((p) => !p.muted)", timeout=3000
        )
        self.page.wait_for_timeout(700)
        self.assertFalse(self.clip_paused())

    def test_audio_at_a_later_time_plays_once(self):
        self.route_tone(frames=8000 * 3)
        self.load_csv("time,message,audio\n00:00,開始,\n00:01,途中,tone.wav")
        self.start_timer()
        self.assertEqual(self.audible_play_count(), 0)
        self.page.wait_for_function(
            "window.__plays.some((p) => !p.muted)", timeout=3000
        )
        self.page.wait_for_timeout(500)
        self.assertEqual(self.audible_play_count(), 1)
        self.assertFalse(self.clip_paused())

    def test_pause_stops_audio_and_resume_does_not_replay_it(self):
        self.route_tone(frames=8000 * 3)
        self.load_csv("time,message,audio\n00:00,最初のイベント,tone.wav")
        self.start_timer()
        self.page.wait_for_function(
            "window.__plays.some((p) => !p.muted)", timeout=3000
        )
        self.page.get_by_role("button", name="一時停止").click()
        self.assertTrue(self.clip_paused())

        self.page.get_by_role("button", name="再開").click()
        self.page.wait_for_timeout(800)
        self.assertEqual(self.audible_play_count(), 1)

    def test_stop_stops_audio_and_start_plays_it_again(self):
        self.route_tone(frames=8000 * 3)
        self.load_csv("time,message,audio\n00:00,最初のイベント,tone.wav")
        self.start_timer()
        self.page.wait_for_function(
            "window.__plays.some((p) => !p.muted)", timeout=3000
        )
        self.page.get_by_role("button", name="停止", exact=True).click()
        self.assertTrue(self.clip_paused())

        self.start_timer()
        self.page.wait_for_function(
            "window.__plays.filter((p) => !p.muted).length === 2", timeout=3000
        )
        self.page.wait_for_timeout(500)
        self.assertFalse(self.clip_paused())

    def test_audio_file_names_with_spaces_and_japanese_are_loaded(self):
        requested = []

        def handle(route):
            requested.append(route.request.url)
            route.fulfill(
                status=200, content_type="audio/wav", body=make_tone_wav()
            )

        self.page.route("**/audio/**", handle)
        self.load_csv("time,message,audio\n00:00,日本語の音声,ベル 1.wav")
        expect(self.page.locator("#status")).to_contain_text("1件")
        expect(self.page.locator("#errors")).to_be_hidden()
        self.assertTrue(
            any(unquote(url).endswith("/audio/ベル 1.wav") for url in requested)
        )

    def test_blank_message_clears_the_previous_message(self):
        self.load_csv("time,message\n00:00,前のメッセージ\n00:01,")
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("前のメッセージ")
        expect(self.page.locator("#message")).to_have_text("", timeout=3000)

    def test_message_without_duration_stays_until_the_next_event(self):
        for contents in (
            "time,message\n00:00,表示し続ける",
            "time,message,duration\n00:00,表示し続ける,",
        ):
            with self.subTest(contents=contents):
                self.page.reload()
                self.load_csv(contents)
                self.start_timer()
                expect(self.page.locator("#message")).to_have_text("表示し続ける")
                self.page.wait_for_timeout(1500)
                expect(self.page.locator("#message")).to_have_text("表示し続ける")

    def test_blank_candidate_hides_the_previous_candidate(self):
        self.load_csv(
            "time,message,candidate\n"
            "00:00,最初,受験番号 001\n"
            "00:01,次,\n"
        )
        self.start_timer()
        expect(self.page.locator("#candidate")).to_have_text("受験番号 001")
        expect(self.page.locator("#candidate")).to_be_hidden(timeout=3000)

    def test_csv_with_any_invalid_row_is_rejected_as_a_whole(self):
        self.load_csv("time,message\n00:00,正しい行\n不正,誤った行\n00:02,正しい行")
        expect(self.page.get_by_role("alert")).to_be_visible()
        expect(self.page.get_by_role("alert")).to_contain_text("3行目")
        expect(self.page.get_by_role("button", name="開始")).to_be_disabled()

    def test_empty_or_header_only_csv_is_rejected(self):
        for contents in ("", "   \n\n", "time,message\n", "time,message\n\n\n"):
            with self.subTest(contents=contents):
                self.page.reload()
                self.load_csv(contents)
                expect(self.page.get_by_role("alert")).to_be_visible()
                expect(self.page.get_by_role("button", name="開始")).to_be_disabled()

    def test_valid_csv_after_an_invalid_one_clears_the_error(self):
        self.load_csv("time,message\n不正な時刻,イベント")
        expect(self.page.get_by_role("alert")).to_be_visible()
        expect(self.page.get_by_role("button", name="開始")).to_be_disabled()

        self.load_csv("time,message\n00:00,正しいCSV")
        expect(self.page.get_by_role("alert")).to_be_hidden()
        expect(self.page.get_by_role("button", name="開始")).to_be_enabled()
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("正しいCSV")

    def test_missing_audio_is_warned_once_per_file(self):
        self.load_csv(
            "time,message,audio\n"
            "00:00,一つ目,missing-a.wav\n"
            "00:01,二つ目,missing-a.wav\n"
            "00:02,三つ目,missing-b.wav\n"
        )
        alert = self.page.get_by_role("alert")
        expect(alert).to_contain_text("missing-b.wav")
        text = alert.inner_text()
        self.assertEqual(text.count("missing-a.wav"), 1)
        self.assertEqual(text.count("missing-b.wav"), 1)
        self.assertNotIn("null", text)

    def test_time_parsing_accepts_documented_formats(self):
        cases = {
            "00:00": 0,
            "0:00": 0,
            "2:30": 150,
            "02:30": 150,
            "59:59": 3599,
            "00:02:30": 150,
            "1:00:00": 3600,
            "01:59:59": 7199,
            "10:00:00": 36000,
            " 00:05 ": 5,
        }
        for text, expected in cases.items():
            with self.subTest(time=text):
                self.assertEqual(
                    self.page.evaluate("(t) => parseTime(t)", text), expected
                )

    def test_time_parsing_rejects_invalid_formats(self):
        for text in (
            "", "abc", "5", "00", "60:00", "00:60", "1:60:00", "1:00:60",
            "-1:00", "00:-1", "1.5:00", "1:2:3:4", "00:00:", ":30", "００:００",
        ):
            with self.subTest(time=text):
                self.assertIsNone(self.page.evaluate("(t) => parseTime(t)", text))

    def test_time_formatting_uses_hours_minutes_and_seconds(self):
        cases = {0: "00:00:00", 59.9: "00:00:59", 61: "00:01:01",
                 3661: "01:01:01", 36000: "10:00:00", -5: "00:00:00"}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(
                    self.page.evaluate("(s) => formatTime(s)", seconds), expected
                )

    def test_csv_parser_handles_encodings_and_line_endings(self):
        expected_times = [0, 1]
        for name, text in {
            "bom": "\ufefftime,message\n00:00,A\n00:01,B",
            "crlf": "time,message\r\n00:00,A\r\n00:01,B\r\n",
            "cr": "time,message\r00:00,A\r00:01,B",
            "blank lines": "\ntime,message\n\n00:00,A\n   \n00:01,B\n\n",
            "trailing newline": "time,message\n00:00,A\n00:01,B\n",
        }.items():
            with self.subTest(case=name):
                result = self.parse_csv(text)
                self.assertEqual(result["errors"], [])
                self.assertEqual([e["time"] for e in result["events"]], expected_times)
                self.assertEqual([e["message"] for e in result["events"]], ["A", "B"])

    def test_csv_parser_handles_quoting(self):
        result = self.parse_csv(
            'time,message\n00:00,"a,b"\n00:01,"say ""hi"""\n00:02,"1行目\n2行目"\n'
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [e["message"] for e in result["events"]],
            ["a,b", 'say "hi"', "1行目\n2行目"],
        )

    def test_csv_parser_columns_and_optional_values(self):
        result = self.parse_csv(
            "time,message,audio,candidate,duration,memo\n"
            "00:00,全部,a.wav,受験番号 001,2.5,無視される\n"
            "00:01,途中まで,b.wav\n"
            "00:02,空欄,,,\n"
            "00:03\n"
        )
        self.assertEqual(result["errors"], [])
        first, second, third, fourth = result["events"]
        self.assertEqual(
            (first["audio"], first["candidate"], first["duration"]),
            ("a.wav", "受験番号 001", 2.5),
        )
        self.assertEqual(
            (second["audio"], second["candidate"], second["duration"]),
            ("b.wav", "", None),
        )
        self.assertEqual(
            (third["audio"], third["candidate"], third["duration"]),
            ("", "", None),
        )
        self.assertEqual((fourth["message"], fourth["duration"]), ("", None))

    def test_csv_parser_reports_row_errors_with_line_numbers(self):
        cases = {
            "time,message\n,時刻が空欄": "2行目",
            "time,message\n00:00,A,余分な列": "2行目",
            "time,message,duration\n00:00,A,0": "2行目",
            "time,message,duration\n00:00,A,0.0": "2行目",
            "time,message,duration\n00:00,A,-1": "2行目",
            "time,message,duration\n00:00,A,1e3": "2行目",
            "time,message,duration\n00:00,A,abc": "2行目",
            "time,message,duration\n00:00,A,1,2": "2行目",
        }
        for text, line in cases.items():
            with self.subTest(csv=text):
                result = self.parse_csv(text)
                self.assertEqual(len(result["errors"]), 1)
                self.assertIn(line, result["errors"][0])

    def test_csv_parser_keeps_only_the_last_event_for_the_same_time(self):
        result = self.parse_csv(
            "time,message,audio,candidate,duration\n"
            "00:00,最初,a.wav,受験番号 001,5\n"
            "00:05,別の時刻\n"
            "0:00,最後,,,\n"
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [(e["time"], e["message"]) for e in result["events"]],
            [(0, "最後"), (5, "別の時刻")],
        )
        last = result["events"][0]
        self.assertEqual(
            (last["audio"], last["candidate"], last["duration"]), ("", "", None)
        )

    def test_csv_parser_keeps_the_last_of_three_events_at_the_same_time(self):
        result = self.parse_csv(
            "time,message\n01:00,一つ目\n00:00:00,別\n1:00,二つ目\n01:00,三つ目\n"
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [(e["time"], e["message"]) for e in result["events"]],
            [(0, "別"), (60, "三つ目")],
        )

    def test_invalid_row_is_still_an_error_even_if_its_time_is_duplicated(self):
        result = self.parse_csv(
            "time,message,duration\n00:00,A\n00:00,B,abc\n00:00,C\n"
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("3行目", result["errors"][0])

    def test_only_the_last_event_for_the_same_time_runs(self):
        self.route_tone(frames=8000 * 3)
        self.load_csv(
            "time,message,audio,candidate\n"
            "00:00,無視される,tone.wav,無視される候補\n"
            "00:00,実行される,,実行される候補\n"
        )
        expect(self.page.locator("#status")).to_contain_text("1件")
        expect(self.page.locator("#errors")).to_be_hidden()
        self.start_timer()
        expect(self.page.locator("#message")).to_have_text("実行される")
        expect(self.page.locator("#candidate")).to_have_text("実行される候補")
        self.page.wait_for_timeout(700)
        self.assertEqual(self.audible_play_count(), 0)

    def test_audio_of_an_ignored_event_is_not_loaded_or_warned(self):
        self.load_csv(
            "time,message,audio\n"
            "00:00,無視される,missing.wav\n"
            "00:00,実行される,\n"
        )
        expect(self.page.locator("#status")).to_contain_text("1件")
        expect(self.page.locator("#errors")).to_be_hidden()

    def test_csv_parser_reports_every_invalid_row(self):
        result = self.parse_csv("time,message\n不正,A\n00:01,B\n00:60,C\n")
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("2行目", result["errors"][0])
        self.assertIn("4行目", result["errors"][1])

    def test_csv_parser_requires_time_and_message_columns(self):
        for text in ("message\nA", "time\n00:00", "audio,candidate\nx,y", "\n"):
            with self.subTest(csv=text):
                result = self.parse_csv(text)
                self.assertEqual(result["events"], [])
                self.assertEqual(len(result["errors"]), 1)

    def test_csv_parser_sorts_events_by_time(self):
        result = self.parse_csv(
            "time,message\n1:00:00,最後\n00:10,中間\n00:00,最初\n"
        )
        self.assertEqual(
            [(e["time"], e["message"]) for e in result["events"]],
            [(0, "最初"), (10, "中間"), (3600, "最後")],
        )

    def test_fullscreen_can_be_toggled(self):
        self.page.get_by_role("button", name="全画面表示").click()
        self.page.wait_for_function("document.fullscreenElement !== null")
        self.page.get_by_role("button", name="全画面表示").click()
        self.page.wait_for_function("document.fullscreenElement === null")


if __name__ == "__main__":
    unittest.main()
