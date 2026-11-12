# -*- coding: utf-8 -*-
"""
ui_selftest.py — проверка интерфейса без свистка и без GStreamer.

Рисует экраны в PNG-файлы (ui_shot_*.png в текущей папке), чтобы можно было
посмотреть раскладку, и проверяет логику управления (клавиатура, мышь, меню).

Запуск (на ПК или на Pi):
    python3 ui_selftest.py

На ПК используется SDL_VIDEODRIVER=dummy — настоящий экран не нужен.
"""

import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import ui
import video_view


class StubRecorder:
    """Минимальная замена Recorder: только то, что использует ui.UI."""

    def __init__(self, state=config.STATE_NO_DEVICE):
        self.state = state
        self.message = "Заглушка"
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
        self.elapsed = 0.0
        self.free_mb = 2873.0
        self.started = 0

    def poll(self):
        pass

    def pull_preview_sample(self):
        return None

    def status(self):
        live = self.state in (config.STATE_LIVE, config.STATE_NO_SIGNAL)
        return {
            "state": self.state,
            "message": self.message,
            "device": self.device,
            "device_name": self.device_name,
            "video_standard": self.video_standard,
            "capture_w": self.capture_w,
            "capture_h": self.capture_h,
            "capture_fps": self.capture_fps,
            "fps": 25.0 if live else 0.0,
            "luma": 40.0 if live else None,
            "luma_std": 20.0 if live else None,
            "rec_state": self.rec_state,
            "rec_file": self.rec_file,
            "rec_elapsed": self.elapsed,
            "rec_message": self.rec_message,
            "bitrate_key": self.bitrate_key,
            "measured_bps": 5.0e6,
            "free_mb": self.free_mb,
        }

    def start_recording(self, bitrate_key=None):
        self.rec_state = config.REC_RUNNING
        self.rec_file = "/tmp/REC_test.mkv"
        self.started += 1
        return True, "Запись: REC_test.mkv"

    def stop_recording(self):
        self.rec_state = config.REC_IDLE

    def close(self):
        pass


def key(k):
    return pygame.event.Event(pygame.KEYDOWN, {"key": k})


def make_test_frame(width=720, height=480):
    """
    Синтетический I420-кадр: яркость — горизонтальный градиент, цвет — нейтральный
    (U=V=128, т.е. среднетоновая шкала). По картинке видно, что кадр не перевёрнут
    и не разрулен, а распаковка не перепутала плоскости.
    """
    import numpy as np
    y = np.tile(np.arange(width, dtype=np.uint16) * 255 // (width - 1),
                (height, 1)).astype(np.uint8)
    u = np.full((height // 2, width // 2), 128, dtype=np.uint8)
    v = np.full((height // 2, width // 2), 128, dtype=np.uint8)
    return y.tobytes() + u.tobytes() + v.tobytes()


def check_conversion():
    """Цвета I420 -> RGB должны совпадать с формулой BT.601."""
    width, height = 8, 4
    cases = [
        ((128, 128, 128), (128, 128, 128)),   # серое поле
        ((235, 128, 128), (235, 235, 235)),   # белое
        ((16, 128, 128), (16, 16, 16)),       # чёрное
    ]
    for (y, u, v), want in cases:
        data = (bytes([y]) * width * height
                + bytes([u]) * (width // 2 * height // 2)
                + bytes([v]) * (width // 2 * height // 2))
        surf = video_view.frame_to_surface(width, height, data)
        got = surf.get_at((0, 0))[:3]
        assert got == want, "I420%s -> %s, ожидали %s" % ((y, u, v), got, want)
    print("I420 -> RGB: OK")


def check_input():
    """Клавиатура и мышь должны переключать экраны и запись."""
    rec = StubRecorder()
    u = ui.UI(rec, video_view.VideoView())

    # меню битрейта: открыть, выбрать второй пункт, применить
    u.handle_event(key(pygame.K_b))
    assert u.screen == "menu", u.screen
    assert u.menu_index == config.BITRATE_ORDER.index(config.DEFAULT_BITRATE)
    u.handle_event(key(pygame.K_DOWN))
    assert u.menu_index == 2, u.menu_index
    u.handle_event(key(pygame.K_RETURN))
    assert u.screen == "live", u.screen
    assert rec.bitrate_key == config.BITRATE_ORDER[2], rec.bitrate_key

    # Esc в меню — назад, не выход
    u.handle_event(key(pygame.K_b))
    u.handle_event(key(pygame.K_ESCAPE))
    assert u.screen == "live" and not u.quit_requested

    # запись по R
    u.handle_event(key(pygame.K_r))
    assert rec.rec_state == config.REC_RUNNING, rec.rec_state
    u.handle_event(key(pygame.K_r))
    assert rec.rec_state == config.REC_IDLE, rec.rec_state

    # мышь: нажатие на кнопку REC (без сдвига — это нажатие, не свайп)
    pos = ui.REC_BUTTON_RECT.center
    u.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, {"button": 1, "pos": pos}))
    u.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, {"button": 1, "pos": pos}))
    assert rec.rec_state == config.REC_RUNNING, rec.rec_state

    # нажатие на кнопку битрейта открывает меню
    pos = ui.BITRATE_BUTTON_RECT.center
    u.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, {"button": 1, "pos": pos}))
    u.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, {"button": 1, "pos": pos}))
    assert u.screen == "menu", u.screen

    # Q — выход
    u.handle_event(key(pygame.K_q))
    assert u.quit_requested
    print("Управление: OK")


def shot(canvas, u, rec, name):
    u.draw(canvas, rec.status())
    path = "ui_shot_%s.png" % name
    pygame.image.save(canvas, path)
    print("  картинка:", path)


def draw_screens():
    canvas = pygame.Surface((config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
    video = video_view.VideoView()
    video.surface = video_view.frame_to_surface(720, 480, make_test_frame())
    video.size = (720, 480)

    # 1. нет устройства
    rec = StubRecorder(config.STATE_NO_DEVICE)
    u = ui.UI(rec, video)
    shot(canvas, u, rec, "no_device")

    # 2. живой экран с видео
    rec = StubRecorder(config.STATE_LIVE)
    u = ui.UI(rec, video)
    shot(canvas, u, rec, "live")

    # 3. идёт запись
    rec = StubRecorder(config.STATE_LIVE)
    rec.rec_state = config.REC_RUNNING
    rec.elapsed = 83
    u = ui.UI(rec, video)
    shot(canvas, u, rec, "rec")

    # 4. меню битрейта
    rec = StubRecorder(config.STATE_LIVE)
    u = ui.UI(rec, video)
    u.open_menu()
    shot(canvas, u, rec, "menu")

    # 5. нет сигнала
    rec = StubRecorder(config.STATE_NO_SIGNAL)
    u = ui.UI(rec, video)
    shot(canvas, u, rec, "no_signal")

    # 6. экран диагностики USB (клавиша D)
    rec = StubRecorder(config.STATE_LIVE)
    u = ui.UI(rec, video)
    u.screen = "diag"
    u._diag["device"] = {"vid": "534d", "pid": "0021",
                         "name": "MacroSilicon MS210x / EasierCAP (AV TO USB2.0)"}
    u._diag["usb"] = True
    u._diag["video"] = "/dev/v4l/by-id/usb-MACROSILICON_AV_TO_USB2.0_20150130-video-index0"
    u._diag["modes"] = [("MJPG", 480, 320, 25.0), ("MJPG", 640, 480, 25.0),
                        ("MJPG", 720, 480, 25.0), ("YUY2", 480, 320, 25.0)]
    u._diag["diag_scroll"] = 0
    shot(canvas, u, rec, "diag")

    # 7. мало места
    rec = StubRecorder(config.STATE_LIVE)
    rec.free_mb = 120.0
    u = ui.UI(rec, video)
    shot(canvas, u, rec, "low_space")


def main():
    pygame.init()
    print("=== Проверка интерфейса (без свистка) ===")
    check_conversion()
    check_input()
    print("Экраны:")
    draw_screens()
    pygame.quit()
    print("Готово.")


if __name__ == "__main__":
    main()
