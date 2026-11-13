# -*- coding: utf-8 -*-
"""
ui.py — интерфейс видеорекордера на холсте 480x320 в стиле Sony NEX-5N.

Что рисует:
  * видео в кадрировке config.video_rect_on_canvas();
  * верхнюю строку: состояние (LIVE / NO DEVICE / NO SIGNAL / STARTING),
    индикатор REC с таймером, свободное место;
  * нижнюю строку: кнопку битрейта, подсказку по клавишам, кнопку REC;
  * меню выбора битрейта (навигация и выбор);
  * сообщения о проблемах: нет устройства (с диагностикой), нет сигнала,
    мало места, ошибки записи.

Управление:
  * клавиатура: R — запись, B — меню битрейта, Q/Esc — выход,
    стрелки + Enter — навигация по меню;
  * мышь/тач: нажатия по кнопкам, свайп вверх/низ в меню.

Модуль не зависит от GStreamer: получает уже готовый кадр (video_view) и
словарь status() от Recorder. Благодаря этому интерфейс проверяется локально
на ПК (SDL_VIDEODRIVER=dummy).
"""

import os
import time

import pygame

import config
import v4l2_checker

# ---------------------------------------------------------------------
# Раскладка (прямоугольники на холсте)
# ---------------------------------------------------------------------
TOP_BAR_RECT = pygame.Rect(0, 0, config.CANVAS_WIDTH, config.TOP_BAR_HEIGHT)
BOTTOM_BAR_RECT = pygame.Rect(0, config.CANVAS_HEIGHT - config.BOTTOM_BAR_HEIGHT,
                              config.CANVAS_WIDTH, config.BOTTOM_BAR_HEIGHT)
BITRATE_BUTTON_RECT = pygame.Rect(config.BITRATE_BUTTON_RECT)
REC_BUTTON_RECT = pygame.Rect(config.REC_BUTTON_RECT)
MENU_RECT = pygame.Rect(config.MENU_RECT)

# Цвета состояний захвата (точка в верхней строке)
STATE_COLORS = {
    config.STATE_LIVE: (60, 200, 90),
    config.STATE_NO_DEVICE: (200, 60, 60),
    config.STATE_NO_SIGNAL: config.WARNING_YELLOW,
    config.STATE_STARTING: config.SONY_ORANGE,
}
STATE_LABELS = {
    config.STATE_LIVE: "LIVE",
    config.STATE_NO_DEVICE: "NO DEVICE",
    config.STATE_NO_SIGNAL: "NO SIGNAL",
    config.STATE_STARTING: "STARTING",
}


def fmt_time(seconds):
    """Секунды -> "HH:MM:SS" (для таймера записи)."""
    s = int(max(0, seconds))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


# =====================================================================
# Шрифты и текст
# =====================================================================
class Fonts:
    """Шрифты DejaVu (с запасным встроенным, если файла нет)."""

    def __init__(self):
        self._cache = {}

    def get(self, size, bold=False):
        key = (size, bold)
        font = self._cache.get(key)
        if font is None:
            path = config.FONT_BOLD_PATH if bold else config.FONT_PATH
            try:
                font = pygame.font.Font(path, size)
            except (IOError, OSError):
                font = pygame.font.Font(None, size)
            self._cache[key] = font
        return font


class TextCache:
    """Кэш отрисованного текста: font.render — не самая дешёвая операция."""

    MAX = 256

    def __init__(self, fonts):
        self.fonts = fonts
        self._cache = {}

    def render(self, s, size, color, bold=False):
        key = (s, size, color, bold)
        surf = self._cache.get(key)
        if surf is None:
            font = self.fonts.get(size, bold)
            surf = font.render(s, True, color)
            if len(self._cache) >= self.MAX:
                self._cache.clear()
            self._cache[key] = surf
        return surf


# =====================================================================
# Интерфейс
# =====================================================================
class UI:
    def __init__(self, recorder, video, to_canvas=None):
        self.rec = recorder
        self.video = video
        # to_canvas переводит координаты экрана в координаты холста
        self.to_canvas = to_canvas or (lambda p: p)

        self.fonts = Fonts()
        self.text = TextCache(self.fonts)

        self.screen = "live"                 # "live" | "menu" | "recordings" | "capture_mode"
        self.menu_index = 0
        self.rec_index = 0                   # выделенная запись в браузере
        self.capture_mode_index = 0          # выделенный режим захвата
        self.capture_mode_items = []         # список режимов [(fmt, w, h, fps, label), ...]
        self.menu_items = [
            (key,
             config.BITRATE_PRESETS[key]["label"],
             "%.1f Мбит/с" % (config.BITRATE_PRESETS[key]["bps"] / 1e6))
            for key in config.BITRATE_ORDER
        ]

        self.quit_requested = False
        self._press_pos = None
        self._press_t = 0.0

        self._notify_text = ""
        self._notify_color = config.TEXT_MAIN
        self._notify_until = 0.0

        self._diag_t = 0.0
        self._diag = {"usb": None, "video": None}
        self._panel_cache = {}
        # Кэш отрисованных строк диагностики (пересчитываем только при изменении данных)
        self._diag_lines_cache = None
        self._diag_cache_key = None
        # Ключ кэша UI: меняется только когда реально изменились данные
        self._ui_cache_key = None

    # -----------------------------------------------------------------
    # Плашки (полупрозрачные, со скруглёнными углами)
    # -----------------------------------------------------------------
    def _fit_text(self, text, font_size, max_width, bold=False):
        """
        Обрезать текст с добавлением '...' если он не влезает в max_width.
        Для неразрывных строк (пути к устройствам).
        bold — использовать ли жирный шрифт для измерения ширины (он шире!).
        """
        font = self.fonts.get(font_size, bold)
        surf = font.render(text, True, (255, 255, 255))
        if surf.get_width() <= max_width:
            return text
        ellipsis = "..."
        ell_w = font.render(ellipsis, True, (255, 255, 255)).get_width()
        available = max_width - ell_w
        if available <= 0:
            return ellipsis
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            cand = text[:mid]
            if font.render(cand, True, (255, 255, 255)).get_width() <= available:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ellipsis

    def _wrap_text(self, text, font, max_width):
        """
        Перенос строки по словам: разбивает длинный текст на несколько строк,
        каждая не шире max_width. Возвращает список строк.
        """
        words = text.split()
        if not words:
            return [""]
        lines = []
        current = words[0]
        for word in words[1:]:
            test = current + " " + word
            if font.render(test, True, (255, 255, 255)).get_width() <= max_width:
                current = test
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _panel(self, rect, fill, border=None):
        key = (tuple(rect), fill, border)
        surf = self._panel_cache.get(key)
        if surf is None:
            w, h = int(rect[2]), int(rect[3])
            surf = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.rect(surf, fill, (0, 0, w, h), border_radius=config.PANEL_RADIUS)
            if border is not None:
                pygame.draw.rect(surf, border, (0, 0, w, h), width=1,
                                 border_radius=config.PANEL_RADIUS)
            if len(self._panel_cache) > 32:
                self._panel_cache.clear()
            self._panel_cache[key] = surf
        return surf

    # -----------------------------------------------------------------
    # Отрисовка
    # -----------------------------------------------------------------
    def _ui_cache_key_from_status(self, status):
        """Ключ кэша UI: меняется только когда реально изменились данные."""
        return (
            self.screen,
            status.get("state"),
            status.get("rec_state"),
            int(status.get("rec_elapsed", 0)),      # таймер — точность 1 с
            status.get("bitrate_key"),
            None if status.get("free_mb") is None else int(status.get("free_mb")),
            status.get("message"),
        )

    def need_ui_redraw(self, status):
        """True, если нужно перерисовать интерфейс (изменился статус/экран)."""
        key = self._ui_cache_key_from_status(status)
        if key != self._ui_cache_key:
            self._ui_cache_key = key
            return True
        return False

    def draw(self, canvas, status):
        """
        Отрисовать интерфейс на canvas (pygame.Surface 480x320).
        В GPU-режиме main.py скопирует canvas в ui_tex и нарисует через Renderer.
        """
        canvas.fill(config.BLACK)
        rect = config.video_rect_on_canvas()
        self.video.draw(canvas, rect)
        # тонкая рамка вокруг видео — виден край кадра
        pygame.draw.rect(canvas, (45, 45, 48), rect, 1)

        if self.screen == "menu":
            self._draw_menu(canvas, status)
        elif self.screen == "recordings":
            self._draw_recordings(canvas, status)
        elif self.screen == "diag":
            self._draw_diag(canvas, status)
        elif self.screen == "capture_mode":
            self._draw_capture_mode_menu(canvas, status)
        else:
            self._draw_live(canvas, status)

        self._draw_notify(canvas)

    # --- живой экран ---------------------------------------------------
    def _draw_live(self, canvas, status):
        self._draw_top_bar(canvas, status)
        self._draw_bottom_bar(canvas, status)

        state = status["state"]
        if state == config.STATE_NO_DEVICE:
            self._draw_no_device(canvas, status)
        elif state == config.STATE_NO_SIGNAL:
            self._draw_message_panel(canvas, config.TEXT_NO_SIGNAL,
                                     ["Проверьте кабель камеры и её питание"],
                                     color=config.WARNING_YELLOW)
        elif state == config.STATE_STARTING:
            self._draw_message_panel(canvas, "Запуск захвата...",
                                     [status.get("message") or ""],
                                     color=config.TEXT_DIM)

    def _draw_top_bar(self, canvas, status):
        bar = TOP_BAR_RECT
        canvas.blit(self._panel(bar, config.PANEL_GRAPHITE, config.PANEL_BORDER),
                    (bar.x, bar.y))

        # состояние: цветная точка + надпись
        state = status["state"]
        color = STATE_COLORS.get(state, config.TEXT_DIM)
        cy = bar.y + bar.height // 2
        pygame.draw.circle(canvas, color, (bar.x + config.OSD_MARGIN + 4, cy), 4)
        label = self.text.render(STATE_LABELS.get(state, state),
                                 config.FONT_SIZE_SMALL, config.TEXT_MAIN, bold=True)
        canvas.blit(label, (bar.x + config.OSD_MARGIN + 12, cy - label.get_height() // 2))

        # видеостандарт и разрешение (кратко, между состоянием и REC)
        std = status.get("video_standard") or ""
        res = "%dx%d@%g" % (status.get("capture_w", config.CAPTURE_WIDTH),
                            status.get("capture_h", config.CAPTURE_HEIGHT),
                            status.get("capture_fps", config.CAPTURE_FPS))
        info_text = "%s %s" % (std, res) if std else res
        if info_text:
            info = self.text.render(info_text, config.FONT_SIZE_SMALL, config.TEXT_DIM)
            info_x = bar.x + config.OSD_MARGIN + 12 + label.get_width() + 8
            # не наезжаем на индикатор записи (rx = bar.x + 190)
            if info_x + info.get_width() < bar.x + 180:
                canvas.blit(info, (info_x, cy - info.get_height() // 2))

        # индикатор записи / ожидания
        rec_state = status["rec_state"]
        rx = bar.x + 190
        if rec_state == config.REC_RUNNING:
            if int(time.monotonic() / config.REC_BLINK_PERIOD_S) % 2 == 0:
                pygame.draw.circle(canvas, config.REC_RED, (rx, cy), 5)
            text = "REC " + fmt_time(status["rec_elapsed"])
            rcolor = config.REC_RED
        elif rec_state == config.REC_STOPPING:
            text = "STOPPING"
            rcolor = config.WARNING_YELLOW
        else:
            text = config.TEXT_STBY
            rcolor = config.TEXT_DIM
        rec = self.text.render(text, config.FONT_SIZE_SMALL, rcolor, bold=True)
        canvas.blit(rec, (rx + 12, cy - rec.get_height() // 2))

        # свободное место (справа)
        free = status["free_mb"]
        if free is None:
            text, color = "?", config.TEXT_DIM
        elif free < config.MIN_FREE_SPACE_MB:
            text, color = config.TEXT_LOW_SPACE, config.WARNING_YELLOW
        else:
            text, color = "%d МБ" % int(free), config.TEXT_MAIN
        surf = self.text.render(text, config.FONT_SIZE_SMALL, color, bold=True)
        canvas.blit(surf, (bar.right - config.OSD_MARGIN - surf.get_width(),
                           cy - surf.get_height() // 2))

    def _draw_bottom_bar(self, canvas, status):
        bar = BOTTOM_BAR_RECT
        canvas.blit(self._panel(bar, config.PANEL_GRAPHITE, config.PANEL_BORDER),
                    (bar.x, bar.y))

        # кнопка битрейта
        preset = config.BITRATE_PRESETS.get(status["bitrate_key"])
        value = "?" if preset is None else "%.1f МБИТ/С" % (preset["bps"] / 1e6)
        self._draw_button(canvas, BITRATE_BUTTON_RECT,
                          [("БИТРЕЙТ", config.FONT_SIZE_SMALL, config.TEXT_DIM, False),
                           (value, config.FONT_SIZE_NORMAL, config.TEXT_MAIN, True)],
                          active=False)

        # кнопка REC
        rec_state = status["rec_state"]
        recording = rec_state == config.REC_RUNNING
        self._draw_button(canvas, REC_BUTTON_RECT,
                          [("STOP" if recording else "REC",
                            config.FONT_SIZE_NORMAL,
                            config.WHITE if recording else config.REC_RED, True)],
                          active=recording)

    def _draw_button(self, canvas, rect, lines, active=False):
        """Прямоугольная кнопка с одной или несколькими строками текста."""
        fill = config.BUTTON_ACTIVE if active else (25, 25, 28, 210)
        canvas.blit(self._panel(rect, fill, config.PANEL_BORDER), (rect.x, rect.y))
        total = sum(self.fonts.get(size, bold).get_linesize() for _, size, _, bold in lines)
        y = rect.y + (rect.height - total) // 2
        for text, size, color, bold in lines:
            surf = self.text.render(text, size, color, bold)
            canvas.blit(surf, (rect.x + (rect.width - surf.get_width()) // 2, y))
            y += surf.get_height()

    # --- экран "нет устройства" ---------------------------------------
    def _refresh_diag(self):
        now = time.monotonic()
        if now - self._diag_t < config.STATUS_UPDATE_INTERVAL_S:
            return
        self._diag_t = now
        self._diag["usb"] = v4l2_checker.usb_dongle_present()
        self._diag["video"] = v4l2_checker.find_video_device()

    def _draw_no_device(self, canvas, status):
        self._refresh_diag()
        usb = self._diag.get("usb")
        video = self._diag.get("video")
        dev_name = os.path.basename(video) if video else "не найдено"
        # Обрезаем длинное имя устройства, чтобы не вылезало за рамки модалки
        dev_name = self._fit_text(dev_name, config.FONT_SIZE_NORMAL, 340 - 40)
        lines = [
            status.get("message") or "Свисток захвата не подключён к USB",
            "",
            "Свисток в USB: " + ("да" if usb else "нет"),
            "Видеоустройство: " + dev_name,
            "",
            "Проверьте: USB-свисток, кабель камеры,",
            "питание (нет ли значка молнии на экране).",
        ]
        self._draw_message_panel(canvas, "НЕТ УСТРОЙСТВА", lines,
                                 color=config.WARNING_YELLOW)

    def _draw_message_panel(self, canvas, title, lines, color=config.TEXT_MAIN):
        w = 340
        font = self.fonts.get(config.FONT_SIZE_NORMAL)
        max_text_w = w - 40   # отступы по бокам

        # Разворачиваем длинные строки по словам.
        # Пути к устройствам (/dev/..., usb-...) не переносим — обрезаем многоточием.
        wrapped_lines = []
        for line in lines:
            if line.startswith("/dev/") or line.startswith("usb-"):
                wrapped_lines.append(self._fit_text(line, font, max_text_w))
            else:
                wrapped_lines.extend(self._wrap_text(line, font, max_text_w))

        line_h = 18
        h = 34 + len(wrapped_lines) * line_h + 10
        x = (config.CANVAS_WIDTH - w) // 2
        y = (config.CANVAS_HEIGHT - h) // 2
        canvas.blit(self._panel((x, y, w, h), config.PANEL_GRAPHITE_DARK,
                                config.PANEL_BORDER), (x, y))
        t = self.text.render(title, config.FONT_SIZE_LARGE, color, bold=True)
        canvas.blit(t, (x + (w - t.get_width()) // 2, y + 10))
        for i, line in enumerate(wrapped_lines):
            s = self.text.render(line, config.FONT_SIZE_NORMAL, config.TEXT_DIM)
            canvas.blit(s, (x + (w - s.get_width()) // 2, y + 34 + i * line_h))

    # --- меню битрейта --------------------------------------------------
    def _menu_item_rects(self):
        x = MENU_RECT.x + 12
        w = MENU_RECT.width - 24
        y0 = MENU_RECT.y + 52
        return [pygame.Rect(x, y0 + i * config.MENU_ITEM_HEIGHT, w,
                            config.MENU_ITEM_HEIGHT - 4)
                for i in range(len(self.menu_items))]

    def _draw_menu(self, canvas, status):
        # затемняем видео под меню
        overlay = pygame.Surface(canvas.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 120))
        canvas.blit(overlay, (0, 0))

        canvas.blit(self._panel(MENU_RECT, config.PANEL_GRAPHITE_DARK,
                                config.PANEL_BORDER), (MENU_RECT.x, MENU_RECT.y))
        title = self.text.render("БИТРЕЙТ", config.FONT_SIZE_LARGE,
                                 config.SONY_ORANGE, bold=True)
        canvas.blit(title, (MENU_RECT.x + 16, MENU_RECT.y + 12))

        for i, (key, label, value) in enumerate(self.menu_items):
            rect = self._menu_item_rects()[i]
            active = (i == self.menu_index)
            current = (key == status.get("bitrate_key"))
            if active:
                canvas.blit(self._panel(rect, config.BUTTON_ACTIVE), (rect.x, rect.y))
            name_color = config.TEXT_MAIN if active else (
                config.SONY_ORANGE if current else config.TEXT_DIM)
            name = self.text.render(label, config.FONT_SIZE_NORMAL, name_color, bold=True)
            canvas.blit(name, (rect.x + 12, rect.y + (rect.height - name.get_height()) // 2))
            val = self.text.render(value, config.FONT_SIZE_SMALL,
                                   config.TEXT_MAIN if active else config.TEXT_DIM)
            canvas.blit(val, (rect.right - 12 - val.get_width(),
                              rect.y + (rect.height - val.get_height()) // 2))

        hint = self.text.render("↑↓ — выбор   Enter — применить   Esc — назад",
                                config.FONT_SIZE_SMALL, config.TEXT_DIM)
        canvas.blit(hint, (MENU_RECT.centerx - hint.get_width() // 2,
                           MENU_RECT.bottom - 24))

    def _draw_capture_mode_menu(self, canvas, status):
        """Отрисовка меню выбора режима захвата (разрешение, FPS, формат)."""
        # затемняем видео под меню
        overlay = pygame.Surface(canvas.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        canvas.blit(overlay, (0, 0))

        panel = pygame.Rect(24, 30, config.CANVAS_WIDTH - 48, config.CANVAS_HEIGHT - 60)
        canvas.blit(self._panel(panel, config.PANEL_GRAPHITE_DARK,
                                config.PANEL_BORDER), (panel.x, panel.y))

        title = self.text.render("РЕЖИМ ЗАХВАТА", config.FONT_SIZE_LARGE,
                                 config.SONY_ORANGE, bold=True)
        canvas.blit(title, (panel.x + 16, panel.y + 10))

        # подсказка о записи
        if status["rec_state"] == config.REC_RUNNING:
            warn = self.text.render("Запись активна — смена режима недоступна",
                                    config.FONT_SIZE_SMALL, config.WARNING_YELLOW)
            canvas.blit(warn, (panel.x + 16, panel.bottom - 24))
            return

        # список режимов
        items = self.capture_mode_items
        if not items:
            empty = self.text.render("Режимы не найдены", config.FONT_SIZE_NORMAL,
                                     config.TEXT_DIM)
            canvas.blit(empty, (panel.x + 16, panel.y + 44))
            return

        item_h = 30
        visible = (panel.height - 60) // item_h
        start = max(0, min(self.capture_mode_index, len(items) - visible)) if visible > 0 else 0
        end = min(len(items), start + visible)

        for i in range(start, end):
            f, w, h, fps, label = items[i]
            y = panel.y + 60 + (i - start) * item_h
            active = (i == self.capture_mode_index)
            current = (f == config.CAPTURE_FORMAT and w == config.CAPTURE_WIDTH
                       and h == config.CAPTURE_HEIGHT
                       and abs(fps - config.CAPTURE_FPS) < 0.5)
            if active:
                canvas.blit(self._panel((panel.x + 8, y - 2,
                                         panel.width - 16, item_h),
                                        config.BUTTON_ACTIVE), (panel.x + 8, y - 2))
            name_color = config.TEXT_MAIN if active else (
                config.SONY_ORANGE if current else config.TEXT_DIM)
            name = self.text.render(label, config.FONT_SIZE_NORMAL,
                                    name_color, bold=active)
            canvas.blit(name, (panel.x + 16, y))

        if len(items) > visible:
            hint = self.text.render("↑↓ — выбор   Enter — применить   Esc — назад",
                                    config.FONT_SIZE_SMALL, config.TEXT_DIM)
        else:
            hint = self.text.render("Enter — применить   Esc — назад",
                                    config.FONT_SIZE_SMALL, config.TEXT_DIM)
        canvas.blit(hint, (panel.centerx - hint.get_width() // 2,
                           panel.bottom - 24))

    def _apply_capture_mode(self):
        """Применить выбранный режим захвата."""
        if not self.capture_mode_items:
            return
        f, w, h, fps, label = self.capture_mode_items[self.capture_mode_index]
        changed = False
        if config.CAPTURE_FORMAT != f:
            config.CAPTURE_FORMAT = f
            changed = True
        if config.CAPTURE_WIDTH != w:
            config.CAPTURE_WIDTH = w
            changed = True
        if config.CAPTURE_HEIGHT != h:
            config.CAPTURE_HEIGHT = h
            changed = True
        if abs(config.CAPTURE_FPS - fps) > 0.5:
            config.CAPTURE_FPS = fps
            changed = True
        if changed:
            self.screen = "live"
            self.notify("Режим захвата: %s" % label)
            # Перезапускаем захват с новыми параметрами
            self.rec.set_capture_mode(f, w, h, fps)
        else:
            self.screen = "live"

    # --- всплывающее сообщение ----------------------------------------
    def notify(self, text, ok=True, seconds=2.0):
        self._notify_text = text
        self._notify_color = config.TEXT_MAIN if ok else config.WARNING_YELLOW
        self._notify_until = time.monotonic() + seconds

    def _draw_notify(self, canvas):
        if time.monotonic() > self._notify_until or not self._notify_text:
            return
        surf = self.text.render(self._notify_text, config.FONT_SIZE_NORMAL,
                                self._notify_color, bold=True)
        w, h = surf.get_width() + 20, surf.get_height() + 12
        x = (config.CANVAS_WIDTH - w) // 2
        y = config.CANVAS_HEIGHT - config.BOTTOM_BAR_HEIGHT - h - 8
        canvas.blit(self._panel((x, y, w, h), config.PANEL_GRAPHITE_DARK,
                                config.PANEL_BORDER), (x, y))
        canvas.blit(surf, (x + 10, y + 6))

    # -----------------------------------------------------------------
    # Браузер записей
    # -----------------------------------------------------------------
    def _list_recordings(self):
        """Список файлов записей из config.VIDEO_DIR, отсортированных по дате."""
        try:
            files = []
            for name in os.listdir(config.VIDEO_DIR):
                if not name.endswith(".mkv"):
                    continue
                path = os.path.join(config.VIDEO_DIR, name)
                try:
                    st = os.stat(path)
                    files.append({
                        "name": name,
                        "path": path,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
                except OSError:
                    continue
            files.sort(key=lambda f: f["mtime"], reverse=True)
            return files
        except OSError:
            return []

    def _draw_recordings(self, canvas, status):
        """Экран со списком записей."""
        # затемняем видео под списком
        overlay = pygame.Surface(canvas.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        canvas.blit(overlay, (0, 0))

        panel = pygame.Rect(24, 30, config.CANVAS_WIDTH - 48, config.CANVAS_HEIGHT - 60)
        canvas.blit(self._panel(panel, config.PANEL_GRAPHITE_DARK,
                                config.PANEL_BORDER), (panel.x, panel.y))

        title = self.text.render("ЗАПИСИ", config.FONT_SIZE_LARGE,
                                 config.SONY_ORANGE, bold=True)
        canvas.blit(title, (panel.x + 16, panel.y + 10))

        files = self._list_recordings()
        if not files:
            empty = self.text.render("Записей нет", config.FONT_SIZE_NORMAL,
                                     config.TEXT_DIM)
            canvas.blit(empty, (panel.x + 16, panel.y + 44))
        else:
            # заголовки столбцов
            hdr_y = panel.y + 44
            name_col = self.text.render("ФАЙЛ", config.FONT_SIZE_SMALL,
                                        config.TEXT_DIM, bold=True)
            size_col = self.text.render("РАЗМЕР", config.FONT_SIZE_SMALL,
                                        config.TEXT_DIM, bold=True)
            canvas.blit(name_col, (panel.x + 16, hdr_y))
            canvas.blit(size_col, (panel.right - 12 - size_col.get_width(), hdr_y))

            # список файлов
            item_h = 22
            visible = (panel.height - 60) // item_h
            start = max(0, min(self.rec_index, len(files) - visible)) if visible > 0 else 0
            end = min(len(files), start + visible)

            for i in range(start, end):
                f = files[i]
                y = panel.y + 66 + (i - start) * item_h
                active = (i == self.rec_index)
                if active:
                    canvas.blit(self._panel((panel.x + 8, y - 2,
                                             panel.width - 16, item_h),
                                            config.BUTTON_ACTIVE), (panel.x + 8, y - 2))
                name_color = config.TEXT_MAIN if active else config.TEXT_DIM
                name = self.text.render(f["name"], config.FONT_SIZE_NORMAL,
                                        name_color, bold=active)
                canvas.blit(name, (panel.x + 16, y))
                size_mb = f["size"] / (1024 * 1024)
                size_txt = "%.1f МБ" % size_mb
                size = self.text.render(size_txt, config.FONT_SIZE_NORMAL,
                                        config.TEXT_MAIN if active else config.TEXT_DIM)
                canvas.blit(size, (panel.right - 12 - size.get_width(), y))

            if len(files) > visible:
                hint = self.text.render("↑↓ — прокрутка   Del — удалить   Esc — назад",
                                        config.FONT_SIZE_SMALL, config.TEXT_DIM)
            else:
                hint = self.text.render("Del — удалить   Esc — назад",
                                        config.FONT_SIZE_SMALL, config.TEXT_DIM)
            canvas.blit(hint, (panel.centerx - hint.get_width() // 2,
                               panel.bottom - 24))

    # --- управление -------------------------------------------------------
    def handle_event(self, event):
        if event.type == pygame.KEYDOWN:
            self._on_key(event.key)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._press_pos = event.pos
            self._press_t = time.monotonic()
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._on_release(event.pos)

    def _on_key(self, key):
        if key == pygame.K_q:
            self.quit_requested = True
            return
        if self.screen == "menu":
            if key in (pygame.K_ESCAPE, pygame.K_b):
                self.screen = "live"
            elif key == pygame.K_UP:
                self.menu_index = (self.menu_index - 1) % len(self.menu_items)
            elif key == pygame.K_DOWN:
                self.menu_index = (self.menu_index + 1) % len(self.menu_items)
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                self._apply_bitrate()
            return
        if self.screen == "capture_mode":
            items = self.capture_mode_items
            if not items:
                self.screen = "live"
                return
            if key == pygame.K_ESCAPE:
                self.screen = "live"
            elif key == pygame.K_UP:
                self.capture_mode_index = max(0, self.capture_mode_index - 1)
            elif key == pygame.K_DOWN:
                self.capture_mode_index = min(len(items) - 1, self.capture_mode_index + 1)
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                self._apply_capture_mode()
            return
            files = self._list_recordings()
            if key == pygame.K_ESCAPE:
                self.screen = "live"
            elif key == pygame.K_UP and files:
                self.rec_index = max(0, self.rec_index - 1)
            elif key == pygame.K_DOWN and files:
                self.rec_index = min(len(files) - 1, self.rec_index + 1)
            elif key in (pygame.K_DELETE,) and files:
                self._delete_recording()
            return
        if self.screen == "diag":
            if key == pygame.K_ESCAPE:
                self.screen = "live"
            elif key == pygame.K_d:
                self._diag_t = 0.0
                self.notify("Пересканирование USB...")
            elif key == pygame.K_UP:
                self._diag["diag_scroll"] = max(0, self._diag.get("diag_scroll", 0) - 1)
            elif key == pygame.K_DOWN:
                self._diag["diag_scroll"] = self._diag.get("diag_scroll", 0) + 1
            return
        if key == pygame.K_ESCAPE:
            self.quit_requested = True
        elif key == pygame.K_r:
            self.toggle_record()
        elif key == pygame.K_b:
            self.open_menu()
        elif key == pygame.K_f:
            self.open_recordings()
        elif key == pygame.K_d:
            self.screen = "diag"

    def _on_release(self, pos):
        if self._press_pos is None:
            return
        dt = time.monotonic() - self._press_t
        dx = pos[0] - self._press_pos[0]
        dy = pos[1] - self._press_pos[1]
        self._press_pos = None
        if dt > config.SWIPE_MAX_TIME_S:
            return
        # Свайп вниз от левого верхнего угла — открыть меню режимов захвата
        if (abs(dy) >= config.SWIPE_MIN_DISTANCE and dy > 0
                and self._press_pos[0] < 90 and self._press_pos[1] < 70
                and self.screen == "live"):
            self.open_capture_mode_menu()
            return
        if abs(dy) >= config.SWIPE_MIN_DISTANCE:
            self._on_swipe(1 if dy > 0 else -1)
        elif abs(dx) <= config.CLICK_MAX_DISTANCE and abs(dy) <= config.CLICK_MAX_DISTANCE:
            self._on_tap(self.to_canvas(pos))

    def _on_swipe(self, direction):
        if self.screen == "menu":
            self.menu_index = (self.menu_index + direction) % len(self.menu_items)

    def _on_tap(self, pos):
        # Тап по верхней левой зоне (x < 90, y < 70) — открыть меню режимов захвата
        if pos[0] < 90 and pos[1] < 70 and self.screen == "live":
            self.open_capture_mode_menu()
            return
        if self.screen == "menu":
            rects = self._menu_item_rects()
            for i, rect in enumerate(rects):
                if rect.collidepoint(pos):
                    if i == self.menu_index:
                        self._apply_bitrate()
                    else:
                        self.menu_index = i
                    return
            if not MENU_RECT.collidepoint(pos):
                self.screen = "live"
            return
        if self.screen == "capture_mode":
            items = self.capture_mode_items
            if items:
                item_h = 30
                panel_top = 60
                for i in range(len(items)):
                    y = panel_top + i * item_h
                    if y <= pos[1] < y + item_h and pos[0] > 24 and pos[0] < config.CANVAS_WIDTH - 24:
                        self.capture_mode_index = i
                        self._apply_capture_mode()
                        return
            self.screen = "live"
            return
        if REC_BUTTON_RECT.collidepoint(pos):
            self.toggle_record()
        elif BITRATE_BUTTON_RECT.collidepoint(pos):
            self.open_menu()

    # --- действия -------------------------------------------------------
    def open_menu(self):
        self.screen = "menu"
        keys = [item[0] for item in self.menu_items]
        try:
            self.menu_index = keys.index(self.rec.bitrate_key)
        except ValueError:
            self.menu_index = 0

    def _apply_bitrate(self):
        key, label, value = self.menu_items[self.menu_index]
        self.rec.bitrate_key = key
        self.screen = "live"
        self.notify("Битрейт: %s (%s)" % (label, value))

    def open_recordings(self):
        self.screen = "recordings"
        self.rec_index = 0

    def open_capture_mode_menu(self):
        """Открыть меню выбора режима захвата (разрешение, FPS, формат)."""
        status = self.rec.status()
        if status["rec_state"] == config.REC_RUNNING:
            self.notify("Остановите запись перед сменой режима", ok=False)
            return
        # Собираем поддерживаемые режимы из v4l2_checker
        path = v4l2_checker.find_video_device()
        self.capture_mode_items = []
        if path:
            modes = v4l2_checker.list_modes(path, max_age=0)
            seen = set()
            for f, w, h, fps in modes:
                key = (f, w, h, round(fps, 2))
                if key in seen:
                    continue
                seen.add(key)
                fmt_label = "MJPG" if f.upper() == "MJPG" else "YUYV"
                label = "%s %dx%d@%g" % (fmt_label, w, h, fps)
                self.capture_mode_items.append((f, w, h, fps, label))
        if not self.capture_mode_items:
            # Фоллбэк — текущие настройки из config
            self.capture_mode_items = [
                (config.CAPTURE_FORMAT, config.CAPTURE_WIDTH,
                 config.CAPTURE_HEIGHT, config.CAPTURE_FPS,
                 "%s %dx%d@%g" % (config.CAPTURE_FORMAT,
                                  config.CAPTURE_WIDTH,
                                  config.CAPTURE_HEIGHT,
                                  config.CAPTURE_FPS))
            ]
        # Подсветка текущего режима
        self.capture_mode_index = 0
        for i, (f, w, h, fps, label) in enumerate(self.capture_mode_items):
            if (f == config.CAPTURE_FORMAT and w == config.CAPTURE_WIDTH
                    and h == config.CAPTURE_HEIGHT and abs(fps - config.CAPTURE_FPS) < 0.5):
                self.capture_mode_index = i
                break
        self.screen = "capture_mode"

    def _delete_recording(self):
        files = self._list_recordings()
        if not files or self.rec_index >= len(files):
            return
        f = files[self.rec_index]
        try:
            os.remove(f["path"])
            self.notify("Удалено: %s" % f["name"])
            files = self._list_recordings()
            self.rec_index = min(self.rec_index, max(0, len(files) - 1))
        except OSError as e:
            self.notify("Не удалось удалить: %s" % e, ok=False)

    def toggle_record(self):
        status = self.rec.status()
        if status["rec_state"] == config.REC_RUNNING:
            self.rec.stop_recording()
            self.notify("Запись остановлена")
        elif status["rec_state"] == config.REC_IDLE:
            ok, msg = self.rec.start_recording()
            self.notify(msg, ok)
        else:
            self.notify("Запись ещё закрывается...")

    # -----------------------------------------------------------------
    # Диагностика USB (экран "D")
    # -----------------------------------------------------------------
    def _refresh_diag_full(self):
        """Полное сканирование USB и видеоустройства (с вызовом v4l2-ctl)."""
        now = time.monotonic()
        if now - self._diag_t < 2.0:
            return
        self._diag_t = now
        dev = v4l2_checker.find_usb_device()
        self._diag["device"] = dev
        self._diag["usb"] = dev is not None
        path = v4l2_checker.find_video_device()
        self._diag["video"] = path
        if path:
            self._diag["modes"] = v4l2_checker.list_modes(path, max_age=0)
        else:
            self._diag["modes"] = []
        self._diag_scroll = 0   # сброс прокрутки при пересканировании

    def _draw_diag(self, canvas, status):
        """Экран диагностики USB: состояние свистка, устройство, режимы."""
        self._refresh_diag_full()
        dev = self._diag.get("device")
        video = self._diag.get("video")
        modes = self._diag.get("modes") or []
        usb = self._diag.get("usb", False)

        # Ключ кэша: состояние + режимы. Если ничего не изменилось — используем кэш.
        cache_key = (usb, dev.get("name") if dev else None, video, tuple(modes),
                     config.CAPTURE_FORMAT, config.CAPTURE_WIDTH,
                     config.CAPTURE_HEIGHT, config.CAPTURE_FPS)
        if cache_key == self._diag_cache_key and self._diag_lines_cache is not None:
            all_lines, all_colors, all_bold = self._diag_lines_cache
        else:
            all_lines, all_colors, all_bold = self._build_diag_lines(dev, video, modes, usb, status)
            self._diag_lines_cache = (all_lines, all_colors, all_bold)
            self._diag_cache_key = cache_key

        # затемняем видео под экраном
        overlay = pygame.Surface(canvas.get_size(), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        canvas.blit(overlay, (0, 0))

        panel = pygame.Rect(16, 24, config.CANVAS_WIDTH - 32, config.CANVAS_HEIGHT - 48)
        canvas.blit(self._panel(panel, config.PANEL_GRAPHITE_DARK,
                                config.PANEL_BORDER), (panel.x, panel.y))

        title = self.text.render("ДИАГНОСТИКА USB", config.FONT_SIZE_LARGE,
                                 config.SONY_ORANGE, bold=True)
        canvas.blit(title, (panel.x + 16, panel.y + 10))

        font = self.fonts.get(config.FONT_SIZE_NORMAL)
        max_text_w = panel.width - 100   # отступы + место под скроллбар справа (зазор >= 15 px)
        line_h = 17
        inner_top = panel.y + 40
        inner_bottom = panel.bottom - 24
        max_lines = (inner_bottom - inner_top) // line_h

        # Прокрутка: если строк больше чем помещается на экране
        total_lines = len(all_lines)
        scroll = self._diag.get("diag_scroll", 0)
        scroll = max(0, min(scroll, max(0, total_lines - max_lines)))
        self._diag["diag_scroll"] = scroll

        y = inner_top
        for i in range(scroll, min(scroll + max_lines, total_lines)):
            s = self.text.render(all_lines[i], config.FONT_SIZE_NORMAL,
                                 all_colors[i], all_bold[i])
            canvas.blit(s, (panel.x + 16, y))
            y += line_h

        # Индикатор прокрутки справа
        if total_lines > max_lines:
            pct = scroll / max(1, total_lines - max_lines)
            sb_y = inner_top + int(pct * (inner_bottom - inner_top - 20))
            sb_h = max(10, int((max_lines / total_lines) * (inner_bottom - inner_top)))
            pygame.draw.rect(canvas, (100, 100, 100),
                             (panel.right - 6, sb_y, 4, sb_h))

    def _build_diag_lines(self, dev, video, modes, usb, status):
        """Собирает строки диагностики, цвета и флажи bold. Вынесен из _draw_diag для кэширования."""
        font = self.fonts.get(config.FONT_SIZE_NORMAL)
        max_text_w = config.CANVAS_WIDTH - 200   # отступы + место под скроллбар (зазор >= 20 px)

        def add(text, color=config.TEXT_DIM, bold=False):
            # Длинные строки (пути, имена устройств) обрезаем, остальные — переносим по словам
            if text.startswith("/dev/") or text.startswith("usb-"):
                all_lines.append(self._fit_text(text, config.FONT_SIZE_NORMAL,
                                                max_text_w, bold=bold))
            else:
                for part in self._wrap_text(text, font, max_text_w):
                    all_lines.append(part)
            all_colors.append(color)
            all_bold.append(bold)

        all_lines = []
        all_colors = []
        all_bold = []

        if usb and dev:
            add("Свисток в USB: да (%s)" % dev.get("name", "?"),
                 config.SONY_ORANGE, bold=True)
        elif usb:
            add("Свисток в USB: да (устройство не определено)",
                 config.SONY_ORANGE, bold=True)
        else:
            add("Свисток в USB: НЕТ", config.REC_RED, bold=True)
        if dev:
            add("  Вид/Продукт: %s:%s  (%s)" % (dev.get("vid", "?"), dev.get("pid", "?"),
                                               dev.get("name", "?")))
        add("Видеоустройство: " + (os.path.basename(video) if video else "не найдено"))
        add("Текущий режим: %s %dx%d@%g" % (
            config.CAPTURE_FORMAT, config.CAPTURE_WIDTH,
            config.CAPTURE_HEIGHT, config.CAPTURE_FPS))
        add("")
        add("Поддерживаемые режимы:", config.TEXT_MAIN, bold=True)
        if modes:
            for f, w, h, fps in modes:
                add("  %s %dx%d @ %g" % (f, w, h, fps))
        else:
            add("  (не удалось получить — v4l2-ctl установлен?)", config.TEXT_DIM)
        add("")
        add("Состояние захвата: %s" % status.get("state", "?"),
             STATE_COLORS.get(status.get("state"), config.TEXT_DIM), bold=True)
        add("Запись: %s" % status.get("rec_state", "?"))
        if status.get("rec_file"):
            add("  Файл: %s" % os.path.basename(status["rec_file"]))
        add("")
        add("D — пересканировать   Esc — назад", config.TEXT_DIM)

        return all_lines, all_colors, all_bold

    def _max_diag_scroll(self):
        """Максимально допустимый diag_scroll для текущего кэша линий."""
        if self._diag_lines_cache is None:
            return 0
        total_lines = len(self._diag_lines_cache[0])
        max_lines = (config.CANVAS_HEIGHT - 72) // 17
        return max(0, total_lines - max_lines)

    def _clamp_diag_scroll(self):
        """Ограничить diag_scroll в допустимом диапазоне."""
        max_scroll = self._max_diag_scroll()
        self._diag["diag_scroll"] = max(0, min(self._diag.get("diag_scroll", 0), max_scroll))

    def _on_key_diag(self, key):
        if key == pygame.K_ESCAPE:
            self.screen = "live"
        elif key == pygame.K_d:
            self._diag_t = 0.0  # форсировать пересканирование
            self.notify("Пересканирование USB...")
        elif key == pygame.K_UP:
            self._diag["diag_scroll"] = max(0, self._diag.get("diag_scroll", 0) - 1)
            self._clamp_diag_scroll()
        elif key == pygame.K_DOWN:
            self._diag["diag_scroll"] = self._diag.get("diag_scroll", 0) + 1
            self._clamp_diag_scroll()
