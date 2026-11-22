# analizador_iq — busca celdas LTE en las grabaciones de grabador_iq

## Primera vez
    ./install.sh (en la raíz) compila el decodificador analizar_celda_iq
    # el nucleo C de deteccion (libpss_det.so) se compila solo la primera vez

## Interfaz
    python3 tools/analizador_iq/analizador_ui.py   # o desde el menú: «Analizador IQ»

Elige la grabación (la banda se detecta sola por los .json), el modo y pulsa «Analizar».

## Línea de comandos
    ./analizar_grabacion.py --listar
    ./analizar_grabacion.py --modo lista --earfcn 1175,1176        # Starlink Entel D2C
    ./analizar_grabacion.py --modo auto                            # portadoras visibles
    ./analizar_grabacion.py --modo exhaustivo                      # todos los EARFCN (lento)

## Cómo funciona
1. Espectrograma de toda la grabación.
2. Detección de la PSS en cada EARFCN candidato × hipótesis de Doppler durante TODA la
   grabación, integrando 100 ms (20 PSS) para ver señales bajo el ruido.
3. En los momentos de PSS más fuerte: MIB, SIB1 (PLMN, operador, TAC, ECI) y SIB2 con
   srsRAN (`analizar_celda_iq`, programa aparte; cell_search_plmn no se toca).

## Operadores
Para añadir operadores sin tocar el código, crea `operadores.json` junto a este archivo:
    [{"mcc": "730", "mnc": "01", "nombre": "Entel Chile (terrestre)"}]
