#!/usr/bin/env bash
# Quita las aplicaciones del menú y, si lo confirmas, lo compilado y los ajustes.
# No borra tus grabaciones (~/capturas_iq) ni los paquetes instalados con apt.
set -u
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
pregunta() { read -r -p "$1 [s/N] " r; [ "${r,,}" = "s" ]; }

DEST="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
rm -f "$DEST"/lte.buscador.desktop "$DEST"/lte.grabador.desktop "$DEST"/lte.analizador.desktop
update-desktop-database "$DEST" 2>/dev/null || true
echo "Aplicaciones quitadas del menú."

if pregunta "¿Borrar lo compilado (carpeta build y binarios)?"; then
  rm -rf "$REPO/build" "$REPO/tools/grabador_iq/rx_iq_grabador" "$REPO/tools/analizador_iq/libpss_det.so"
  echo "Borrado."
fi
if pregunta "¿Borrar la configuración y registros de las ventanas (~/.local/share/herramientas-lte)?"; then
  rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/herramientas-lte"
  echo "Borrado."
fi
if [ -f /etc/sysctl.d/60-usrp-lte.conf ] && pregunta "¿Quitar el ajuste de búferes de red (pide sudo)?"; then
  sudo rm -f /etc/sysctl.d/60-usrp-lte.conf && echo "Quitado (se aplica al reiniciar)."
fi
echo "Listo. Tus grabaciones en ~/capturas_iq no se tocaron."
