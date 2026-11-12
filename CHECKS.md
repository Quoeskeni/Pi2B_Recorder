# Проверка после установки и перезагрузки (вводить по одной команде)

## 1. Графика и память GPU
```bash
grep -E "vc4-kms-v3d|gpu_mem" /boot/firmware/config.txt   # должна быть строка dtoverlay=vc4-kms-v3d
vcgencmd get_mem gpu                              # аппаратному кодеку нужно >= 128M
```
Если меньше 128M: `sudo nano /boot/firmware/config.txt` → добавить в конец `gpu_mem=128` → `sudo reboot`.

## 2. Свисток захвата
```bash
lsusb | grep -i 534d                 # должна быть строка с ID 534d:0021
v4l2-ctl --list-devices              # имя свистка и его /dev/videoN
ls -l /dev/v4l/by-id/                # стабильное имя устройства
v4l2-ctl -d /dev/video0 --list-formats-ext   # этот свисток: MJPEG до 720x480@25, YUYV только 480x320@25
arecord -l                           # есть ли у свистка звук (USB Audio)
ls /dev/video1*                      # video10/11/12 — аппаратный кодек Pi
lsmod | grep bcm2835_codec           # драйвер аппаратного кодека загружен
```

## 3. GStreamer-элементы на месте
```bash
gst-inspect-1.0 --version            # ожидаем 1.26.x (на Bookworm — 1.22.x)
for e in v4l2src v4l2jpegdec jpegdec v4l2h264enc h264parse matroskamux kmssink appsink appsrc; do gst-inspect-1.0 $e >/dev/null 2>&1 && echo "OK  $e" || echo "НЕТ $e"; done
```

## 4. Python-модули
```bash
python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); print('GStreamer', Gst.version_string())"
python3 -c "import pygame; print('Pygame', pygame.version.ver, 'SDL', pygame.get_sdl_version())"
python3 -c "import pykms; print('pykms OK')"
groups                               # в списке должны быть video, render, input
python3 -c "import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])"   # должны быть мышь и клавиатура
```

## 5. Есть ли аппаратный декодер JPEG
```bash
gst-inspect-1.0 | grep -i v4l2
```
Если в списке есть `v4l2jpegdec` — распаковка MJPEG пойдёт на GPU. Если нет — будет программный `jpegdec`, тоже должно хватить.

## 6. Живое видео на экран (ручной тест, без нашей программы)
Можно запускать из PuTTY — картинка появится на HDMI-мониторе. Выход — Ctrl+C.
```bash
# 6а: аппаратная распаковка JPEG (v4l2jpegdec)
gst-launch-1.0 v4l2src device=/dev/video0 ! image/jpeg,width=720,height=480,framerate=25/1 ! v4l2jpegdec ! videoconvert ! kmssink
# 6б: программная распаковка (если 6а выдаёт ошибку)
gst-launch-1.0 v4l2src device=/dev/video0 ! image/jpeg,width=720,height=480,framerate=25/1 ! jpegdec ! videoconvert ! kmssink
```
Во время теста во втором окне PuTTY — `top`: сравните загрузку процессора в 6а и 6б.

## 7. 10 секунд записи: MJPEG -> аппаратный H.264 -> MKV
**Сначала остановите превью из шага 6 (Ctrl+C или `pkill gst-launch`)** — свисток может отдавать видео только одной программе, иначе будет ошибка `Device '/dev/video0' is busy`.

Во втором окне PuTTY заранее запустите `top` и посмотрите загрузку процессора во время теста.
```bash
time gst-launch-1.0 -e v4l2src device=/dev/video0 num-buffers=250 \
  ! image/jpeg,width=720,height=480,framerate=25/1 ! v4l2jpegdec ! videoconvert ! video/x-raw,format=I420 \
  ! v4l2h264enc extra-controls="controls,video_bitrate=5000000,h264_i_frame_period=25,repeat_sequence_header=1" \
  ! "video/x-h264,level=(string)4" ! h264parse ! matroskamux ! filesink location=$HOME/Videos/test_h264.mkv
ls -lh $HOME/Videos/test_h264.mkv
```
Если 6а не работал — замените `v4l2jpegdec` на `jpegdec`. Ожидаем примерно 6 МБ (5 Мбит/с × 10 с). Строка `real` у `time` должна быть около 10–11 секунд: если сильно больше — процессор не успевает.

## 7б. Если шаг 7 не завершается сам (зависает после записи)
Признак: нет строки `Got EOS from element "pipeline0"`, приходится жать Ctrl+C.
Тесты ниже по очереди проверяют отдельные элементы. `timeout` сам остановит тест,
если тот зависнет, так что Ctrl+C жать не нужно. Каждую команду вводите целиком, одной строкой.

**А. Только аппаратный энкодер H.264, без свистка (тестовая картинка):**
```bash
time timeout -s INT -k 5 30 gst-launch-1.0 -e videotestsrc num-buffers=250 ! video/x-raw,width=720,height=480,framerate=25/1,format=I420 ! v4l2h264enc extra-controls="controls,video_bitrate=5000000,h264_i_frame_period=25,repeat_sequence_header=1" ! "video/x-h264,level=(string)4" ! h264parse ! matroskamux ! filesink location=$HOME/Videos/test_enc.mkv
```

**Б. Только аппаратный декодер JPEG, без записи:**
```bash
time timeout -s INT -k 5 30 gst-launch-1.0 -e v4l2src device=/dev/video0 num-buffers=250 ! image/jpeg,width=720,height=480,framerate=25/1 ! v4l2jpegdec ! fakesink
```

**В. Шаг 7 с программным декодером `jpegdec` (`top` во втором окне):**
```bash
time timeout -s INT -k 5 40 gst-launch-1.0 -e v4l2src device=/dev/video0 num-buffers=250 ! image/jpeg,width=720,height=480,framerate=25/1 ! jpegdec ! videoconvert ! video/x-raw,format=I420 ! v4l2h264enc extra-controls="controls,video_bitrate=5000000,h264_i_frame_period=25,repeat_sequence_header=1" ! "video/x-h264,level=(string)4" ! h264parse ! matroskamux ! filesink location=$HOME/Videos/test_h264_sw.mkv
```

Для каждого теста запишите две вещи: была ли строка `Got EOS from element "pipeline0"` и что показала строка `real`.

## 8. 10 секунд записи MJPEG без пережатия
```bash
gst-launch-1.0 -e v4l2src device=/dev/video0 num-buffers=250 \
  ! image/jpeg,width=720,height=480,framerate=25/1 ! matroskamux ! filesink location=$HOME/Videos/test_mjpeg.mkv
ls -lh $HOME/Videos/test_mjpeg.mkv
```
Размер файла покажет «родной» битрейт свистка (МБ × 0.8 = Мбит/с).

Файлы `test_*.mkv` можно скачать на ПК (WinSCP) и посмотреть в VLC.

## 9. Проверка наших модулей (v4l2_checker.py и recorder.py)
Скопируйте `config.py`, `v4l2_checker.py`, `recorder.py` в одну папку (у вас `/home/pi`).
Все `gst-launch` должны быть остановлены (`pkill gst-launch`).

**9а. Поиск свистка:**
```bash
python3 v4l2_checker.py
```
В конце должно быть `Итог: Устройство ... готово: MJPG 720x480@25`.

**9б. Превью на HDMI и запись 10 секунд:**
```bash
python3 recorder.py
```
Каждую секунду печатается строка состояния. Через ~10 с должно быть
`Запись закончена (файл закрыт правильно)` и `Готово. Файл: ...`.

**9в. Выдёргивание свистка во время записи** (запись 60 секунд):
```bash
python3 recorder.py 60
```
Секунд через 10 выдерните свисток из USB, подождите 5 секунд и воткните обратно.
Программа не должна упасть: запись закроется, состояние станет `NO_DEVICE`,
после возвращения свистка — снова `LIVE`. Выход — Ctrl+C.

**9г. Яркость с проводом камеры и без него** (без записи):
```bash
python3 recorder.py 0
```
Посмотрите числа `яркость X±Y`: сначала с подключённой камерой, потом выдернув
жёлтый провод от камеры (свисток оставить в USB). Выход — Ctrl+C.
По этим числам настроим надпись NO INPUT SIGNAL.
