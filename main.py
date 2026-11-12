# -*- coding: utf-8 -*-
"""
main.py — главная программа видеорекордера.

Соединяет Recorder (захват/запись через GStreamer) с интерфейсом ui.UI
(холст 480x320, цвета Sony NEX-5N) и выводит результат на экран через SDL.

Запуск на Raspberry Pi:
    python3 main.py                      # обычная работа (папка ~/Videos)
    python3 main.py --fullscreen         # на весь экран (киоск-режим)
    python3 main.py --preview kmssink    # превью сразу на HDMI, без интерфейса
    python3 main.py --test-ui            # отладка интерфейса БЕЗ свистка

Остановка: клавиша Q или Ctrl+C.

ВАЖНО:
  * Режим вывода "kms" (аппаратные слои через pykms) пока НЕ реализован —
    программа честно переключается на "sdl", а не делает вид, что всё работает.
  * Запускайте БЕЗ sudo, иначе "~" в папке записей превратится в /root.
"""

import argparse
import logging
import os
import signal
import sys
import time

import pygame

import config
import ui
import video_view

log = logging.getLogger("main")


# Отключаем аудиодрайвер SDL до pygame.init(), чтобы ALSA не спамила
# подроблениями about underrun и не грузила CPU.
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_VIDEO_ALLOW_SCREENSAVER", "1")


def parse_args():
    p = argparse.ArgumentParser(
        description="Видеорекордер: захват видео, превью на экране, запись H.264 в MKV.")
    p.add_argument("--backend", choices=["auto", "kms", "sdl"], default=None,
                   help="режим вывода (по умолчанию config.DISPLAY_BACKEND)")
    p.add_argument("--preview", choices=["appsink", "kmssink", "none"],
                   default="appsink",
                   help="превью: appsink — в интерфейс (по умолчанию), "
                        "kmssink — сразу на HDMI, none — без превью")
    p.add_argument("--fullscreen", action="store_true",
                   help="на весь экран (для экрана 480x320 и киоск-режима)")
    p.add_argument("--test-ui", action="store_true",
                   help="отладка интерфейса БЕЗ захвата видео (надпись TEST на экране)")
    p.add_argument("--standard", choices=["PAL", "NTSC", "auto"], default=None,
                   help="видеостандарт: PAL (50 п/с), NTSC (59.94 п/с), auto (по умолчанию)")
    return p.parse_args()


def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))
    except OSError as e:
        print("Не удалось открыть журнал %s: %s" % (config.LOG_FILE, e), file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _free_space_mb():
    import shutil
    path = os.path.abspath(config.VIDEO_DIR)
    while not os.path.exists(path) and path != os.path.dirname(path):
        path = os.path.dirname(path)
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return None


class TestRecorder:
    """
    Заглушка вместо Recorder для --test-ui.

    НЕ имитирует кадры и запись: состояние всегда NO_DEVICE, видео нет,
    запись запрещена. Нужна, чтобы проверить интерфейс без свистка
    (локально на ПК или на Pi без камеры).
    """

    def __init__(self):
        self.state = config.STATE_NO_DEVICE
        self.message = "РЕЖИМ ОТЛАДКИ: захват видео отключён"
        self.device = None
        self.device_name = "Тест (без устройства)"
        self.video_standard = "PAL"
        self.capture_w = 720
        self.capture_h = 576
        self.capture_fps = 50.0
        self.rec_state = config.REC_IDLE
        self.rec_file = None
        self.rec_message = ""
        self.bitrate_key = config.DEFAULT_BITRATE

    def poll(self):
        pass

    def pull_preview_sample(self):
        return None

    def status(self):
        return {
            "state": self.state,
            "message": self.message,
            "device": self.device,
            "device_name": self.device_name,
            "video_standard": self.video_standard,
            "capture_w": self.capture_w,
            "capture_h": self.capture_h,
            "capture_fps": self.capture_fps,
            "fps": 0.0,
            "luma": None,
            "luma_std": None,
            "rec_state": self.rec_state,
            "rec_file": self.rec_file,
            "rec_elapsed": 0.0,
            "rec_message": self.rec_message,
            "bitrate_key": self.bitrate_key,
            "measured_bps": 0.0,
            "free_mb": _free_space_mb(),
        }

    def start_recording(self, bitrate_key=None):
        return False, "Режим отладки: запись недоступна"

    def stop_recording(self):
        pass

    def close(self):
        pass


def setup_display(requested, fullscreen=False):
    """
    Готовит окно SDL. Возвращает (screen, present, to_canvas):
      present(canvas) — показать холст на экране с сохранением пропорций;
      to_canvas(pos)  — координаты экрана -> координаты холста.
    """
    if requested in ("auto", "kms"):
        # Режим kms (видео и меню на разных аппаратных слоях GPU через pykms)
        # ещё не реализован. Не делаем вид, что он есть, — честно берём sdl.
        if requested == "kms":
            log.warning("backend=kms ещё не реализован — использую sdl")
        requested = "sdl"

    # В оконном режиме не тянем холст на весь 1080p экран — открываем окно 480x320,
    # иначе pygame будет делать программный рескейл 480x320->1620x1080 на CPU
    # на каждом кадре, что на Cortex-A7 без NEON убивает FPS.
    if fullscreen:
        info = pygame.display.Info()
        screen_w, screen_h = info.current_w, info.current_h
        if screen_w <= 0 or screen_h <= 0:
            screen_w, screen_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT
    else:
        screen_w, screen_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT
    canvas_w, canvas_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT

    flags = pygame.FULLSCREEN if fullscreen else 0
    try:
        screen = pygame.display.set_mode((screen_w, screen_h), flags)
    except pygame.error as e:
        if not fullscreen:
            raise
        log.warning("Не удалось включить fullscreen (%s) — открываю окно", e)
        screen = pygame.display.set_mode((config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
        screen_w, screen_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT

    # Экран совпадает с холстом: показываем как есть (так на 3.5" 480x320)
    if (screen_w, screen_h) == (canvas_w, canvas_h):
        return screen, (lambda c: screen.blit(c, (0, 0))), (lambda p: p)

    # Экран больше холста (fullscreen) — вписываем с сохранением пропорций
    # быстрым transform.scale (без smoothscale).
    lx, ly, lw, lh = config.letterbox_rect(screen_w, screen_h)
    scale = lw / canvas_w

    def present(canvas):
        screen.fill((0, 0, 0))
        screen.blit(pygame.transform.scale(canvas, (lw, lh)), (lx, ly))

    def to_canvas(p):
        return ((p[0] - lx) / scale, (p[1] - ly) / scale)

    return screen, present, to_canvas


def main():
    args = parse_args()
    setup_logging()
    backend = args.backend or config.DISPLAY_BACKEND

    # Применяем выбор видеостандарта из командной строки
    if args.standard:
        config.VIDEO_STANDARD = args.standard
        std = config.STANDARD_SETTINGS[args.standard]
        config.CAPTURE_WIDTH = std["width"]
        config.CAPTURE_HEIGHT = std["height"]
        config.CAPTURE_FPS = std["fps"]
        log.info("Видеостандарт задан: %s (%dx%d@%g)" % (
            args.standard, std["width"], std["height"], std["fps"]))

    if args.test_ui:
        log.warning("=" * 62)
        log.warning("РЕЖИМ ОТЛАДКИ --test-ui: захват видео ОТКЛЮЧЁН")
        log.warning("=" * 62)
        rec = TestRecorder()
    else:
        import recorder as recorder_module      # импорт здесь: gi/GStreamer не всегда есть
        try:
            rec = recorder_module.Recorder(preview=args.preview)
        except Exception as e:
            if args.preview == "kmssink":
                log.warning("kmssink недоступен (%s) — переключаю на appsink", e)
                rec = recorder_module.Recorder(preview="appsink")
            else:
                raise

    pygame.init()
    screen, present, to_canvas = setup_display(backend, args.fullscreen)

    canvas = pygame.Surface((config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
    video = video_view.VideoView()
    interface = ui.UI(rec, video, to_canvas)
    clock = pygame.time.Clock()

    log.info("Интерфейс запущен: холст %dx%d, фон %s, превью %s",
             config.CANVAS_WIDTH, config.CANVAS_HEIGHT, backend, args.preview)
    log.info("Управление: R — запись, B — битрейт, Q — выход")

    # Обработка сигналов системы для корректного завершения (SIGTERM от systemd,
    # SIGINT от Ctrl+C). Закрываем запись (MKV допишет оглавление) и останавливаем
    # захват, а не просто убиваем процесс.
    _quit = {"flag": False}

    def _on_signal(signum, frame):
        log.info("Получен сигнал %s — завершаемся корректно", signum)
        _quit["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        # Тайминг для диагностики производительности (пишем раз в 2 секунды).
        _last_timing_t = 0.0
        _timing = {"pull": 0.0, "update": 0.0, "draw": 0.0, "present": 0.0,
                   "count": 0}
        while not interface.quit_requested and not _quit["flag"]:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    interface.quit_requested = True
                    break
                interface.handle_event(event)
            if interface.quit_requested:
                break

            t0 = time.monotonic()
            rec.poll()                                  # захват: состояние, ошибки
            t1 = time.monotonic()
            video.update(rec.pull_preview_sample())     # последний кадр для экрана
            t2 = time.monotonic()
            interface.draw(canvas, rec.status())        # интерфейс на холсте
            t3 = time.monotonic()
            present(canvas)                             # холст -> экран
            t4 = time.monotonic()
            pygame.display.flip()
            clock.tick(config.UI_FPS)

            # Накопительная статистика timing
            _timing["pull"] += (t1 - t0)
            _timing["update"] += (t2 - t1)
            _timing["draw"] += (t3 - t2)
            _timing["present"] += (t4 - t3)
            _timing["count"] += 1
            now = time.monotonic()
            if now - _last_timing_t >= 2.0 and _timing["count"] > 0:
                c = _timing["count"]
                log.info("Тайминг кадра (ms, за %.0f с): pull=%.1f update=%.1f "
                         "draw=%.1f present=%.1f (ср. на кадр)",
                         now - _last_timing_t,
                         _timing["pull"] * 1000 / c, _timing["update"] * 1000 / c,
                         _timing["draw"] * 1000 / c, _timing["present"] * 1000 / c)
                _last_timing_t = now
                _timing = {"pull": 0.0, "update": 0.0, "draw": 0.0,
                           "present": 0.0, "count": 0}
    except KeyboardInterrupt:
        log.info("Ctrl+C — выходим")
    finally:
        log.info("Закрываем захват...")
        try:
            rec.close()
        except Exception as e:
            log.error("Ошибка при закрытии: %s", e)
        pygame.quit()
        log.info("Выход")
    return 0


if __name__ == "__main__":
    sys.exit(main())
