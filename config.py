# -*- coding: utf-8 -*-
"""
config.py — все настройки видеорекордера в одном месте.

Здесь только константы (числа, строки, цвета). Никакой логики.
Если нужно что-то поменять (битрейт, папку, цвета) — меняйте здесь,
остальные файлы программы берут значения отсюда.

Самопроверка:  python3 config.py
"""

import os

# =====================================================================
# 1. ВИРТУАЛЬНЫЙ ЭКРАН (ХОЛСТ ИНТЕРФЕЙСА)
# =====================================================================
# Весь интерфейс рисуется на холсте 480x320 (как будущий 3.5" экран).
# На мониторе 1920x1080 холст увеличивается с сохранением пропорций,
# по бокам остаются чёрные поля (letterbox).
CANVAS_WIDTH = 480
CANVAS_HEIGHT = 320

# Режим вывода изображения:
#   "kms" — как в Ruby FPV: видео и меню на РАЗНЫХ аппаратных слоях GPU
#           (через модуль pykms). Почти не нагружает процессор.
#           Работает только на HDMI/DSI-экранах.
#   "sdl" — простой режим: обычное окно Pygame, видео рисуется внутри окна.
#           Нужен для отладки и для SPI-экранов.
#   "auto" — попробовать "kms", при неудаче переключиться на "sdl".
DISPLAY_BACKEND = "auto"

# =====================================================================
# 1б. СОСТОЯНИЯ ЗАХВАТА И ЗАПИСИ
# =====================================================================
# Общие константы для recorder.py и интерфейса (main.py/ui.py), чтобы
# интерфейс не импортировал GStreamer. recorder.py делает свои имена
# через эти же значения (NO_DEVICE = STATE_NO_DEVICE и т.д.).
STATE_NO_DEVICE = "NO_DEVICE"   # свистка нет — показываем диагностику
STATE_STARTING = "STARTING"     # захват запускается, кадров ещё нет
STATE_LIVE = "LIVE"             # кадры идут
STATE_NO_SIGNAL = "NO_SIGNAL"   # кадры идут, но они чёрные (провод камеры выдернут)

REC_IDLE = "IDLE"               # не пишем
REC_RUNNING = "RECORDING"       # пишем
REC_STOPPING = "STOPPING"       # закрываем файл (ждём EOS)

# =====================================================================
# 2. ЗАХВАТ ВИДЕО (USB-свисток MacroSilicon MS210x / EasierCAP, UTV007)
# =====================================================================
# Поддерживаемые USB-устройства. Каждое: vid, pid, имя, поддерживаемые режимы.
# Режимы: (fourcc, ширина, высота, fps) — fps может быть дробным (59.94).
# Главный цикл перебирает устройства по порядку и берёт первое найденное.
USB_DEVICES = [
    {
        "vid": "534d",
        "pid": "0021",
        "name": "MacroSilicon MS210x / EasierCAP (AV TO USB2.0)",
        "modes": {
            "MJPG": [(480, 320, 25.0), (640, 480, 25.0), (720, 480, 25.0)],
            "YUY2": [(480, 320, 25.0)],
        },
    },
    {
        "vid": "1b71",
        "pid": "3002",
        "name": "Fushicai UTV007",
        # UTV007 не отдаёт MJPG и не отдаёт 50/59.94 fps напрямую.
        # Только YUYV/YUY2, чересстрочный, 25 fps (PAL) / 29.97 fps (NTSC).
        # 50p / 59.94p получаются через deinterlace в GStreamer.
        "modes": {
            "YUYV": [(720, 576, 25.0), (720, 480, 29.97)],
        },
    },
]

# USB ID текущего устройства (заполняется при обнаружении в v4l2_checker).
USB_VENDOR_ID = "534d"     # MacroSilicon (по умолчанию)
USB_PRODUCT_ID = "0021"    # MS210x (по умолчанию)
USB_DEVICE_NAME = "MacroSilicon MS210x"

# Где искать устройство (по порядку). Пути /dev/v4l/by-id/... не меняются
# после переподключения провода, а номер /dev/videoN — может поменяться.
VIDEO_DEVICE_CANDIDATES = [
    "/dev/v4l/by-id/usb-MACROSILICON*-video-index0",
    "/dev/v4l/by-id/usb-UTV007*-video-index0",
    "/dev/v4l/by-id/usb-*UTV007*-video-index0",
    "/dev/video0",          # запасной вариант
]

# Видеостандарт: "PAL" (50 п/с), "NTSC" (59.94 п/с) или "auto" —
# определяется по найденному устройству и его режимам.
VIDEO_STANDARD = "auto"

# Стандарты видео (ширина, высота, кадры в секунду). Используются как
# предпочитаемый режим при запросе у устройства.
STANDARD_SETTINGS = {
    "PAL":   {"width": 720, "height": 576,  "fps": 50.0,  "label": "PAL 50p"},
    "NTSC":  {"width": 720, "height": 480,  "fps": 59.94, "label": "NTSC 59.94p"},
}

# Выбранный режим захвата (может быть переопределен после обнаружения устройства):
#   "MJPG" — сжатый (лучшее качество). Кадры нужно распаковывать.
#   "YUY2" — несжатый (проще, но разрешение ниже у многих свистков).
CAPTURE_FORMAT = "MJPG"

# Режим захвата (ширина, высота, fps). По умолчанию PAL 720x576@50,
# но MS210x не тянет 576/50 — v4l2_checker подберет ближайшее поддерживаемое.
CAPTURE_WIDTH = 720
CAPTURE_HEIGHT = 576
CAPTURE_FPS = 50.0

# Чем распаковывать MJPEG (только для CAPTURE_FORMAT = "MJPG"):
#   "v4l2jpegdec" — аппаратный декодер Raspberry Pi (почти не грузит процессор).
#   "jpegdec"     — программный декодер (~40% одного ядра на Pi 2).
# Проверено на Pi: оба работают. У v4l2jpegdec есть особенность — он зависает,
# когда поток штатно заканчивается (EOS). Программе это не мешает: захват
# никогда не останавливается через EOS, а файл записи закрывается отдельно,
# в другом пайплайне, где декодера нет. Если с v4l2jpegdec будут странные
# проблемы (картинка замирает, программа виснет при выходе) — поставьте "jpegdec".
JPEG_DECODER = "jpegdec"

# Если с выбранным декодером кадры не идут (NO_FRAMES_TIMEOUT_S) — переключиться
# на запасной ("v4l2jpegdec" <-> "jpegdec") и попробовать снова.
# Помогает, если v4l2jpegdec не смог занять аппаратный M2M-кодек Pi.
# Выбор фиксируется в журнале: рабочий декодер запоминается до перезапуска.
JPEG_DECODER_FALLBACK = True

# Как записывать видео в файл:
#   "h264"       — распаковать MJPEG и пережать аппаратным кодером H.264
#                  (работают пресеты битрейта, файлы компактные).
#   "mjpeg_copy" — писать MJPEG со свистка как есть, без пережатия
#                  (почти ноль нагрузки на процессор, но файлы крупнее,
#                  пресеты битрейта не действуют).
RECORD_MODE = "h264"

# Пропорции картинки с камеры: "4:3" (обычный режим) или "16:9"
# (широкоэкранный анаморфный режим камеры). Влияет на вписывание в холст
# и на метку пропорций в файле.
VIDEO_ASPECT = "4:3"

# Как часто (в секундах) искать устройство, если оно отключено
DEVICE_RETRY_INTERVAL_S = 1.0

# Если кадров нет дольше этого времени (в секундах) — считаем, что сигнал
# потерян, и перезапускаем захват.
NO_FRAMES_TIMEOUT_S = 3.0

# Если свисток в USB, а провод от камеры выдернут, свисток продолжает слать
# кадры — обычно чёрные или однотонные. Тогда "NO INPUT SIGNAL" можно
# определить только по самой картинке: средняя яркость (0..255) и её разброс.
# Пороги нужно подобрать под ваш свисток: `python3 recorder.py 0` печатает
# яркость каждую секунду — посмотрите цифры с проводом и без провода.
# Пока пороги не подобраны, оставьте False.
NO_SIGNAL_ON_BLACK = False
BLACK_FRAME_LUMA_MAX = 24      # средняя яркость ниже этого...
BLACK_FRAME_STD_MAX = 6        # ...и почти без разброса -> "нет сигнала"

# =====================================================================
# 3. ЗАПИСЬ ВИДЕО
# =====================================================================
# Папка для записей: ~/Videos в домашней папке пользователя
# (в Trixie/Bookworm имя пользователя задаётся в Raspberry Pi Imager,
# поэтому не пишем жёстко /home/pi). Запускайте программу БЕЗ sudo,
# иначе "~" превратится в /root.
VIDEO_DIR = os.path.expanduser("~/Videos")

# Имя файла: REC_20261112_041500.mkv  (формат для time.strftime)
FILENAME_PATTERN = "REC_%Y%m%d_%H%M%S.mkv"

# Пресеты битрейта (бит в секунду). Ключ — внутреннее имя.
BITRATE_PRESETS = {
    "economy":  {"label": "ЭКОНОМ",   "bps": 2_500_000},
    "standard": {"label": "СТАНДАРТ", "bps": 5_000_000},
    "max":      {"label": "МАКСИМУМ", "bps": 8_000_000},
}
BITRATE_ORDER = ["economy", "standard", "max"]   # порядок в меню
DEFAULT_BITRATE = "standard"

# Ключевой кадр (I-frame) каждые N кадров. 25 = раз в секунду:
# файл легче резать и меньше теряется при обрыве записи.
H264_KEYFRAME_INTERVAL = 25

# Уровень H.264. Без явного уровня энкодер v4l2h264enc без явного
# уровня иногда отказывается запускаться.
H264_LEVEL = "4"

# Нижняя граница свободного места: меньше — запись не начнётся
# (или остановится сама).
MIN_FREE_SPACE_MB = 500

# Сколько секунд ждать корректного закрытия файла при остановке записи
RECORD_STOP_TIMEOUT_S = 5.0

# Звук (Этап 2).
# Проверено: у свистка есть звук (arecord -l -> card 1: MS210x).
# Обращаемся по имени карты, а не по номеру — номер может поменяться.
AUDIO_ENABLED = False
AUDIO_DEVICE = "plughw:CARD=MS210x,DEV=0"

# =====================================================================
# 4. ЦВЕТА В СТИЛЕ SONY NEX-5N
# =====================================================================
# Формат (R, G, B) или (R, G, B, A), где A — непрозрачность 0..255
# (0 = полностью прозрачно, 255 = полностью непрозрачно).
SONY_ORANGE = (255, 102, 0)            # #FF6600 — фирменный акцент
WHITE = (255, 255, 255)
TEXT_MAIN = (240, 240, 240)            # основной текст (чуть мягче белого)
TEXT_DIM = (150, 150, 150)             # второстепенный текст
REC_RED = (230, 0, 18)                 # красная точка REC
WARNING_YELLOW = (255, 200, 0)         # предупреждения (мало места и т.п.)
BLACK = (0, 0, 0)
TRANSPARENT = (0, 0, 0, 0)

PANEL_GRAPHITE = (28, 28, 30, 170)     # полупрозрачная графитовая плашка
PANEL_GRAPHITE_DARK = (15, 15, 17, 215)  # фон меню настроек
PANEL_BORDER = (70, 70, 74, 200)       # тонкая рамка плашек
BUTTON_ACTIVE = (255, 102, 0, 230)     # выбранный пункт меню

# =====================================================================
# 5. ШРИФТЫ И РАЗМЕРЫ ЭЛЕМЕНТОВ (в пикселях холста 480x320)
# =====================================================================
# Шрифт DejaVu ставится пакетом fonts-dejavu-core. Если файла нет,
# программа возьмёт встроенный шрифт Pygame.
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE_SMALL = 11
FONT_SIZE_NORMAL = 14
FONT_SIZE_LARGE = 20

OSD_MARGIN = 6           # отступ плашек от края холста
PANEL_RADIUS = 4         # скругление углов плашек
TOP_BAR_HEIGHT = 24      # высота верхней строки индикаторов
BOTTOM_BAR_HEIGHT = 40   # высота нижней строки (кнопки под видео)

# Прямоугольники кнопок и меню на холсте (x, y, ширина, высота).
# Считаются от размера холста, чтобы интерфейс не был привязан к 480x320 числами.
BITRATE_BUTTON_RECT = (OSD_MARGIN, CANVAS_HEIGHT - BOTTOM_BAR_HEIGHT + 4, 124, 32)
REC_BUTTON_RECT = (CANVAS_WIDTH - OSD_MARGIN - 72,
                   CANVAS_HEIGHT - BOTTOM_BAR_HEIGHT + 4, 72, 32)
MENU_RECT = (56, 56, CANVAS_WIDTH - 112, CANVAS_HEIGHT - 112)
MENU_ITEM_HEIGHT = 36

# =====================================================================
# 6. ВРЕМЕННЫЕ ПАРАМЕТРЫ
# =====================================================================
UI_FPS = 30                   # частота главного цикла программы
REC_BLINK_PERIOD_S = 1.0      # период мигания красной точки REC
STATUS_UPDATE_INTERVAL_S = 1.0  # как часто пересчитывать место/битрейт

# =====================================================================
# 7. ЖЕСТЫ МЫШЬЮ (эмуляция свайпа)
# =====================================================================
SWIPE_MIN_DISTANCE = 60       # минимум пикселей по вертикали для свайпа
SWIPE_MAX_TIME_S = 0.8        # протяжка дольше — не свайп
CLICK_MAX_DISTANCE = 8        # сдвиг меньше — считаем обычным кликом
MOUSE_SENSITIVITY = 0.5       # скорость своего курсора в режиме "kms"

# =====================================================================
# 8. НАДПИСИ
# =====================================================================
TEXT_NO_SIGNAL = "NO INPUT SIGNAL"
TEXT_REC = "REC"
TEXT_STBY = "STBY"
TEXT_LOW_SPACE = "MEMORY FULL"

# =====================================================================
# 9. ЖУРНАЛ (ЛОГ)
# =====================================================================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camcorder.log")
LOG_LEVEL = "INFO"            # "DEBUG" — подробнее, для поиска проблем


# =====================================================================
# Вспомогательные вычисления (их не нужно менять руками)
# =====================================================================
def video_rect_on_canvas():
    """
    Прямоугольник (x, y, ширина, высота), куда вписывается видео
    на холсте 480x320 с сохранением пропорций VIDEO_ASPECT.
    4:3  -> (26, 0, 427, 320)  — чёрные полосы слева и справа
    16:9 -> (0, 25, 480, 270)  — чёрные полосы сверху и снизу
    """
    aw, ah = (int(n) for n in VIDEO_ASPECT.split(":"))
    if CANVAS_WIDTH * ah > CANVAS_HEIGHT * aw:      # холст шире видео
        h = CANVAS_HEIGHT
        w = round(h * aw / ah)
    else:                                           # холст уже видео
        w = CANVAS_WIDTH
        h = round(w * ah / aw)
    return ((CANVAS_WIDTH - w) // 2, (CANVAS_HEIGHT - h) // 2, w, h)


def pixel_aspect_ratio():
    """
    Форма пикселя (pixel aspect ratio) для записи в файл, чтобы плееры
    показывали правильные пропорции. Считается из размера кадра и VIDEO_ASPECT.
    720x480, 4:3  -> (8, 9)     720x480, 16:9 -> (32, 27)
    """
    from fractions import Fraction
    aw, ah = (int(n) for n in VIDEO_ASPECT.split(":"))
    par = Fraction(aw * CAPTURE_HEIGHT, ah * CAPTURE_WIDTH)
    return (par.numerator, par.denominator)


def letterbox_rect(screen_w, screen_h):
    """
    Где на реальном экране (например 1920x1080) окажется холст 480x320.
    Возвращает (x, y, ширина, высота). Для 1920x1080: (150, 0, 1620, 1080).
    """
    scale = min(screen_w / CANVAS_WIDTH, screen_h / CANVAS_HEIGHT)
    w = round(CANVAS_WIDTH * scale)
    h = round(CANVAS_HEIGHT * scale)
    return ((screen_w - w) // 2, (screen_h - h) // 2, w, h)


def _check():
    """Проверка, что в настройках нет опечаток."""
    problems = []
    if DEFAULT_BITRATE not in BITRATE_PRESETS:
        problems.append("DEFAULT_BITRATE нет в BITRATE_PRESETS")
    if set(BITRATE_ORDER) != set(BITRATE_PRESETS):
        problems.append("BITRATE_ORDER не совпадает с BITRATE_PRESETS")
    if VIDEO_ASPECT not in ("4:3", "16:9"):
        problems.append("VIDEO_ASPECT должен быть '4:3' или '16:9'")
    if VIDEO_STANDARD not in ("PAL", "NTSC", "auto"):
        problems.append("VIDEO_STANDARD должен быть 'PAL', 'NTSC' или 'auto'")
    if VIDEO_STANDARD != "auto" and VIDEO_STANDARD not in STANDARD_SETTINGS:
        problems.append("VIDEO_STANDARD=%s нет в STANDARD_SETTINGS" % VIDEO_STANDARD)
    if CAPTURE_FORMAT not in ("MJPG", "YUY2"):
        problems.append("CAPTURE_FORMAT должен быть 'MJPG' или 'YUY2'")
    if not isinstance(CAPTURE_FPS, (int, float)) or CAPTURE_FPS <= 0:
        problems.append("CAPTURE_FPS должен быть положительным числом")
    if RECORD_MODE not in ("h264", "mjpeg_copy"):
        problems.append("RECORD_MODE должен быть 'h264' или 'mjpeg_copy'")
    if RECORD_MODE == "mjpeg_copy" and CAPTURE_FORMAT != "MJPG":
        problems.append("RECORD_MODE='mjpeg_copy' работает только с CAPTURE_FORMAT='MJPG'")
    if JPEG_DECODER not in ("v4l2jpegdec", "jpegdec"):
        problems.append("JPEG_DECODER должен быть 'v4l2jpegdec' или 'jpegdec'")
    if DISPLAY_BACKEND not in ("auto", "kms", "sdl"):
        problems.append("DISPLAY_BACKEND должен быть 'auto', 'kms' или 'sdl'")
    for dev in USB_DEVICES:
        if "vid" not in dev or "pid" not in dev or "name" not in dev or "modes" not in dev:
            problems.append("USB-устройство %s lacks required fields" % dev.get("name", "?"))
    return problems


# Если запустить сам файл (python3 config.py) — покажет сводку настроек
if __name__ == "__main__":
    print("=== Настройки видеорекордера ===")
    print("Холст интерфейса :", CANVAS_WIDTH, "x", CANVAS_HEIGHT)
    print("Режим вывода     :", DISPLAY_BACKEND)
    print("Видеостандарт    :", VIDEO_STANDARD,
          "(" + STANDARD_SETTINGS[VIDEO_STANDARD]["label"] + ")"
          if VIDEO_STANDARD != "auto" else "(авто)")
    print("Захват           : %s %dx%d @ %g fps, %s"
          % (CAPTURE_FORMAT, CAPTURE_WIDTH, CAPTURE_HEIGHT, CAPTURE_FPS, VIDEO_ASPECT))
    print("Декодер MJPEG    :", JPEG_DECODER)
    print("Запись           :", RECORD_MODE, "| форма пикселя:", pixel_aspect_ratio())
    print("Видео на холсте  :", video_rect_on_canvas())
    print("Холст на 1080p   :", letterbox_rect(1920, 1080))
    print("Папка записей    :", VIDEO_DIR,
          "(существует)" if os.path.isdir(VIDEO_DIR) else "(НЕ НАЙДЕНА: mkdir -p %s)" % VIDEO_DIR)
    print("USB-устройства   :")
    for dev in USB_DEVICES:
        print("    %s:%s %s" % (dev["vid"], dev["pid"], dev["name"]))
        for fmt, modes in dev["modes"].items():
            for w, h, fps in modes:
                print("        %s %dx%d @ %g" % (fmt, w, h, fps))
    for key in BITRATE_ORDER:
        p = BITRATE_PRESETS[key]
        mark = "  <- по умолчанию" if key == DEFAULT_BITRATE else ""
        print("Битрейт %-9s: %.1f Мбит/с (~%.1f ГБ/час)%s"
              % (p["label"], p["bps"] / 1e6, p["bps"] * 3600 / 8 / 1e9, mark))
    print("Шрифт            :", FONT_PATH,
          "(есть)" if os.path.isfile(FONT_PATH) else "(нет, будет встроенный)")
    errors = _check()
    print("Ошибки           :", "; ".join(errors) if errors else "нет — всё в порядке")
