#!/bin/bash
# Compila el grabador contra UHD.
# Por defecto usa el UHD del sistema (apt install libuhd-dev). Para usar un UHD
# compilado a mano: UHD_PREFIX=/ruta/a/uhd-install ./compilar.sh
set -e
cd "$(dirname "$0")"
if [ -n "${UHD_PREFIX:-}" ]; then
  FLAGS=(-I"$UHD_PREFIX/include" -L"$UHD_PREFIX/lib" -Wl,-rpath,"$UHD_PREFIX/lib" -luhd)
elif pkg-config --exists uhd; then
  read -r -a FLAGS <<<"$(pkg-config --cflags --libs uhd)"
else
  FLAGS=(-luhd)
fi
g++ -O2 -std=c++17 -pthread rx_iq_grabador.cpp -o rx_iq_grabador "${FLAGS[@]}" -lboost_program_options
echo "OK: $(pwd)/rx_iq_grabador"
