"""Тесты на локальном HTTP-сервере: интернет не нужен, результат предсказуем."""

import contextlib
import io
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import speedtest

FILE_SIZE = 256 * 1024
BODY = b"x" * FILE_SIZE


class Handler(BaseHTTPRequestHandler):
    """Маршруты, на которых проверяем разные ответы сервера."""

    # общие для всех запросов счётчики, их сбрасывает setUp
    lock = threading.Lock()
    hits = 0
    active = 0
    max_active = 0
    last_headers = None

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.hits += 1
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
            cls.last_headers = dict(self.headers)
        try:
            self.route()
        finally:
            with cls.lock:
                cls.active -= 1

    def route(self):
        if self.path == "/file":
            time.sleep(0.01)  # чтобы параллельные запросы, будь они, успели пересечься
            self.send_body(BODY)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/no-length":
            # без Content-Length: конец ответа = закрытие соединения
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(BODY)
            self.close_connection = True
        elif self.path == "/truncated":
            # обещаем 1000 байт, отдаём 100 и рвём соединение
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 100)
            self.close_connection = True
        elif self.path == "/empty":
            self.send_body(b"")
        elif self.path == "/slow":
            time.sleep(1)
            self.send_body(b"late")
        else:
            self.send_error(404, "Not Found")

    def send_body(self, body):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # не засоряем вывод тестов


class LocalServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.hits = Handler.active = Handler.max_active = 0
        Handler.last_headers = None

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = speedtest.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class FetchTest(LocalServerTest):
    def test_reads_whole_body(self):
        size, elapsed = speedtest.fetch(self.base + "/file", timeout=5)
        self.assertEqual(size, FILE_SIZE)
        self.assertGreater(elapsed, 0)

    def test_asks_caches_for_fresh_copy(self):
        speedtest.fetch(self.base + "/file", timeout=5)
        self.assertEqual(Handler.last_headers.get("Cache-Control"), "no-cache")
        self.assertEqual(Handler.last_headers.get("Accept-Encoding"), "identity")

    def test_body_without_content_length(self):
        size, _ = speedtest.fetch(self.base + "/no-length", timeout=5)
        self.assertEqual(size, FILE_SIZE)

    def test_follows_redirect(self):
        size, _ = speedtest.fetch(self.base + "/redirect", timeout=5)
        self.assertEqual(size, FILE_SIZE)

    def test_truncated_response_is_an_error(self):
        with self.assertRaisesRegex(speedtest.SpeedTestError, "получено 100 из 1000"):
            speedtest.fetch(self.base + "/truncated", timeout=5)


class MainTest(LocalServerTest):
    def test_makes_n_sequential_requests(self):
        code, out, _ = self.run_main(self.base + "/file", "-n", "5")
        self.assertEqual(code, 0)
        self.assertEqual(Handler.hits, 5)
        self.assertEqual(Handler.max_active, 1)  # ни разу не было двух запросов сразу
        self.assertIn("[5/5]", out)
        self.assertIn("Среднее время запроса", out)
        self.assertIn("1.31 МБ (1 310 720 байт)", out)  # 5 × 256 КиБ
        self.assertIn("Мбит/с", out)

    def test_default_is_ten_requests(self):
        code, _, _ = self.run_main(self.base + "/file")
        self.assertEqual(code, 0)
        self.assertEqual(Handler.hits, 10)

    def test_warns_about_small_file(self):
        _, _, err = self.run_main(self.base + "/file", "-n", "1")
        self.assertIn("меньше 1 МБ", err)

    def test_warns_about_empty_response(self):
        code, _, err = self.run_main(self.base + "/empty", "-n", "2")
        self.assertEqual(code, 0)
        self.assertIn("пустые ответы", err)

    def test_http_error_stops_the_run(self):
        code, _, err = self.run_main(self.base + "/missing")
        self.assertEqual(code, 1)
        self.assertEqual(Handler.hits, 1)
        self.assertIn("Ошибка на запросе 1/10", err)
        self.assertIn("HTTP 404", err)

    def test_timeout(self):
        code, _, err = self.run_main(self.base + "/slow", "-n", "1", "-t", "0.2")
        self.assertEqual(code, 1)
        self.assertIn("время ожидания", err)

    def test_connection_refused(self):
        code, _, err = self.run_main("http://127.0.0.1:1/file", "-n", "1")
        self.assertEqual(code, 1)
        self.assertIn("не удалось подключиться", err)

    def test_bad_url(self):
        code, _, err = self.run_main("ftp://example.com/file")
        self.assertEqual(code, 2)
        self.assertIn("некорректный адрес", err)

    def test_bad_arguments(self):
        for argv in (["x", "-n", "0"], ["x", "-n", "abc"], ["x", "-t", "-1"], []):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                speedtest.main(argv)
            self.assertEqual(ctx.exception.code, 2)


class PureFunctionsTest(unittest.TestCase):
    def test_summarize(self):
        s = speedtest.summarize([(1_000_000, 1.0), (3_000_000, 1.0), (2_000_000, 2.0)])
        self.assertEqual(s["requests"], 3)
        self.assertEqual(s["total_bytes"], 6_000_000)
        self.assertAlmostEqual(s["total_time"], 4.0)
        self.assertAlmostEqual(s["avg_time"], 4.0 / 3)
        self.assertAlmostEqual(s["min_time"], 1.0)
        self.assertAlmostEqual(s["max_time"], 2.0)
        # 6 МБ за 4 с = 1.5 МБ/с = 12 Мбит/с
        self.assertAlmostEqual(s["mbyte_per_sec"], 1.5)
        self.assertAlmostEqual(s["mbit_per_sec"], 12.0)

    def test_format_size(self):
        self.assertEqual(speedtest.format_size(0), "0 Б")
        self.assertEqual(speedtest.format_size(999), "999 Б")
        self.assertEqual(speedtest.format_size(1500), "1.50 КБ")
        self.assertEqual(speedtest.format_size(21_000_000), "21.00 МБ")
        self.assertEqual(speedtest.format_size(3_200_000_000), "3.20 ГБ")

    def test_normalize_url(self):
        self.assertEqual(speedtest.normalize_url("example.com/a.jpg"), "https://example.com/a.jpg")
        self.assertEqual(speedtest.normalize_url(" http://x.ru/a "), "http://x.ru/a")
        for bad in ("ftp://example.com/a", "https://", "http://[::1"):
            with self.subTest(url=bad), self.assertRaises(speedtest.SpeedTestError):
                speedtest.normalize_url(bad)


if __name__ == "__main__":
    unittest.main()
