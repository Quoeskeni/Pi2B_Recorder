# -*- coding: utf-8 -*-
"""
main.py — главная программа видеорекордера.

Соединяет Recorder (захват/запись через GStreamer) с интерфейсом ui.UI
(холст 480x320, цвета Sony NEX-5N) и выводит результат на экран через SDL2.

Приоритет вывода:
  1. SDL2 Renderer (Window + Renderer + Texture) — аппаратное ускорение
     VideoCore IV / OpenGL ES, работает в любом разрешении за 1–3 мс на кадр.
  2. Обычный pygame.Surface + blit — фоллбэк для ui_selftest.py и систем
     без pygame._sdl2.

Запуск на Raspberry Pi:
    python3 main.py                      # обычная работа (папка ~/Videos)
    python3 main.py --fullscreen         # на весь экран (киоск-режим)
    python3 main.py --preview appsink    # превью в интерфейсе (по умолчанию)
    python3 main.py --preview none       # без превью (только запись)
    python3 main.py --test-ui            # отладка интерфейса БЕЗ свистка

Остановка: клавиша Q или Ctrl+C.

ВАЖНО:
  * Запускайте БЕЗ sudo, иначе "~" в папке записей превратится в /root.
  * kmssink убран из режимов — используется только appsink.
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

# Пытаемся импортировать SDL2 модули для GPU-ускорения (VideoCore IV / OpenGL ES)
# В pygame 2.6 Window, Renderer, Texture все находятся в pygame._sdl2.video.
_SDL2_IMPORT_ERR = None
try:
    from pygame._sdl2.video import Window, Renderer, Texture
    _HAVE_SDL2_RENDERER = True
except Exception as e:
    _SDL2_IMPORT_ERR = e
    _HAVE_SDL2_RENDERER = False


def parse_args():
    p = argparse.ArgumentParser(
        description="Видеорекордер: захват видео, превью на экране, запись H.264 в MKV.")
    p.add_argument("--backend", choices=["auto", "sdl"], default=None,
                   help="режим вывода (по умолчанию config.DISPLAY_BACKEND)")
    p.add_argument("--preview", choices=["appsink", "none"],
                   default="appsink",
                   help="превью: appsink — в интерфейс (по умолчанию), "
                        "none — без превью (только запись)")
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


def get_rss_mb():
    """Текущий RSS памяти процесса в МБ (из /proc/self/statm)."""
    try:
        with open("/proc/self/statm") as f:
            parts = f.read().split()
            # statm: size resident shared text lib data page_size_in_pages
            rss_pages = int(parts[1])
            page_size = os.sysconf("SC_PAGE_SIZE")
            return rss_pages * page_size / (1024 * 1024)
    except Exception:
        try:
            # ru_maxrss на Linux — в килобайтах
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return 0.0


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
    Готовит окно SDL и (при возможности) GPU-рендерер.

    Возвращает кортеж:
      screen        — pygame.Surface (окно) или None если GPU
      renderer      — SDL2 Renderer или None
      video_tex     — SDL2 Texture для видео или None
      ui_tex        — SDL2 Texture для UI или None
      canvas        — pygame.Surface 480x320 (холст, на который рисует ui.UI)
      to_canvas     — функция перевода координат экрана -> холста
      use_gpu       — True если используется GPU-рендерер
    """
    if requested in ("auto", "sdl"):
        requested = "sdl"

    if fullscreen:
        info = pygame.display.Info()
        screen_w, screen_h = info.current_w, info.current_h
        if screen_w <= 0 or screen_h <= 0:
            screen_w, screen_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT
    else:
        screen_w, screen_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT

    canvas_w, canvas_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT

    renderer = None
    frame_tex = None
    use_gpu = False

    # Пытаемся инициализировать GPU-рендерер pygame._sdl2 ПЕРЕД display.set_mode,
    # потому что в pygame нельзя смешивать display.set_mode и _sdl2.video.Window.
    if _HAVE_SDL2_RENDERER:
        try:
            window = Window(title="Видеорекордер", size=(canvas_w, canvas_h),
                            fullscreen_desktop=fullscreen, resizable=False)
            renderer = Renderer(window, accelerated=True, vsync=False)
            renderer.logical_size = (canvas_w, canvas_h)
            frame_tex = Texture(renderer, (canvas_w, canvas_h), streaming=True)
            use_gpu = True
            log.info("GPU-ускорение включено: SDL2 Renderer (logical %dx%d)" %
                     (canvas_w, canvas_h))
        except Exception as exc:
            log.warning("SDL2 GPU init failed: %s", exc, exc_info=True)
            renderer = None
            frame_tex = None

    if use_gpu:
        # С GPU-рендерером не нужно pygame.display.set_mode — окно уже создано через Window
        screen = None
        screen_w, screen_h = window.size
    else:
        # Обычное окно pygame (фоллбэк, если GPU недоступен)
        flags = pygame.FULLSCREEN | pygame.SCALED if fullscreen else pygame.SCALED
        try:
            screen = pygame.display.set_mode((screen_w, screen_h), flags)
            # После создания окна проверим реальный размер
            real_w, real_h = screen.get_size()
            if real_w != screen_w or real_h != screen_h:
                log.warning("Реальный размер окна %dx%d отличается от запрошенного %dx%d" %
                            (real_w, real_h, screen_w, screen_h))
                screen_w, screen_h = real_w, real_h
        except pygame.error as e:
            if not fullscreen:
                raise
            log.warning("Не удалось включить fullscreen (%s) — открываю окно", e)
            screen = pygame.display.set_mode((config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
            screen_w, screen_h = config.CANVAS_WIDTH, config.CANVAS_HEIGHT

    # to_canvas — перевод координат экрана в координаты холста (для мыши/тач)
    if (screen_w, screen_h) == (canvas_w, canvas_h):
        to_canvas = (lambda p: p)
    else:
        lx, ly, lw, lh = config.letterbox_rect(screen_w, screen_h)
        scale = lw / canvas_w

        def to_canvas(p):
            return ((p[0] - lx) / scale, (p[1] - ly) / scale)

    canvas = pygame.Surface((canvas_w, canvas_h))
    return screen, renderer, frame_tex, canvas, to_canvas, use_gpu, screen_w, screen_h


def main():
    args = parse_args()
    setup_logging()
    if _SDL2_IMPORT_ERR is not None:
        log.warning("SDL2 GPU import failed: %s", _SDL2_IMPORT_ERR)
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

    if args.preview == "kmssink":
        log.warning("preview=kmssink убран из режимов — использую appsink")
        args.preview = "appsink"

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
            log.error("Не удалось создать Recorder: %s", e)
            raise

    pygame.init()
    log.info("RSS после pygame.init(): %.1f МБ", get_rss_mb())
    screen, renderer, frame_tex, canvas, to_canvas, use_gpu, screen_w, screen_h = setup_display(
        backend, args.fullscreen)

    # Контейнер для текстуры, чтобы present() мог обновить ссылку
    _tex_container = [frame_tex]

    video = video_view.VideoView(use_gpu=False, renderer=None, video_tex=None)
    interface = ui.UI(rec, video, to_canvas)
    clock = pygame.time.Clock()

    log.info("RSS после создания UI и Recorder: %.1f МБ", get_rss_mb())

    # Логируем реальные параметры дисплея для диагностики
    info = pygame.display.Info()
    log.info("Дисплей: %dx%d, флаги=0x%x, свёртка=%s, GPU=%s",
             screen_w, screen_h,
             pygame.display.get_window_flags() if hasattr(pygame.display, 'get_window_flags') else 0,
             args.fullscreen, use_gpu)

    log.info("Интерфейс запущен: холст %dx%d, фон %s, превью %s, GPU=%s",
             config.CANVAS_WIDTH, config.CANVAS_HEIGHT, backend, args.preview, use_gpu)
    log.info("Управление: R — запись, B — битрейт, Q — выход")

    # Функция вывода холста на экран (GPU-текстура или софтовый blit)
    if use_gpu:
        _tex_container = [frame_tex]
        def present(canvas):
            # Загружаем canvas в текстуру (пересоздаём при изменении размера)
            try:
                _tex_container[0].update(canvas)
            except Exception as exc:
                # Формат пикселей canvas может не совпадать с текстурой —
                # пробуем пересоздать текстуру через from_surface
                try:
                    old_tex = _tex_container[0]
                    _tex_container[0] = Texture.from_surface(renderer, canvas)
                    if old_tex is not None:
                        try:
                            old_tex.close()
                        except Exception:
                            pass
                    log.warning("Пересоздана GPU-текстура из-за формата пикселей: %s", exc)
                except Exception as exc2:
                    log.error("Не удалось обновить GPU-текстуру: %s", exc2)
                    return
            renderer.clear()
            renderer.draw(_tex_container[0], None,
                          pygame.Rect(0, 0, config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
            renderer.present()
    elif canvas.get_size() == (config.CANVAS_WIDTH, config.CANVAS_HEIGHT):
        def present(canvas):
            screen.blit(canvas, (0, 0))
            pygame.display.flip()
    else:
        # Не-GPU fullscreen: масштабируем 480x320 на весь экран
        lx, ly, lw, lh = config.letterbox_rect(screen_w, screen_h)
        def present(canvas):
            screen.fill((0, 0, 0))
            screen.blit(pygame.transform.scale(canvas, (lw, lh)), (lx, ly))
            pygame.display.flip()

    # Обработка сигналов системы для корректного завершения
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
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_m:
                        interface.open_capture_mode_menu()
                interface.handle_event(event)
            if interface.quit_requested:
                break

            now = time.monotonic()
            t0 = now
            rec.poll()                                  # захват: состояние, ошибки
            t1 = time.monotonic()
            new_frame = video.update(rec.pull_preview_sample())  # последний кадр для экрана
            t2 = time.monotonic()

            status = rec.status()
            need_draw = new_frame or interface.need_ui_redraw(status)
            if need_draw:
                interface.draw(canvas, status)          # интерфейс на холсте (с кэшем UI)
                t3 = time.monotonic()
            else:
                t3 = t2

            if need_draw:
                present(canvas)
                t4 = time.monotonic()
            else:
                t4 = t3

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
                rss = get_rss_mb()
                log.info("Тайминг кадра (ms, за %.0f с): pull=%.1f update=%.1f "
                         "draw=%.1f present=%.1f | RSS=%.1f МБ (ср. на кадр)",
                         now - _last_timing_t,
                         _timing["pull"] * 1000 / c,
                         _timing["update"] * 1000 / c,
                         _timing["draw"] * 1000 / c,
                         _timing["present"] * 1000 / c,
                         rss)
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
        # Освобождаем GPU-текстуру
        try:
            if _tex_container[0] is not None:
                _tex_container[0].close()
        except Exception:
            pass
        pygame.quit()
        log.info("Выход")
    return 0


if __name__ == "__main__":
    sys.exit(main())
