#!/usr/bin/env python3
"""Замер скорости скачивания.

Делает N последовательных GET-запросов к указанному адресу, каждый раз
дочитывает ответ до конца и печатает среднее время запроса, общий объём
скачанных данных и скорость скачивания в Мбит/с.

Только стандартная библиотека Python, ставить ничего не нужно.
"""

import argparse
import http.client
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30.0  # сколько секунд ждать данных от сервера
CHUNK_SIZE = 64 * 1024  # читаем ответ кусками, чтобы не держать весь файл в памяти
SMALL_FILE_BYTES = 1_000_000  # на файлах меньше 1 МБ замер сильно искажается
USER_AGENT = "download-speed-test/1.0"
SIZE_UNITS = ("Б", "КБ", "МБ", "ГБ", "ТБ")


class SpeedTestError(Exception):
    """Ошибка, которую показываем пользователю текстом, без traceback."""


def normalize_url(raw):
    """Добавляет https://, если схема не указана, и проверяет адрес."""
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:  # например, незакрытая скобка в IPv6-адресе
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise SpeedTestError(f"некорректный адрес {raw!r}: нужен http:// или https:// адрес")
    return url


def fetch(url, timeout):
    """Скачивает url до конца. Возвращает (байт в теле ответа, секунд)."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        # просим CDN и прокси не отдавать сохранённую копию
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        # без сжатия: меряем настоящий размер файла
        "Accept-Encoding": "identity",
    })

    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        expected = response.headers.get("Content-Length")
        size = 0
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:  # пустой кусок = сервер отдал всё
                break
            size += len(chunk)
    elapsed = time.perf_counter() - start

    # urllib молча принимает оборванный ответ, поэтому сверяем размер сами
    if expected is not None and expected.isdigit() and size != int(expected):
        raise SpeedTestError(f"ответ оборван: получено {size} из {expected} байт")
    return size, elapsed


def summarize(results):
    """Считает итоги по списку пар (байты, секунды)."""
    times = [elapsed for _, elapsed in results]
    total_bytes = sum(size for size, _ in results)
    total_time = sum(times)
    # Скорость = весь объём / всё время. Среднее от скоростей отдельных
    # запросов не берём: короткий запрос вносил бы в него столько же веса,
    # сколько длинный.
    bytes_per_sec = total_bytes / total_time if total_time > 0 else 0.0
    return {
        "requests": len(results),
        "total_bytes": total_bytes,
        "total_time": total_time,
        "avg_time": total_time / len(results),
        "min_time": min(times),
        "max_time": max(times),
        "mbit_per_sec": bytes_per_sec * 8 / 1_000_000,
        "mbyte_per_sec": bytes_per_sec / 1_000_000,
    }


def format_size(num_bytes):
    """Размер в десятичных единицах (1 МБ = 1 000 000 байт), как у провайдеров."""
    value = float(num_bytes)
    unit = 0
    while value >= 1000 and unit < len(SIZE_UNITS) - 1:
        value /= 1000
        unit += 1
    if unit == 0:
        return f"{int(value)} {SIZE_UNITS[0]}"
    return f"{value:.2f} {SIZE_UNITS[unit]}"


def mbit_per_sec(num_bytes, seconds):
    return num_bytes * 8 / 1_000_000 / seconds if seconds > 0 else 0.0


def describe_error(exc):
    """Превращает исключение сети или HTTP в понятный текст."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"сервер ответил HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, socket.timeout):
            return "превышено время ожидания ответа"
        return f"не удалось подключиться: {exc.reason}"
    if isinstance(exc, socket.timeout):  # в Python 3.10+ это TimeoutError
        return "превышено время ожидания данных от сервера"
    return str(exc) or type(exc).__name__


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидалось целое число, получено {text!r}")
    if value < 1:
        raise argparse.ArgumentTypeError("число должно быть не меньше 1")
    return value


def positive_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидалось число, получено {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError("число должно быть больше 0")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Замер скорости скачивания: N последовательных запросов к одному адресу.",
    )
    parser.add_argument("url", help="адрес файла, например тяжёлой картинки")
    parser.add_argument(
        "-n", "--requests", type=positive_int, default=DEFAULT_REQUESTS,
        help="сколько запросов сделать (по умолчанию %(default)s)",
    )
    parser.add_argument(
        "-t", "--timeout", type=positive_float, default=DEFAULT_TIMEOUT,
        help="сколько секунд ждать данных от сервера (по умолчанию %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        url = normalize_url(args.url)
    except SpeedTestError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    count = args.requests
    width = len(str(count))
    print(f"Адрес:    {url}")
    print(f"Запросов: {count}, по очереди\n", flush=True)

    results = []
    for i in range(1, count + 1):
        # номер запроса печатаем сразу: на медленной сети видно, что работа идёт
        print(f"[{i:>{width}}/{count}] ", end="", flush=True)
        try:
            size, elapsed = fetch(url, args.timeout)
        except (OSError, http.client.HTTPException, ValueError, SpeedTestError) as exc:
            print("ошибка", flush=True)
            print(f"Ошибка на запросе {i}/{count}: {describe_error(exc)}", file=sys.stderr)
            return 1
        results.append((size, elapsed))
        print(
            f"{format_size(size):>10}  за {elapsed:7.3f} с"
            f"  ({mbit_per_sec(size, elapsed):.2f} Мбит/с)",
            flush=True,
        )

    s = summarize(results)
    print()
    print(f"Среднее время запроса: {s['avg_time']:.3f} с"
          f" (мин {s['min_time']:.3f} с, макс {s['max_time']:.3f} с)")
    exact_bytes = f"{s['total_bytes']:,}".replace(",", " ")  # 21 000 000
    print(f"Всего скачано:         {format_size(s['total_bytes'])} ({exact_bytes} байт)")
    print(f"Общее время:           {s['total_time']:.3f} с")
    print(f"Скорость скачивания:   {s['mbit_per_sec']:.2f} Мбит/с ({s['mbyte_per_sec']:.2f} МБ/с)",
          flush=True)

    if s["total_bytes"] == 0:
        print("\nВнимание: сервер вернул пустые ответы, скорость посчитать не по чему.",
              file=sys.stderr)
    elif s["total_bytes"] / count < SMALL_FILE_BYTES:
        print("\nВнимание: файл меньше 1 МБ. На таком размере время уходит в основном на "
              "установку соединения, и скорость получается заниженной. "
              "Для точного замера возьмите файл побольше.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        sys.exit(130)
