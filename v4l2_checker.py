# -*- coding: utf-8 -*-
"""
v4l2_checker.py — поиск USB-свистка захвата и проверка его режимов.

Что умеет:
  * usb_dongle_present()  — воткнут ли свисток в USB (по ID 534d:0021);
  * find_video_device()   — путь к видеоустройству (/dev/v4l/by-id/... или /dev/videoN);
  * list_modes(path)      — какие форматы/размеры/кадры в секунду умеет устройство;
  * check_device()        — всё сразу: найдено ли устройство и умеет ли оно режим из config.py.

Ни одна функция не "падает": при любой проблеме возвращается понятный ответ.

Самопроверка (на Pi, БЕЗ запущенного gst-launch):
    python3 v4l2_checker.py
"""

import glob
import os
import re
import subprocess

import config


# ---------------------------------------------------------------------
# 1. Воткнут ли свисток в USB
# ---------------------------------------------------------------------
def usb_dongle_present():
    """
    True, если в USB есть устройство с ID 534d:0021.
    Читаем служебные файлы ядра /sys/bus/usb/devices/*/idVendor и idProduct —
    это то же самое, что показывает lsusb, только без запуска программы.
    """
    for dev_dir in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(dev_dir, "idVendor")) as f:
                vendor = f.read().strip().lower()
            with open(os.path.join(dev_dir, "idProduct")) as f:
                product = f.read().strip().lower()
        except OSError:
            continue        # у "портов" и хабов этих файлов может не быть
        if vendor == config.USB_VENDOR_ID and product == config.USB_PRODUCT_ID:
            return True
    return False


# ---------------------------------------------------------------------
# 2. Путь к видеоустройству
# ---------------------------------------------------------------------
def find_video_device():
    """
    Возвращает путь к видеоустройству свистка или None, если его нет.
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
def list_modes(path):
    """
    Запускает `v4l2-ctl --list-formats-ext` и возвращает список режимов:
        [("MJPG", 720, 480, 25.0), ("YUYV", 480, 320, 25.0), ...]
    Если v4l2-ctl не установлен или устройство не отвечает — пустой список.
    """
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


def _config_fourcc():
    """Название формата из config.py так, как его пишет v4l2-ctl (YUY2 там зовётся YUYV)."""
    return "YUYV" if config.CAPTURE_FORMAT == "YUY2" else config.CAPTURE_FORMAT


def mode_supported(modes):
    """True, если среди режимов есть тот, что выбран в config.py."""
    want = (_config_fourcc(), config.CAPTURE_WIDTH, config.CAPTURE_HEIGHT)
    return any((f, w, h) == want and abs(fps - config.CAPTURE_FPS) < 0.5
               for f, w, h, fps in modes)


# ---------------------------------------------------------------------
# 4. Общая проверка
# ---------------------------------------------------------------------
def check_device():
    """
    Возвращает кортеж (path, message):
      path    — путь к устройству, если им можно пользоваться, иначе None;
      message — пояснение по-русски (для журнала и для экрана).
    """
    path = find_video_device()
    if path is None:
        if usb_dongle_present():
            return None, "Свисток в USB есть, но видеоустройство не появилось (подождите пару секунд)"
        return None, "Свисток захвата не подключён к USB"

    modes = list_modes(path)
    if not modes:
        # v4l2-ctl нет или устройство занято/не ответило. Не мешаем запуску:
        # GStreamer сам сообщит об ошибке, если режим не подойдёт.
        return path, "Устройство %s найдено (режимы проверить не удалось)" % path
    if not mode_supported(modes):
        return None, "Устройство %s не умеет режим %s %dx%d@%d" % (
            path, config.CAPTURE_FORMAT, config.CAPTURE_WIDTH,
            config.CAPTURE_HEIGHT, config.CAPTURE_FPS)
    return path, "Устройство %s готово: %s %dx%d@%d" % (
        path, config.CAPTURE_FORMAT, config.CAPTURE_WIDTH,
        config.CAPTURE_HEIGHT, config.CAPTURE_FPS)


# ---------------------------------------------------------------------
# Самопроверка: python3 v4l2_checker.py
# ---------------------------------------------------------------------
if __name__ == "__main__":
    print("Свисток в USB      :", "да" if usb_dongle_present() else "НЕТ")
    p = find_video_device()
    print("Видеоустройство    :", p if p else "не найдено")
    if p:
        print("Настоящий путь     :", os.path.realpath(p))
        modes = list_modes(p)
        print("Режимы устройства  :")
        for f, w, h, fps in modes:
            print("    %s %dx%d @ %g" % (f, w, h, fps))
        if not modes:
            print("    (не удалось получить — установлен ли v4l-utils?)")
    path, msg = check_device()
    print("Итог               :", msg)
