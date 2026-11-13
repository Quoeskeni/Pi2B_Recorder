# -*- coding: utf-8 -*-
"""
video_view.py — кадр из GStreamer (Gst.Sample, I420) -> поверхность Pygame (RGB).

Захват отдаёт несжатый I420. Pygame умеет показывать только RGB/RGBA, поэтому
I420 переводится в RGB векторно (numpy). Без numpy модуль не работает — но он
всё равно нужен recorder.py для измерения яркости, так что зависимость уже есть.

Зачем отдельный модуль: чтобы ui.py не знал ни про GStreamer, ни про numpy —
интерфейс остаётся чисто Pygame-ным и проверяется локально на ПК.

Ещё умеет вписать кадр в прямоугольник холста с сохранением пропорций.
"""

import re
import logging

import numpy as np
import pygame

try:                     # GStreamer нужен только для типа Sample и флагов отображения
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    _HAVE_GST = True
except Exception:        # локальный запуск без GStreamer (отладка интерфейса)
    _HAVE_GST = False
    Gst = None

try:                     # GstVideo даёт правильные шаги плоскостей (stride)
    import gi
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo
    _HAVE_GST_VIDEO = True
except Exception:
    _HAVE_GST_VIDEO = False
    GstVideo = None

# width/height в строке caps GStreamer выглядят как "width=(int)720,height=(int)480"
_INT_SIZE_RE = re.compile(r"width=\(int\)(\d+),\s*height=\(int\)(\d+)")
_PLAIN_SIZE_RE = re.compile(r"width=(\d+),\s*height=(\d+)")


def caps_size(caps):
    """(width, height) из caps или (None, None), если разобрать не удалось."""
    s = caps.to_string() if hasattr(caps, "to_string") else str(caps)
    m = _INT_SIZE_RE.search(s) or _PLAIN_SIZE_RE.search(s)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def plane_info(caps, width, height):
    """
    (offsets, strides) для трёх плоскостей I420.

    Правильный источник — GstVideo.VideoInfo (у videoconvert строки могут быть
    выровнены шире кадра). Если GstVideo недоступен — считаем, что кадр плотный
    (stride = ширина), как и предполагает _measure_luma в recorder.py.
    """
    if _HAVE_GST_VIDEO:
        try:
            vinfo = GstVideo.VideoInfo.new_from_caps(caps)
            if vinfo.width == width and vinfo.height == height:
                return tuple(vinfo.offset), tuple(vinfo.stride)
        except Exception:
            pass
    y = width * height
    c = (width // 2) * (height // 2)
    return (0, y, y + c), (width, width // 2, width // 2)


def _read_plane(data, offset, stride, width, height):
    """Одна плоскость I420 из буфера в виде массива (height, width)."""
    if stride == width:      # обычный случай: строки идут подряд
        return np.frombuffer(data, dtype=np.uint8, count=width * height,
                             offset=offset).reshape(height, width)
    rows = [np.frombuffer(data, dtype=np.uint8, count=width,
                          offset=offset + y * stride) for y in range(height)]
    return np.stack(rows, axis=0)


def i420_to_rgb(data, width, height, offsets, strides):
    """
    Несжатый I420 -> RGB (массив height x width x 3, uint8).
    Формула BT.601 (PAL, как у нашего свистка). Цветовые плоскости вдвое мельче
    яркостной — раздвигаем их до размера кадра.
    """
    y = _read_plane(data, offsets[0], strides[0], width, height).astype(np.float32)
    u = _read_plane(data, offsets[1], strides[1], width // 2, height // 2).astype(np.float32)
    v = _read_plane(data, offsets[2], strides[2], width // 2, height // 2).astype(np.float32)
    u = u.repeat(2, axis=0).repeat(2, axis=1)
    v = v.repeat(2, axis=0).repeat(2, axis=1)

    r = y + 1.402 * (v - 128.0)
    g = y - 0.344136 * (u - 128.0) - 0.714136 * (v - 128.0)
    b = y + 1.772 * (u - 128.0)

    rgb = np.empty((height, width, 3), dtype=np.uint8)
    np.clip(r, 0.0, 255.0, out=r)
    np.clip(g, 0.0, 255.0, out=g)
    np.clip(b, 0.0, 255.0, out=b)
    rgb[:, :, 0] = r
    rgb[:, :, 1] = g
    rgb[:, :, 2] = b
    return rgb


def frame_to_surface(width, height, data, offsets=None, strides=None):
    """
    I420 (bytes) -> pygame.Surface (RGB). Не требует GStreamer — годится для тестов.

    data — буфер целиком (все три плоскости), offsets/strides — из plane_info.
    """
    if offsets is None or strides is None:
        _offsets, _strides = plane_info(None, width, height)
        offsets = offsets if offsets is not None else _offsets
        strides = strides if strides is not None else _strides
    rgb = i420_to_rgb(data, width, height, offsets, strides)
    try:
        return pygame.image.frombuffer(rgb, (width, height), "RGB")
    except (TypeError, ValueError):
        return pygame.image.frombuffer(rgb.tobytes(), (width, height), "RGB")


def sample_to_surface(sample):
    """
    Gst.Sample (RGB или I420) -> pygame.Surface (RGB).

    Кадр отдаётся со съёмника только один раз (appsink max-buffers=1 drop=true,
    его забирают через try-pull-sample), поэтому каждый вызов — это новый кадр.

    ВАЖНО: данные из mapinfo.data копируются в байты ПЕРЕД buf.unmap(),
    иначе np.frombuffer получит ValueError на освобождённой памяти
    (см. отчёт об ошибке "operation forbidden on released memoryview").
    """
    if sample is None or not _HAVE_GST:
        return None
    caps = sample.get_caps()
    width, height = caps_size(caps)
    if not width or not height:
        return None

    # Определяем формат из caps через GStreamer API (не через строку!).
    # caps.to_string() даёт "format=(string)RGB", где \w+ не Matches "(string)",
    # поэтому fmt оставался None и RGB-данные дешифровались как I420.
    fmt = None
    caps_str = ""
    if _HAVE_GST:
        try:
            struct = caps.get_structure(0)
            if struct is not None:
                fmt = struct.get_value("format")
                caps_str = str(caps)
        except Exception:
            pass
    if fmt is None:
        # фоллбэк: парсим строку caps
        caps_str = caps.to_string() if hasattr(caps, "to_string") else str(caps)
        m = re.search(r"format=(\w+)", caps_str)
        if m:
            fmt = m.group(1)

    buf = sample.get_buffer()
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return None
    try:
        data = bytes(mapinfo.data)
        # Для RGB есть stride-выравнивание строк (выравнивание до 4 байт).
        # Если len(data) > width * height * 3 — обрезаем лишние байты в конце строки
        # простым срезом NumPy, без GstVideo.VideoFrame (он возвращает кортеж, а не объект,
        # и не вызывать .unmap() ведёт к утечке +20.5 МБ каждые 2 секунды).
        if fmt == "RGB" and len(data) > width * height * 3:
            import numpy as np
            arr = np.frombuffer(data, dtype=np.uint8)
            # reshape в (height, stride_bytes) и обрезаем до width*3
            row_bytes = len(data) // height
            arr = arr.reshape(height, row_bytes)[:, :width * 3]
            data = arr.tobytes()
    finally:
        buf.unmap(mapinfo)

    # Логируем реальные caps и размер данных при первом кадре (один раз).
    if not hasattr(sample_to_surface, "_logged"):
        sample_to_surface._logged = True
        log_ = logging.getLogger("video_view")
        log_.info("Первый кадр preview: caps=%s, fmt=%s, size=%dx%d, "
                  "len(data)=%d, expected=%d",
                  caps_str, fmt, width, height, len(data), width * height * 3)

    if fmt == "RGB":
        # GStreamer уже сделал конвертацию I420->RGB — берём готовые байты.
        # Если stride был больше width, данные уже обрезаны через VideoFrame.
        try:
            return pygame.image.frombuffer(data, (width, height), "RGB")
        except (TypeError, ValueError):
            return pygame.image.frombuffer(data, (width, height), "RGB")
    else:
        # I420 (или другой) — конвертируем через NumPy (старая логика)
        # Для I420 ширина/высота должны быть чётными
        if width % 2 or height % 2:
            return None
        offsets, strides = plane_info(caps, width, height)
        return frame_to_surface(width, height, data, offsets, strides)


class VideoView:
    """
    Хранит последний кадр и рисует его в прямоугольнике холста.

    Поддерживает два режима вывода:
      1. Софтовый pygame.Surface (для ui_selftest.py и фоллбэка).
      2. GPU-ускорение через SDL2 Texture (VideoCore IV / OpenGL ES),
         когда передан renderer и video_tex.

    Использование из главного цикла:
        video.update(rec.pull_preview_sample())   # каждый кадр цикла
        video.draw(canvas, config.video_rect_on_canvas())
    """

    def __init__(self, use_gpu=False, renderer=None, video_tex=None):
        self.surface = None          # pygame.Surface для софтового режима
        self.texture = None          # SDL2 Texture для GPU-режима
        self._renderer = renderer    # SDL2 Renderer
        self._use_gpu = use_gpu and renderer is not None
        self._scaled = {}            # (size_исходника, size_цели) -> Surface
        # Привязываем внешнюю текстуру видео (создаётся в main.py)
        if video_tex is not None:
            self.texture = video_tex

    def update(self, sample):
        """sample — Gst.Sample или None. Возвращает True, если поверхность обновилась."""
        if sample is None:
            return False
        surf = sample_to_surface(sample)
        if surf is not None:
            self.surface = surf
            self._scaled.clear()   # масштабированная копия устарела
            return True
        return False

    def update_gpu_texture(self):
        """Обновить SDL2 Texture из текущего pygame.Surface (видеокадр)."""
        if self._use_gpu and self.texture is not None and self.surface is not None:
            try:
                self.texture.update(self.surface)
            except Exception as e:
                logging.getLogger("video_view").debug(
                    "Не удалось обновить GPU-текстуру видео: %s", e)

    def draw(self, canvas, rect):
        """
        Вписать кадр в rect (x, y, w, h) с сохранением пропорций.
        Возвращает True, если что-то нарисовали.
        Софтовый режим: рисует на pygame.Surface canvas.
        """
        if self.surface is None:
            return False
        dst = pygame.Rect(rect)
        src_size = self.surface.get_size()
        scale = min(dst.width / src_size[0], dst.height / src_size[1])
        size = (max(1, int(round(src_size[0] * scale))),
                max(1, int(round(src_size[1] * scale))))
        key = (src_size, size)
        scaled = self._scaled.get(key)
        if scaled is None:
            scaled = pygame.transform.scale(self.surface, size)
            if len(self._scaled) > 4:
                self._scaled.clear()
            self._scaled[key] = scaled
        canvas.blit(scaled, (dst.x + (dst.width - size[0]) // 2,
                             dst.y + (dst.height - size[1]) // 2))
        return True

    def draw_gpu(self, renderer):
        """
        Нарисовать видео-текстуру на SDL2 Renderer (GPU-режим).
        Вызывается из main.py вместо soft draw().
        """
        if not self._use_gpu or self.texture is None:
            return False
        try:
            renderer.draw(self.texture, None, (0, 0,
                                               config.CANVAS_WIDTH,
                                               config.CANVAS_HEIGHT))
            return True
        except Exception as e:
            logging.getLogger("video_view").debug(
                "Не удалось нарисовать GPU-текстуру видео: %s", e)
            return False
