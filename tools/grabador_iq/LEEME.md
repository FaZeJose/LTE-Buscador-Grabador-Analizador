# grabador_iq — captura IQ LTE con USRP X300 (solo RX)

## Interfaz (recomendado)
    ./compilar.sh                 # una vez
    python3 grabador_ui.py        # o desde el menú: «Grabador IQ»

- Configura banda/EARFCN, pulsa «Ver plan» y luego «Grabar».
- «Monitor» muestra el espectro en vivo sin guardar nada: úsalo para ajustar la ganancia
  (pico entre −20 y −6 dBFS, sin «SAT»; con sc8 entre −12 y −6).
- El espectro y la cascada se calculan sobre las mismas muestras que se escriben al .iq.
- «¿sc16 o sc8? · manual» explica los formatos.

## Línea de comandos
    ./grabar_lte.py --banda 2 --earfcn-inicio 600 --earfcn-fin 1199 --dry-run
    ./grabar_lte.py --banda 2 --earfcn-inicio 600 --earfcn-fin 1199 --uno-a-la-vez

## Convertir para srsRAN
    ./iq_a_cf32.py ~/capturas_iq/<archivo>.iq --earfcn 1000 --prb 100
    pdsch_ue -i <archivo>_e1000_100prb.cf32 -p 100

Recomendado antes de grabar:  sudo sysctl -w net.core.rmem_max=33554432 net.core.wmem_max=33554432
rx_iq_grabador solo crea un rx_streamer: no puede transmitir.
