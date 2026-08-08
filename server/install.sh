#!/bin/sh
set -eu

APP_DIR="/DATA/AppData/ZimaGotchi"
IMAGE="zimagotchi:3.3.0"
CONTAINER="zimagotchi"

DEVICE_PATH="$(readlink -f /sys/class/net/wlan0/device)"
USB_PORT="$(basename "$(dirname "$DEVICE_PATH")")"
USB_VENDOR="$(cat "/sys/bus/usb/devices/$USB_PORT/idVendor")"
USB_PRODUCT="$(cat "/sys/bus/usb/devices/$USB_PORT/idProduct")"
if [ "$USB_VENDOR:$USB_PRODUCT" != "0bda:0179" ]; then
  echo "Adaptador recusado: esperado 0bda:0179, encontrado $USB_VENDOR:$USB_PRODUCT" >&2
  exit 1
fi

mkdir -p "$APP_DIR/data/captures"
cp app.py index.html networks.json Dockerfile "$APP_DIR/"
cd "$APP_DIR"

sudo docker build -t "$IMAGE" .
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
sudo docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --network host \
  --cap-add NET_RAW \
  --cap-add NET_ADMIN \
  -e WIFI_INTERFACE=wlan0 \
  -e PORT=8686 \
  -e BASE_DWELL=8 \
  -e MAX_DWELL=30 \
  -e OFFLINE_SECONDS=240 \
  -e WATCHDOG_TIMEOUT=300 \
  -e CHANNEL_HOLD_SECONDS=86400 \
  -e USB_RESET_COOLDOWN=1800 \
  -e USB_RESET_SECONDS=8 \
  -e USB_RESET_MAX_FAILURES=3 \
  -e USB_PORT="$USB_PORT" \
  -e DATA_DIR=/data \
  -v "$APP_DIR/data:/data" \
  -v /sys/bus/usb/drivers/usb:/host-usb-driver:rw \
  -v /sys/bus/usb/devices:/host-usb-devices:ro \
  "$IMAGE"

echo "ZimaGotchi iniciado em http://IP_DO_ZIMAOS:8686"
