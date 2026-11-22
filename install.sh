#!/usr/bin/env bash
# Instalador de LTE Buscador · Grabador · Analizador (basado en srsRAN_4G).
#
# Instala las dependencias con apt, compila lo necesario de srsRAN y las
# herramientas, y agrega las tres aplicaciones al menú de Ubuntu.
#
#   ./install.sh                 instalación normal (UHD de apt)
#   UHD_PREFIX=/ruta ./install.sh  usar un UHD compilado a mano
#   SIN_MENU=1 ./install.sh      no crear los accesos del menú
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$REPO"

verde() { printf '\033[1;32m%s\033[0m\n' "$*"; }
paso()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
error() { printf '\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || error "no lo ejecutes con sudo; el script pide la contraseña cuando la necesita."
[ -r /etc/os-release ] && . /etc/os-release
[ "${ID:-}" = "ubuntu" ] || [ "${ID_LIKE:-}" = "debian" ] || [ "${ID_LIKE:-}" = "ubuntu debian" ] || \
  echo "Aviso: probado en Ubuntu 26.04; en ${PRETTY_NAME:-este sistema} podría fallar."

# ---------------------------------------------------------------- 1. paquetes
paso "1/6  Dependencias del sistema"
PAQUETES=(build-essential cmake pkg-config git
          libfftw3-dev libmbedtls-dev libboost-program-options-dev libconfig++-dev libsctp-dev
          python3 python3-numpy python3-scipy python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
          desktop-file-utils)
[ -n "${UHD_PREFIX:-}" ] || PAQUETES+=(libuhd-dev uhd-host)

FALTAN=()
for p in "${PAQUETES[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || FALTAN+=("$p")
done
if [ ${#FALTAN[@]} -gt 0 ]; then
  echo "Se instalarán: ${FALTAN[*]}"
  sudo apt-get update
  sudo apt-get install -y "${FALTAN[@]}"
else
  echo "Todo instalado."
fi

# ---------------------------------------------------------------- 2. srsRAN
paso "2/6  Compilando srsRAN (cell_search_plmn y analizar_celda_iq)"
if [ ! -f build/CMakeCache.txt ]; then
  CMAKE_UHD=()
  if [ -n "${UHD_PREFIX:-}" ]; then
    CMAKE_UHD=(-DUHD_INCLUDE_DIRS="$UHD_PREFIX/include" -DUHD_LIBRARIES="$UHD_PREFIX/lib/libuhd.so")
  fi
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_WERROR=OFF "${CMAKE_UHD[@]}"
fi
# hilos según CPU y memoria (cada uno puede usar ~1,5 GB al compilar)
MEM_GB=$(awk '/MemAvailable/ {print int($2/1024/1024)}' /proc/meminfo)
JOBS=$(( MEM_GB * 2 / 3 )); [ "$JOBS" -ge 1 ] || JOBS=1
[ "$JOBS" -le "$(nproc)" ] || JOBS=$(nproc)
echo "Usando $JOBS hilos. Puede tardar entre 10 y 30 minutos."
cmake --build build --target cell_search_plmn analizar_celda_iq -j "$JOBS"
[ -x build/lib/examples/cell_search_plmn ] || error "no se generó cell_search_plmn (¿falta UHD?)."
[ -x build/lib/examples/analizar_celda_iq ] || error "no se generó analizar_celda_iq."

# ---------------------------------------------------------------- 3. herramientas
paso "3/6  Compilando el grabador y el detector de PSS"
UHD_PREFIX="${UHD_PREFIX:-}" tools/grabador_iq/compilar.sh
gcc -O3 -march=native -fopenmp -shared -fPIC tools/analizador_iq/pss_det.c \
    -o tools/analizador_iq/libpss_det.so -lfftw3f -lm
echo "OK: tools/analizador_iq/libpss_det.so"

# ---------------------------------------------------------------- 4. imágenes FPGA
paso "4/6  Imágenes FPGA del USRP X300"
if [ -n "${UHD_PREFIX:-}" ]; then
  DESCARGADOR="$UHD_PREFIX/bin/uhd_images_downloader"; SUDO=""
else
  DESCARGADOR="$(command -v uhd_images_downloader || true)"; SUDO="sudo"
fi
if [ -n "$DESCARGADOR" ] && [ -x "$DESCARGADOR" ]; then
  if [ -n "${UHD_PREFIX:-}" ]; then
    IMG="$(LD_LIBRARY_PATH="$UHD_PREFIX/lib" "$UHD_PREFIX/bin/uhd_config_info" --images-dir 2>/dev/null | sed -n 's/^Images directory: //p')"
  else
    IMG="$(uhd_config_info --images-dir 2>/dev/null | sed -n 's/^Images directory: //p')"
  fi
  if [ -n "$IMG" ] && ls "$IMG"/usrp_x3*_fpga_HG.bit >/dev/null 2>&1; then
    echo "Ya están descargadas en $IMG."
  else
    $SUDO "$DESCARGADOR" -t x3xx || echo "Aviso: no se pudieron descargar (¿sin internet?). Repite: sudo uhd_images_downloader -t x3xx"
  fi
else
  echo "Aviso: no encontré uhd_images_downloader; descarga las imágenes a mano."
fi

# ---------------------------------------------------------------- 5. red
paso "5/6  Búferes de red para recibir del USRP sin pérdidas"
CONF=/etc/sysctl.d/60-usrp-lte.conf
if [ -f "$CONF" ]; then
  echo "Ya configurados ($CONF)."
else
  printf 'net.core.rmem_max=33554432\nnet.core.wmem_max=33554432\n' | sudo tee "$CONF" >/dev/null
  sudo sysctl -q -p "$CONF"
  echo "OK: $CONF"
fi

# ---------------------------------------------------------------- 6. menú
paso "6/6  Aplicaciones en el menú"
if [ "${SIN_MENU:-0}" = "1" ]; then
  echo "Omitido (SIN_MENU=1)."
else
  DEST="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$DEST"
  crear() {  # clave nombre descripcion palabras
    cat >"$DEST/lte.$1.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$2
Comment=$3
Exec=/usr/bin/python3 "$REPO/tools/aplicaciones/ventana_app.py" $1
Icon=$REPO/tools/aplicaciones/iconos/$1.svg
Terminal=false
Categories=Science;Electronics;
Keywords=$4
StartupNotify=true
StartupWMClass=lte-$1
EOF
    chmod +x "$DEST/lte.$1.desktop"
  }
  crear buscador   "Buscador LTE"  "Busca celdas LTE en vivo con el USRP"                    "lte;celda;usrp;starlink;buscar;"
  crear grabador   "Grabador IQ"   "Graba IQ por bloques de 20 MHz con espectro en vivo"     "lte;iq;grabar;usrp;starlink;"
  crear analizador "Analizador IQ" "Busca la PSS y decodifica MIB/SIB1/SIB2 de grabaciones" "lte;iq;analizar;pss;sib;starlink;"
  update-desktop-database "$DEST" 2>/dev/null || true
  echo "OK: Buscador LTE, Grabador IQ y Analizador IQ."
fi

verde "
Instalación terminada.
Busca «Buscador LTE», «Grabador IQ» o «Analizador IQ» en el menú de aplicaciones.
Antes de usar el USRP X300, configura la red (ver README.md, sección «Conectar el X300»)."
