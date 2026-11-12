# -*- coding: utf-8 -*-
"""
v4l2_checker.py — поиск USB-видеозахвата и проверка его режимов.

Что умеет:
  * find_usb_device()  — находит подключенное устройство из списка config.USB_DEVICES
                         (по idVendor/idProduct в /sys/bus/usb/devices);
  * find_video_device() — путь к видеоустройству (/dev/v4l/by-id/... или /dev/videoN);
  * list_modes(path)   — какие форматы/размеры/кадры в секунду умеет устройство;
  * check_device()     — всё сразу: найдено ли устройство, поддерживает ли оно
                         запрошенный режим из config.py, и если нет — какой режим
                         посоветовать вместо него.

Ни одна функция не "падает": при любой проблеме возвращается понятный ответ.

Возвращаемое значение check_device():
    (path, info) — path может быть None, если устройство не найдено;
    info — dict с ключами:
        "message"  — пояснение по-русски (для журнала и для экрана);
        "device"   — dict из config.USB_DEVICES (или None);
        "modes"    — список поддерживаемых режимов [(fourcc, w, h, fps), ...];
        "suggested" — кортеж (fourcc, w, h, fps) ближайшего поддерживаемого
                       режима, если запрошенный недоступен (иначе None).

Самопроверка (на Pi, БЕЗ запущенного gst-launch):
    python3 v4l2_checker.py
"""

import glob
import os
import re
import subprocess
import time

import config


# Кэш режимов устройства: путь -> (время замера, список режимов).
# v4l2-ctl — внешняя программа, а list_modes() зовёт главный цикл, поэтому
# без кэша интерфейс бы подтормаживал (и жечь процессор) на каждый замер.
_MODES_CACHE = {}


# ---------------------------------------------------------------------
# 1. Воткнут ли хоть одно поддерживаемое USB-устройство
# ---------------------------------------------------------------------
def find_usb_device():
    """
    Возвращает dict устройства из config.USB_DEVICES, если оно есть в USB,
    иначе None. Перебирает все устройства из списка по порядку.
    """
    for dev_dir in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(dev_dir, "idVendor")) as f:
                vendor = f.read().strip().lower()
            with open(os.path.join(dev_dir, "idProduct")) as f:
                product = f.read().strip().lower()
        except OSError:
            continue        # у "портов" и хабов этих файлов может не быть
        for dev in config.USB_DEVICES:
            if vendor == dev["vid"].lower() and product == dev["pid"].lower():
                return dev
    return None


def usb_dongle_present():
    """True, если в USB есть хоть одно поддерживаемое устройство."""
    return find_usb_device() is not None


# ---------------------------------------------------------------------
# 2. Путь к видеоустройству
# ---------------------------------------------------------------------
def find_video_device():
    """
    Возвращает путь к видеоустройству или None, если не найдено.
    Перебирает шаблоны из config.VIDEO_DEVICE_CANDIDATES по порядку.
    """
    for pattern in config.VIDEO_DEVICE_CANDIDATES:
        for path in sorted(glob.glob(pattern)):
            if os.path.exists(path):        # by-id — это ссылка, проверяем, что она жива
                return path
    return None


# ---------------------------------------------------------------------
# 3. Список режимов устройства
# ---------------------------------------------------------------------
def _list_modes(path):
    """Запускает `v4l2-ctl --list-formats-ext` и разбирает вывод (без кэша)."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", path, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    modes = []
    fmt = None
    size = None
    for line in out.splitlines():
        # Строка вида:  [0]: 'MJPG' (Motion-JPEG, compressed)
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if m:
            fmt = m.group(1)
            continue
        # Строка вида:  Size: Discrete 720x480
        m = re.search(r"Size:\s*\w+\s+(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        # Строка вида:  Interval: Discrete 0.040s (25.000 fps)
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and fmt and size:
            modes.append((fmt, size[0], size[1], float(m.group(1))))
    return modes


def list_modes(path, max_age=0.0):
    """
    Список режимов устройства:
        [("MJPG", 720, 480, 25.0), ("YUYV", 480, 320, 25.0), ...]
    Если v4l2-ctl не установлен или устройство не отвечает — пустой список.

    max_age — если больше 0, вернуть кэшированный результат не старше max_age
    секунд. Нужно главному циклу: v4l2-ctl запускается как отдельная программа,
    и звать его на каждый кадр нельзя.
    """
    now = time.monotonic()
    if max_age > 0:
        cached = _MODES_CACHE.get(path)
        if cached is not None and now - cached[0] < max_age:
            return cached[1]
    modes = _list_modes(path)
    if max_age > 0:
        _MODES_CACHE[path] = (now, modes)
    return modes


# ---------------------------------------------------------------------
# 4. Подбор режима под устройство
# ---------------------------------------------------------------------
def _config_fourcc():
    """Название формата из config.py так, как его пишет v4l2-ctl (YUY2 там зовётся YUYV)."""
    return "YUYV" if config.CAPTURE_FORMAT == "YUY2" else config.CAPTURE_FORMAT


def mode_supported(modes, fourcc, width, height, fps):
    """True, если среди режимов есть тот, что запрошен (с допуском на fps)."""
    return any(
        (f, w, h) == (fourcc, width, height) and abs(float(fps_) - float(fps)) < 0.5
        for f, w, h, fps_ in modes
    )


def _suggest_mode(modes, fourcc, width, height, fps):
    """
    Подобрать ближайший поддерживаемый режим.
    Приоритет: тот же формат -> тот же размер -> ближайший fps.
    Возвращает (fourcc, w, h, fps) или None.
    """
    # То же формат и размер, ближайший fps
    same = [(f, w, h, fp) for f, w, h, fp in modes
            if f == fourcc and w == width and h == height]
    if same:
        return min(same, key=lambda m: abs(m[3] - fps))
    # То же формат, любой размер
    same_fmt = [(f, w, h, fp) for f, w, h, fp in modes if f == fourcc]
    if same_fmt:
        return same_fmt[0]
    # Любой первый режим
    if modes:
        return modes[0]
    return None


def _standard_for_mode(width, height, fps):
    """Определить видеостандарт (PAL/NTSC) по разрешению и fps."""
    if height >= 540:
        return "PAL"
    if fps >= 50:
        return "PAL"
    if fps >= 59:
        return "NTSC"
    return "PAL"  # по умолчанию


# ---------------------------------------------------------------------
# 5. Общая проверка
# ---------------------------------------------------------------------
def check_device(modes_max_age=0.0):
    """
    Возвращает кортеж (path, info):
      path    — путь к видеоустройству или None;
      info    — dict с ключами:
          "message"   — пояснение по-русски;
          "device"    — dict устройства из config.USB_DEVICES (или None);
          "modes"     — список поддерживаемых режимов;
          "suggested" — предложенный режим, если запрошенный недоступен.

    modes_max_age — кэш проверки режимов в секундах (0 — проверять каждый раз).
    """
    # 1. Ищем USB-устройство
    dev = find_usb_device()
    if dev is None:
        return None, {
            "message": "Свисток захвата не подключён к USB",
            "device": None,
            "modes": [],
            "suggested": None,
        }

    # Обновляем глобальные USB ID, чтобы recorder использовал правильные
    config.USB_VENDOR_ID = dev["vid"]
    config.USB_PRODUCT_ID = dev["pid"]
    config.USB_DEVICE_NAME = dev["name"]

    # 2. Ищем видеоустройство
    path = find_video_device()
    if path is None:
        return None, {
            "message": "Свисток %s в USB есть, но видеоустройство не появилось (подождите пару секунд)" % dev["name"],
            "device": dev,
            "modes": [],
            "suggested": None,
        }

    # 3. Проверяем режимы
    modes = list_modes(path, max_age=modes_max_age)
    if not modes:
        return path, {
            "message": "Устройство %s найдено (%s) (режимы проверить не удалось)" % (path, dev["name"]),
            "device": dev,
            "modes": [],
            "suggested": None,
        }

    fourcc = _config_fourcc()
    req_w, req_h, req_fps = config.CAPTURE_WIDTH, config.CAPTURE_HEIGHT, config.CAPTURE_FPS
    req_fmt = config.CAPTURE_FORMAT

    if not mode_supported(modes, fourcc, req_w, req_h, req_fps):
        suggested = _suggest_mode(modes, fourcc, req_w, req_h, req_fps)
        if suggested:
            sw, sh, sfps = suggested[1], suggested[2], suggested[3]
            sfmt = suggested[0]
            # Если нашли подходящий режим — обновляем config, чтобы recorder использовал его
            config.CAPTURE_WIDTH = sw
            config.CAPTURE_HEIGHT = sh
            config.CAPTURE_FPS = sfps
            # Также корректируем формат, если устройство не поддерживает запрошенный
            if sfmt != fourcc:
                config.CAPTURE_FORMAT = "YUY2" if sfmt == "YUYV" else sfmt
                fourcc = sfmt
            std = _standard_for_mode(sw, sh, sfps)
            msg = ("Устройство %s (%s) не поддерживает %s %dx%d@%g — "
                   "будут использоваться %s %dx%d@%g" %
                   (path, dev["name"], req_fmt, req_w, req_h, req_fps,
                    fourcc, sw, sh, sfps))
        else:
            msg = ("Устройство %s (%s) не поддерживает запрошенный режим "
                   "%s %dx%d@%g и нет подходящей альтернативы" %
                   (path, dev["name"], fourcc, req_w, req_h, req_fps))
            suggested = None
        return path, {
            "message": msg,
            "device": dev,
            "modes": modes,
            "suggested": suggested,
        }

    std = _standard_for_mode(req_w, req_h, req_fps)
    return path, {
        "message": "Устройство %s (%s) готово: %s %dx%d@%g" % (
            path, dev["name"], fourcc, req_w, req_h, req_fps),
        "device": dev,
        "modes": modes,
        "suggested": None,
    }


# ---------------------------------------------------------------------
# Самопроверка: python3 v4l2_checker.py
# ---------------------------------------------------------------------
if __name__ == "__main__":
    dev = find_usb_device()
    print("USB-устройство    :", dev["name"] if dev else "НЕТ")
    p = find_video_device()
    print("Видеоустройство   :", p if p else "не найдено")
    if p:
        print("Настоящий путь    :", os.path.realpath(p))
        modes = list_modes(p)
        print("Режимы устройства :")
        for f, w, h, fps in modes:
            print("    %s %dx%d @ %g" % (f, w, h, fps))
        if not modes:
            print("    (не удалось получить — установлен ли v4l-utils?)")
    path, info = check_device()
    print("Итог              :", info["message"])
