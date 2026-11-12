# -*- coding: utf-8 -*-
"""
recorder.py — захват видео со свистка и запись в файл (GStreamer).

КАК УСТРОЕНО (два независимых "конвейера" GStreamer):

1) Конвейер ЗАХВАТА работает всё время, пока подключён свисток:

   свисток -> распаковка MJPEG -> I420 -> разветвитель (tee) --+--> ПРЕВЬЮ (на экран)
                                                               |
                                                               +--> ДЛЯ ЗАПИСИ (appsink)

   Свисток можно открыть только ОДНОЙ программой, поэтому поток открывается
   один раз и раздваивается: одна копия идёт на экран, другая — в запись.

2) Конвейер ЗАПИСИ создаётся по кнопке REC и уничтожается по STOP:

   appsrc (кадры из захвата) -> аппаратный H.264 -> MKV -> файл

   Почему отдельно: чтобы MKV закрылся правильно, в конвейер записи нужно
   послать сигнал "конец потока" (EOS) и дождаться, пока он дойдёт до файла.
   В конвейере записи нет декодера v4l2jpegdec (он зависает на EOS, проверено
   на Pi), а превью при остановке записи не прерывается ни на секунду.
   Если упадёт энкодер — упадёт только запись, превью продолжит работать.

ОТКАЗОУСТОЙЧИВОСТЬ:
   * свистка нет при запуске         -> состояние NO_DEVICE, повтор каждую секунду;
   * свисток выдернули во время работы -> ошибка ловится, запись закрывается,
                                         захват ждёт возвращения свистка;
   * кадры перестали приходить        -> через NO_FRAMES_TIMEOUT_S захват перезапускается.

КАК ПОЛЬЗОВАТЬСЯ ИЗ ПРОГРАММЫ (main.py):
   rec = Recorder()
   каждый кадр главного цикла:  rec.poll()
   кнопка REC:                  rec.start_recording()  /  rec.stop_recording()
   для экрана:                  rec.status()  и  rec.pull_preview_sample()
   при выходе:                  rec.close()

САМОПРОВЕРКА НА PI (gst-launch при этом должен быть остановлен):
   python3 recorder.py        — превью на HDMI + запись 10 секунд
   python3 recorder.py 60     — то же, но запись 60 секунд
                                (можно выдернуть свисток посреди записи)
   python3 recorder.py 0      — без записи, только превью и состояние каждую
                                секунду (для подбора порогов "нет сигнала")
   Выход: Ctrl+C.
"""

import logging
import os
import re
import shutil
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402  (импорт после require_version — так положено)

import config          # noqa: E402
import v4l2_checker    # noqa: E402

log = logging.getLogger("recorder")

# Состояния захвата и записи общие с интерфейсом — лежат в config.py,
# чтобы ui.py не пришлось импортировать GStreamer. Старые имена оставлены
# как псевдонимы: их использует самопроверка и внешние скрипты.
NO_DEVICE = config.STATE_NO_DEVICE    # свистка нет — показываем диагностику
STARTING = config.STATE_STARTING      # запускаем захват, кадров ещё нет
LIVE = config.STATE_LIVE              # кадры идут
NO_SIGNAL = config.STATE_NO_SIGNAL    # кадры идут, но они чёрные (провод камеры выдернут)

REC_IDLE = config.REC_IDLE            # не пишем
REC_RUNNING = config.REC_RUNNING      # пишем
REC_STOPPING = config.REC_STOPPING    # закрываем файл (ждём EOS)

# Имена состояний конвейера GStreamer — для диагностики "нет кадров"
_GST_STATE_NAMES = {
    Gst.State.VOID_PENDING: "VOID_PENDING",
    Gst.State.NULL: "NULL",
    Gst.State.READY: "READY",
    Gst.State.PAUSED: "PAUSED",
    Gst.State.PLAYING: "PLAYING",
}


# =====================================================================
# Строки описания конвейеров (на языке gst-launch)
# =====================================================================
def _par_string():
    """Форма пикселя для записи в файл, например '8/9'."""
    num, den = config.pixel_aspect_ratio()
    return "%d/%d" % (num, den)


def _source_desc(device):
    """Источник видео: свисток + нужный формат."""
    # ФPS может быть дробным (59.94 NTSC). GStreamer хочет framerate=num/den.
    fps = float(config.CAPTURE_FPS)
    if abs(fps - round(fps)) < 0.01:
        fps_num, fps_den = int(round(fps)), 1
    else:
        # 59.94 -> 60000/1001, 29.97 -> 30000/1001
        fps_num = int(round(fps * 10000))
        fps_den = 10000
        # Сократим дробь
        import math
        g = math.gcd(fps_num, fps_den)
        fps_num //= g
        fps_den //= g
    caps_common = "width=%d,height=%d,framerate=%d/%d" % (
        config.CAPTURE_WIDTH, config.CAPTURE_HEIGHT, fps_num, fps_den)
    if config.CAPTURE_FORMAT == "MJPG":
        return 'v4l2src device="%s" ! image/jpeg,%s' % (device, caps_common)
    return 'v4l2src device="%s" ! video/x-raw,format=YUY2,%s' % (device, caps_common)


def _decode_desc(decoder=None):
    """Превращение кадров со свистка в несжатый I420 (его понимают и экран, и энкодер)."""
    if config.CAPTURE_FORMAT == "MJPG":
        return "%s ! videoconvert ! video/x-raw,format=I420" % (
            decoder or config.JPEG_DECODER)
    return "videoconvert ! video/x-raw,format=I420"


# Очередь, которая при перегрузке выбрасывает старые кадры, а не тормозит поток
_LEAKY_QUEUE = "queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 leaky=downstream"


def capture_pipeline_desc(device, preview, decoder=None):
    """
    Полное описание конвейера захвата.
    preview: "appsink" — кадры забирает программа (для pykms/pygame),
             "kmssink" — сразу на HDMI (только для самопроверки),
             "none"    — превью нет.
    decoder: чем распаковывать MJPEG (по умолчанию config.JPEG_DECODER).
    """
    rec_sink = ("appsink name=rec_sink emit-signals=true sync=false "
                "max-buffers=10 drop=true")
    if preview == "appsink":
        # preview_sink: RGB — готовый формат для pygame, без конвертации на Python.
        # Для записи (rec_sink) оставляем I420 — он нужен энкодеру.
        prev_sink = ("videoconvert ! video/x-raw,format=RGB "
                     "! appsink name=preview_sink sync=false max-buffers=1 drop=true")
    elif preview == "kmssink":
        prev_sink = "kmssink sync=false"
    else:
        prev_sink = "fakesink sync=false"

    src = _source_desc(device)
    decode = _decode_desc(decoder)
    if config.RECORD_MODE == "mjpeg_copy":
        # Разветвляем ещё СЖАТЫЙ поток: в запись идёт MJPEG как есть.
        return (
            "%s ! tee name=jt "
            "jt. ! %s ! %s "
            "jt. ! queue max-size-buffers=3 ! %s ! tee name=vt "
            "vt. ! %s ! %s"
            % (src, _LEAKY_QUEUE, rec_sink, decode, _LEAKY_QUEUE, prev_sink)
        )
    # Режим h264: разветвляем уже распакованный поток.
    # Deinterlace для UTV007: чересстрочный YUYV -> прогрессивный кадр.
    dev = v4l2_checker.find_usb_device()
    deinterlace = ""
    if dev and dev["vid"] == "1b71" and dev["pid"] == "3002":
        deinterlace = "deinterlace fields=all method=linear ! "
    return (
        "%s ! queue max-size-buffers=3 ! %s ! %s tee name=vt "
        "vt. ! %s ! %s "
        "vt. ! %s ! %s"
        % (src, decode, deinterlace, _LEAKY_QUEUE, prev_sink, _LEAKY_QUEUE, rec_sink)
    )


def encoder_desc(bitrate_bps):
    """Аппаратный H.264 с нужным битрейтом (эта же строка проверена в CHECKS.md, шаг 7)."""
    return (
        'v4l2h264enc extra-controls="controls,video_bitrate=%d,'
        'h264_i_frame_period=%d,repeat_sequence_header=1" '
        '! video/x-h264,level=(string)%s ! h264parse'
        % (bitrate_bps, config.H264_KEYFRAME_INTERVAL, config.H264_LEVEL)
    )


def record_pipeline_desc(filename, bitrate_bps):
    """Описание конвейера записи. Формат кадров (caps) appsrc получает позже, из захвата."""
    head = ("appsrc name=src is-live=true format=time do-timestamp=false "
            "max-bytes=20000000 block=false ! queue max-size-buffers=50 max-size-time=0 max-size-bytes=0")
    if config.RECORD_MODE == "mjpeg_copy":
        # MJPEG пишется как есть (jpegparse не ставим: он теряет форму пикселя)
        return '%s ! matroskamux ! filesink location="%s" sync=false' % (head, filename)
    return '%s ! %s ! matroskamux ! filesink location="%s" sync=false' % (
        head, encoder_desc(bitrate_bps), filename)


def free_space_mb(path=None):
    """Свободное место на диске с записями, в мегабайтах (None, если папки нет)."""
    path = os.path.abspath(path or config.VIDEO_DIR)
    while not os.path.exists(path) and path != os.path.dirname(path):
        path = os.path.dirname(path)    # папки ещё нет — смотрим диск, где она появится
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return None


def _set_null_in_background(pipeline, name):
    """
    Остановить конвейер в отдельном потоке.
    Зачем: если элемент завис (как v4l2jpegdec на EOS), остановка тоже может
    повиснуть — пусть висит отдельный поток, а не вся программа.
    """
    def worker():
        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception as e:      # на всякий случай: в фоне ничего не должно падать
            log.warning("Ошибка при остановке %s: %s", name, e)
    t = threading.Thread(target=worker, name="stop-" + name, daemon=True)
    t.start()
    return t


# =====================================================================
# Главный класс
# =====================================================================
class Recorder:
    def __init__(self, preview="appsink"):
        Gst.init(None)
        self.preview_mode = preview
        self._preview_failed = False   # kmssink упал — переключимся на appsink

        # --- захват ---
        self.state = NO_DEVICE
        self.message = "Запуск..."
        self.device = None
        self._capture = None            # конвейер захвата
        self._capture_bus = None
        self._preview_sink = None
        self._decoder = config.JPEG_DECODER   # чем распаковывать MJPEG (может смениться fallback)
        self._capture_started_t = 0.0
        self._next_retry_t = 0.0
        self._last_frame_t = 0.0
        self._frame_counter = 0
        self._fps = 0.0
        self._fps_counter = 0
        self._fps_t = time.monotonic()
        self.luma_mean = None           # средняя яркость кадра 0..255
        self.luma_std = None            # разброс яркости
        self._rec_caps = None           # формат кадров для записи (из захвата)
        self._last_error = None         # чтобы не засорять журнал одинаковыми ошибками

        # --- запись ---
        self.rec_state = REC_IDLE
        self.rec_file = None
        self.rec_message = ""
        self.bitrate_key = config.DEFAULT_BITRATE
        self._record = None             # конвейер записи
        self._record_bus = None
        self._appsrc = None
        self._rec_lock = threading.Lock()   # appsrc трогают два потока
        self._rec_t0_pts = None
        self._rec_start_t = 0.0
        self._rec_elapsed_final = 0.0
        self._stop_deadline = 0.0
        self._last_size = 0
        self._last_size_t = 0.0
        self.measured_bps = 0.0
        self._free_mb = free_space_mb()
        self._status_t = 0.0

    # -----------------------------------------------------------------
    # Захват
    # -----------------------------------------------------------------
    def _start_capture(self):
        # Если предыдущий конвейер ещё не переведён в NULL (например, kmssink
        # упал с ошибкой DRM), сначала гарантированно освобождаем устройство,
        # иначе следующий запуск упадёт с "Device is busy".
        if self._capture is not None:
            self._force_null_capture()

        # modes_max_age=1.0: v4l2-ctl — внешняя программа, не запускаем его
        # чаще раза в секунду (иначе главный цикл подтормаживает).
        path, info = v4l2_checker.check_device(modes_max_age=1.0)
        if path is None:
            msg = info["message"]
            if self.state != NO_DEVICE or msg != self.message:
                log.info(msg)
            self.state = NO_DEVICE
            self.message = msg
            return

        # config.CAPTURE_WIDTH/HEIGHT/FPS могли быть скорректированы
        # v4l2_checker под реальные возможности устройства.
        desc = capture_pipeline_desc(path, self.preview_mode, self._decoder)
        log.log(logging.DEBUG if self._last_error else logging.INFO, "Запуск захвата: %s", desc)
        try:
            pipeline = Gst.parse_launch(desc)
        except Exception as e:      # GLib.Error: элемента нет, опечатка в описании и т.п.
            self.state = NO_DEVICE
            self.message = "Не удалось создать конвейер захвата: %s" % e
            log.error(self.message)
            return

        # Счётчик кадров + яркость: "датчик" на входе разветвителя vt
        tee = pipeline.get_by_name("vt")
        tee.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_frame_probe)

        pipeline.get_by_name("rec_sink").connect("new-sample", self._on_rec_sample)
        self._preview_sink = pipeline.get_by_name("preview_sink")   # None, если превью не appsink

        self._capture = pipeline
        self._capture_bus = pipeline.get_bus()
        self.device = path
        self._capture_started_t = time.monotonic()
        self._last_frame_t = 0.0
        self._rec_caps = None
        self.state = STARTING
        self.message = "Запуск захвата: " + path + " (%dx%d@%g)" % (
            config.CAPTURE_WIDTH, config.CAPTURE_HEIGHT, config.CAPTURE_FPS)

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            # Подробности придут ошибкой на шину, разберёмся в poll()
            log.debug("Конвейер захвата не перешёл в PLAYING")

    def _stop_capture(self, reason, no_frames=False):
        """Остановить захват (если пишем — сначала закрыть запись)."""
        if self.rec_state == REC_RUNNING:
            self.stop_recording()
        if self._capture is not None and reason != self._last_error:
            log.info("Остановка захвата: %s", reason)
            t = _set_null_in_background(self._capture, "capture")
            t.join(3.0)    # ждём, пока v4l2src освободит устройство (макс 3 с),
                           # иначе следующий запуск упадёт с "Device is busy"
        self._capture = None
        self._capture_bus = None
        self._preview_sink = None
        self.state = NO_DEVICE
        self.message = reason
        self.luma_mean = self.luma_std = None
        self._fps = 0.0
        # При ошибке "busy" устройство занято другой программой —
        # ждём дольше перед следующей попыткой, иначе будем падать каждую секунду.
        if "busy" in reason.lower():
            self._next_retry_t = time.monotonic() + 5.0
        else:
            self._next_retry_t = time.monotonic() + config.DEVICE_RETRY_INTERVAL_S
        if no_frames:
            self._try_decoder_fallback()

    def _try_decoder_fallback(self):
        """
        Кадров нет — переключить распаковку MJPEG на запасной декодер.
        Помогает, если v4l2jpegdec не смог занять аппаратный M2M-кодек Pi:
        конвейер при этом остаётся в PLAYING, но кадров нет (см. журнал).
        Включается в config.py: JPEG_DECODER_FALLBACK.
        """
        if not config.JPEG_DECODER_FALLBACK or config.CAPTURE_FORMAT != "MJPG":
            return
        if self._decoder == "v4l2jpegdec":
            log.warning("Декодер v4l2jpegdec не дал кадров — "
                        "следующий запуск захвата с программным jpegdec")
            self._decoder = "jpegdec"

    def _pipeline_state_name(self):
        """Короткое имя состояния конвейера захвата — для диагностики."""
        if self._capture is None:
            return "нет"
        try:
            _ok, state, _pending = self._capture.get_state(0)
            return _GST_STATE_NAMES.get(state, str(state))
        except Exception as e:
            return "неизвестно (%s)" % e

    def _force_null_capture(self):
        """
        Принудительно перевести конвейер захвата в NULL.
        Вызывается при закрытии, чтобы освободить устройство即使 если
        предыдущий конвейер упал без корректной остановки (см. отчёт об ошибке
        "Device is busy").
        """
        if self._capture is None:
            return
        try:
            self._capture.set_state(Gst.State.NULL)
        except Exception as e:
            log.warning("Не удалось перевести конвейер захвата в NULL: %s", e)
        self._capture = None
        self._capture_bus = None
        self._preview_sink = None

    def _on_frame_probe(self, pad, info):
        """Вызывается GStreamer на КАЖДЫЙ кадр (в его потоке). Должно быть быстрым."""
        self._last_frame_t = time.monotonic()
        self._frame_counter += 1
        self._fps_counter += 1
        # Раз в секунду меряем яркость (это дороже, поэтому не каждый кадр)
        if self._frame_counter % config.CAPTURE_FPS == 1:
            self._measure_luma(info.get_buffer())
        return Gst.PadProbeReturn.OK

    def _measure_luma(self, buf):
        """Средняя яркость кадра по плоскости Y (первые ширина*высота байт буфера I420)."""
        try:
            import numpy as np
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                return
            try:
                n = config.CAPTURE_WIDTH * config.CAPTURE_HEIGHT
                y = np.frombuffer(mapinfo.data, dtype=np.uint8, count=n)[::61]
                self.luma_mean = float(y.mean())
                self.luma_std = float(y.std())
            finally:
                buf.unmap(mapinfo)
        except Exception as e:
            log.debug("Не удалось измерить яркость: %s", e)

    def _handle_capture_bus(self):
        while self._capture_bus is not None:
            msg = self._capture_bus.pop()
            if msg is None:
                return
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                text = str(err.message)
                # Одна и та же ошибка при каждой попытке — подробно пишем в журнал один раз
                level = logging.DEBUG if text == self._last_error else logging.ERROR
                log.log(level, "Ошибка захвата: %s (%s)", text, debug)
                self._last_error = text
                # Если kmssink упал с DRM/Permission denied — переключаем превью
                # на appsink и перезапускаем захват, чтобы не висеть в busy.
                if ("permission denied" in text.lower()
                        or "drmmodesetplane" in text.lower()
                        or "kmssink" in text.lower()):
                    if self.preview_mode == "kmssink" and not self._preview_failed:
                        log.warning("kmssink упал (%s) — переключаю превью на appsink", text)
                        self._preview_failed = True
                        self.preview_mode = "appsink"
                if "busy" in text.lower():
                    text = "Свисток занят другой программой (закройте gst-launch)"
                self._stop_capture(text)
                return
            if msg.type == Gst.MessageType.EOS:
                self._stop_capture("Поток со свистка закончился")
                return
            if msg.type == Gst.MessageType.WARNING:
                w, _ = msg.parse_warning()
                log.warning("Предупреждение захвата: %s", w.message)

    def _check_capture_health(self, now):
        if self._capture is None:
            return
        # Кадры перестали приходить (или так и не пошли)?
        last = self._last_frame_t or self._capture_started_t
        limit = config.NO_FRAMES_TIMEOUT_S + (2.0 if not self._last_frame_t else 0.0)
        if now - last > limit:
            # Состояние конвейера подсказывает, где проблема:
            # PAUSED — не прошло preroll/согласование, PLAYING — источник молчит.
            log.info("Состояние конвейера захвата: %s (нет кадров %.0f с)",
                     self._pipeline_state_name(), now - last)
            self._stop_capture("Нет кадров %.0f с — перезапуск захвата" % (now - last),
                               no_frames=True)
            return
        if not self._last_frame_t or self._rec_caps is None:
            return          # ещё STARTING (ждём первый кадр в обеих ветках)
        # Кадры идут: LIVE или "нет сигнала" (чёрная картинка)
        black = (config.NO_SIGNAL_ON_BLACK and self.luma_mean is not None
                 and self.luma_mean < config.BLACK_FRAME_LUMA_MAX
                 and self.luma_std < config.BLACK_FRAME_STD_MAX)
        new_state = NO_SIGNAL if black else LIVE
        if new_state != self.state:
            log.info("Состояние захвата: %s", new_state)
            self._last_error = None
        self.state = new_state
        self.message = "Сигнал есть" if new_state == LIVE else "Чёрный кадр — камера не подключена?"

    # -----------------------------------------------------------------
    # Запись
    # -----------------------------------------------------------------
    def _on_rec_sample(self, sink):
        """Вызывается на каждый кадр ветки записи (в потоке GStreamer)."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        if self._rec_caps is None:
            self._rec_caps = sample.get_caps().to_string()
        with self._rec_lock:
            appsrc = self._appsrc
            if appsrc is None:
                return Gst.FlowReturn.OK       # не пишем — кадр просто пропускаем
            src_buf = sample.get_buffer()
            buf = src_buf.copy()               # копия "обёртки", сами данные не копируются
            # Время в файле начинается с нуля
            if self._rec_t0_pts is None:
                self._rec_t0_pts = src_buf.pts
            if src_buf.pts != Gst.CLOCK_TIME_NONE and self._rec_t0_pts != Gst.CLOCK_TIME_NONE:
                buf.pts = max(0, src_buf.pts - self._rec_t0_pts)
            buf.dts = Gst.CLOCK_TIME_NONE
            appsrc.emit("push-buffer", buf)
        return Gst.FlowReturn.OK

    def _caps_for_record(self):
        """Формат кадров из захвата + правильная форма пикселя для плееров."""
        caps = self._rec_caps
        if "pixel-aspect-ratio" in caps:
            caps = re.sub(r"pixel-aspect-ratio=\(fraction\)[\d/]+",
                          "pixel-aspect-ratio=(fraction)" + _par_string(), caps)
        else:
            caps += ", pixel-aspect-ratio=(fraction)" + _par_string()
        return Gst.Caps.from_string(caps)

    def start_recording(self, bitrate_key=None):
        """Начать запись. Возвращает (True/False, сообщение)."""
        if bitrate_key:
            self.bitrate_key = bitrate_key
        if self.rec_state != REC_IDLE:
            return False, "Запись уже идёт или ещё закрывается"
        if self.state not in (LIVE, NO_SIGNAL) or self._rec_caps is None:
            return False, "Нет видео со свистка — записывать нечего"
        try:
            os.makedirs(config.VIDEO_DIR, exist_ok=True)
        except OSError as e:
            return False, "Не удалось создать папку %s: %s" % (config.VIDEO_DIR, e)
        free = free_space_mb()
        if free is None or free < config.MIN_FREE_SPACE_MB:
            self.rec_message = config.TEXT_LOW_SPACE
            return False, "Мало места: %s МБ (нужно не меньше %d)" % (
                "?" if free is None else int(free), config.MIN_FREE_SPACE_MB)

        filename = os.path.join(config.VIDEO_DIR, time.strftime(config.FILENAME_PATTERN))
        bps = config.BITRATE_PRESETS[self.bitrate_key]["bps"]
        desc = record_pipeline_desc(filename, bps)
        log.info("Запуск записи: %s", desc)
        try:
            pipeline = Gst.parse_launch(desc)
            appsrc = pipeline.get_by_name("src")
            appsrc.set_property("caps", self._caps_for_record())
        except Exception as e:
            return False, "Не удалось создать конвейер записи: %s" % e

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            _set_null_in_background(pipeline, "record")
            return False, "Энкодер не запустился (подробности в camcorder.log)"

        self._record = pipeline
        self._record_bus = pipeline.get_bus()
        self.rec_file = filename
        self.rec_message = ""
        self._rec_start_t = time.monotonic()
        self._last_size = 0
        self._last_size_t = self._rec_start_t
        self.measured_bps = 0.0
        with self._rec_lock:
            self._rec_t0_pts = None
            self._appsrc = appsrc            # с этого момента кадры идут в запись
        self.rec_state = REC_RUNNING
        log.info("Запись начата: %s (%s)", filename, config.BITRATE_PRESETS[self.bitrate_key]["label"])
        return True, "Запись: " + os.path.basename(filename)

    def stop_recording(self):
        """Остановить запись. Файл закрывается в фоне за доли секунды (см. poll())."""
        if self.rec_state != REC_RUNNING:
            return
        with self._rec_lock:
            appsrc = self._appsrc
            self._appsrc = None              # новые кадры в запись больше не идут
        self._rec_elapsed_final = time.monotonic() - self._rec_start_t
        if appsrc is not None:
            appsrc.emit("end-of-stream")     # "конец потока" — MKV допишет оглавление
        self.rec_state = REC_STOPPING
        self._stop_deadline = time.monotonic() + config.RECORD_STOP_TIMEOUT_S
        log.info("Остановка записи, ждём закрытия файла...")

    def _finish_record(self, how):
        """Окончательно освободить конвейер записи."""
        if self._record is not None:
            _set_null_in_background(self._record, "record")
        self._record = None
        self._record_bus = None
        with self._rec_lock:
            self._appsrc = None
        size = 0
        try:
            size = os.path.getsize(self.rec_file)
        except (OSError, TypeError):
            pass
        log.info("Запись закончена (%s): %s, %.1f МБ", how, self.rec_file, size / 1e6)
        self.rec_state = REC_IDLE

    def _handle_record_bus(self, now):
        if self._record_bus is None:
            return
        while True:
            msg = self._record_bus.pop()
            if msg is None:
                break
            if msg.type == Gst.MessageType.EOS:
                self._finish_record("файл закрыт правильно")
                return
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                log.error("Ошибка записи: %s (%s)", err.message, debug)
                self.rec_message = "Ошибка записи"
                if self.rec_state == REC_RUNNING:
                    self._rec_elapsed_final = now - self._rec_start_t
                self._finish_record("ошибка")
                return
        if self.rec_state == REC_STOPPING and now > self._stop_deadline:
            # EOS не дошёл вовремя. Файл всё равно читается, просто без оглавления.
            self._finish_record("не дождались EOS за %.0f с" % config.RECORD_STOP_TIMEOUT_S)

    def _update_record_stats(self, now):
        """Раз в секунду: реальный битрейт файла и свободное место."""
        if now - self._status_t < config.STATUS_UPDATE_INTERVAL_S:
            return
        self._status_t = now
        self._free_mb = free_space_mb()
        # частота кадров
        dt = now - self._fps_t
        if dt > 0:
            self._fps = self._fps_counter / dt
        self._fps_counter = 0
        self._fps_t = now
        if self.rec_state != REC_RUNNING:
            return
        try:
            size = os.path.getsize(self.rec_file)
        except OSError:
            size = self._last_size
        dt = now - self._last_size_t
        if dt > 0:
            self.measured_bps = (size - self._last_size) * 8 / dt
        self._last_size, self._last_size_t = size, now
        if self._free_mb is not None and self._free_mb < config.MIN_FREE_SPACE_MB:
            log.warning("Место кончается — запись остановлена")
            self.rec_message = config.TEXT_LOW_SPACE
            self.stop_recording()

    # -----------------------------------------------------------------
    # Главное: вызывать из главного цикла программы как можно чаще
    # -----------------------------------------------------------------
    def poll(self):
        now = time.monotonic()
        if self._capture is None and now >= self._next_retry_t:
            self._next_retry_t = now + config.DEVICE_RETRY_INTERVAL_S
            self._start_capture()
        self._handle_capture_bus()
        self._check_capture_health(now)
        self._handle_record_bus(now)
        self._update_record_stats(now)

    def pull_preview_sample(self):
        """Последний кадр для экрана (Gst.Sample) или None. Только при preview="appsink"."""
        sink = self._preview_sink
        if sink is None:
            return None
        return sink.emit("try-pull-sample", 0)

    def rec_elapsed(self):
        """Сколько секунд идёт запись (для индикатора REC 00:00:00)."""
        if self.rec_state == REC_RUNNING:
            return time.monotonic() - self._rec_start_t
        if self.rec_state == REC_STOPPING:
            return self._rec_elapsed_final
        return 0.0

    def status(self):
        """Всё, что нужно интерфейсу, одним словарём."""
        std = ""
        if config.VIDEO_STANDARD != "auto":
            std = config.VIDEO_STANDARD
        elif config.CAPTURE_HEIGHT >= 540:
            std = "PAL"
        elif config.CAPTURE_FPS >= 59:
            std = "NTSC"
        else:
            std = "PAL"
        return {
            "state": self.state,
            "message": self.message,
            "device": self.device,
            "device_name": config.USB_DEVICE_NAME,
            "video_standard": std,
            "capture_w": config.CAPTURE_WIDTH,
            "capture_h": config.CAPTURE_HEIGHT,
            "capture_fps": config.CAPTURE_FPS,
            "fps": self._fps,
            "luma": self.luma_mean,
            "luma_std": self.luma_std,
            "rec_state": self.rec_state,
            "rec_file": self.rec_file,
            "rec_elapsed": self.rec_elapsed(),
            "rec_message": self.rec_message,
            "bitrate_key": self.bitrate_key,
            "measured_bps": self.measured_bps,
            "free_mb": self._free_mb,
        }

    def close(self, timeout=None):
        """Корректный выход: закрыть запись (дождаться EOS), остановить захват."""
        timeout = config.RECORD_STOP_TIMEOUT_S if timeout is None else timeout
        if self.rec_state == REC_RUNNING:
            self.stop_recording()
        end = time.monotonic() + timeout + 1
        while self.rec_state == REC_STOPPING and time.monotonic() < end:
            self._handle_record_bus(time.monotonic())
            time.sleep(0.05)
        # Гарантированно освобождаем устройство захвата (переводим конвейер в NULL),
        # чтобы при следующем запуске не было ошибки "Device is busy".
        self._force_null_capture()
        self.state = NO_DEVICE


# =====================================================================
# Самопроверка: python3 recorder.py [секунды записи]
# =====================================================================
def _self_test():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    Gst.init(None)
    preview = "kmssink" if Gst.ElementFactory.find("kmssink") else "none"
    preview = os.environ.get("CAMCORDER_TEST_PREVIEW", preview)
    print("Самопроверка recorder.py: превью=%s, запись=%s" % (
        preview, "%g с" % seconds if seconds > 0 else "нет"))
    print("Выход: Ctrl+C\n")

    rec = Recorder(preview=preview)
    recorded = False
    last_print = 0.0
    try:
        while True:
            rec.poll()
            now = time.monotonic()
            # Как только пошли кадры — начинаем запись (один раз)
            if seconds > 0 and not recorded and rec.state in (LIVE, NO_SIGNAL):
                ok, msg = rec.start_recording()
                print(">>>", msg)
                recorded = ok
            if recorded and rec.rec_state == REC_RUNNING and rec.rec_elapsed() >= seconds:
                rec.stop_recording()
            if now - last_print >= 1.0:
                last_print = now
                s = rec.status()
                luma = "-" if s["luma"] is None else "%.0f±%.0f" % (s["luma"], s["luma_std"])
                rec_info = s["rec_state"]
                if s["rec_state"] != REC_IDLE:
                    rec_info += " %.0f с, %.1f Мбит/с" % (s["rec_elapsed"], s["measured_bps"] / 1e6)
                free = "?" if s["free_mb"] is None else "%.0f" % s["free_mb"]
                print("[%s] %4.1f к/с | яркость %s | запись: %s | свободно %s МБ | %s" % (
                    s["state"], s["fps"], luma, rec_info, free, s["message"]))
            # Запись была и закончилась — ещё пара секунд превью и выход
            if recorded and rec.rec_state == REC_IDLE:
                print("\nГотово. Файл:", rec.rec_file)
                try:
                    print("Размер: %.1f МБ" % (os.path.getsize(rec.rec_file) / 1e6))
                except OSError:
                    print("Файл не найден!")
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nCtrl+C — выходим")
    finally:
        rec.close()
    sys.stdout.flush()
    os._exit(0)     # не ждём фоновые потоки (если какой-то элемент завис)


if __name__ == "__main__":
    _self_test()
