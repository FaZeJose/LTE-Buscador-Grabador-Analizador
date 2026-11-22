#!/usr/bin/env python3
"""
grabar_lte.py - Graba IQ crudo de bandas LTE (bajada) con un USRP X300, SOLO RECEPCION.

Divide el rango de EARFCN pedido en bloques de 'ancho' MHz con 'solape' MHz,
muestra el plan (frecuencias, EARFCN cubiertos, tamaño, espacio libre) y graba
cada bloque con rx_iq_grabador (API C++ de UHD, sin ningun tx_streamer).

Ejemplos:
  ./grabar_lte.py --banda 2 --earfcn-inicio 600 --earfcn-fin 1199 --dry-run
  ./grabar_lte.py --banda 2 --earfcn-inicio 600 --earfcn-fin 1199 --uno-a-la-vez
  ./grabar_lte.py --banda 66 --earfcn-inicio 66436 --earfcn-fin 67335 --formato sc8
"""

import argparse
import datetime as dt
import json
import math
import re
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import es_argparse  # textos de argparse en español

DIR_SCRIPT = Path(__file__).resolve().parent
GRABADOR = Path(os.environ.get("RX_IQ_GRABADOR", DIR_SCRIPT / "rx_iq_grabador"))

MCR_HZ = 200e6          # master clock rate X300
TASA_SPS = MCR_HZ / 10  # 20 Msps
BYTES_POR_MUESTRA = {"sc16": 4, "sc8": 2}
RESERVA_BYTES = 2 * 1024**3   # dejar siempre al menos 2 GiB libres
RESERVA_FRAC = 0.03           # + 3 % del total a grabar

# 3GPP TS 36.101, tabla 5.7.3-1 (bajada): banda -> (F_DL_low MHz, N_Offs-DL, N_DL min, N_DL max)
TABLA_EARFCN_DL = {
    1: (2110.0, 0, 0, 599),
    2: (1930.0, 600, 600, 1199),
    3: (1805.0, 1200, 1200, 1949),
    4: (2110.0, 1950, 1950, 2399),
    5: (869.0, 2400, 2400, 2649),
    7: (2620.0, 2750, 2750, 3449),
    8: (925.0, 3450, 3450, 3799),
    12: (729.0, 5010, 5010, 5179),
    13: (746.0, 5180, 5180, 5279),
    14: (758.0, 5280, 5280, 5379),
    17: (734.0, 5730, 5730, 5849),
    20: (791.0, 6150, 6150, 6449),
    25: (1930.0, 8040, 8040, 8689),
    26: (859.0, 8690, 8690, 9039),
    28: (758.0, 9210, 9210, 9659),
    38: (2570.0, 37750, 37750, 38249),
    40: (2300.0, 38650, 38650, 39649),
    41: (2496.0, 39650, 39650, 41589),
    66: (2110.0, 66436, 66436, 67335),
    71: (617.0, 68586, 68586, 68935),
}

# Rango de sintonia del X300 con UBX-160 / SBX / CBX (aprox.)
FREQ_MIN_MHZ, FREQ_MAX_MHZ = 10.0, 6000.0


def earfcn_a_mhz(banda: int, earfcn: int) -> float:
    if banda not in TABLA_EARFCN_DL:
        raise ValueError(f"Banda {banda} no soportada. Disponibles: {sorted(TABLA_EARFCN_DL)}")
    f_low, n_offs, n_min, n_max = TABLA_EARFCN_DL[banda]
    if not n_min <= earfcn <= n_max:
        raise ValueError(f"EARFCN {earfcn} fuera de la banda {banda} (DL {n_min}-{n_max})")
    return f_low + 0.1 * (earfcn - n_offs)


def mhz_a_earfcn(banda: int, f_mhz: float) -> float:
    f_low, n_offs, _, _ = TABLA_EARFCN_DL[banda]
    return (f_mhz - f_low) / 0.1 + n_offs


def fmt_bytes(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def planificar(banda, e_ini, e_fin, ancho, solape):
    """Devuelve una lista de bloques {indice, fc_mhz, f_lo, f_hi, earfcn_ini, earfcn_fin}."""
    f_ini, f_fin = earfcn_a_mhz(banda, e_ini), earfcn_a_mhz(banda, e_fin)
    span = f_fin - f_ini
    paso = ancho - solape
    if span <= ancho:
        n = 1
    else:
        n = math.ceil(round((span - ancho) / paso, 9)) + 1
    centro = (f_ini + f_fin) / 2
    bloques = []
    for i in range(n):
        fc = centro + (i - (n - 1) / 2) * paso
        f_lo, f_hi = fc - ancho / 2, fc + ancho / 2
        # EARFCN del rango pedido cuya frecuencia cae dentro del bloque
        cubiertos = [e for e in range(e_ini, e_fin + 1)
                     if f_lo - 1e-9 <= earfcn_a_mhz(banda, e) <= f_hi + 1e-9]
        bloques.append({
            "indice": i + 1,
            "fc_mhz": round(fc, 4),
            "f_lo_mhz": round(f_lo, 4),
            "f_hi_mhz": round(f_hi, 4),
            "earfcn_ini": cubiertos[0] if cubiertos else None,
            "earfcn_fin": cubiertos[-1] if cubiertos else None,
            "n_earfcn": len(cubiertos),
        })
    return bloques


def elegir_formato(pedido, bytes_total_sc16, libre, uno_a_la_vez, bytes_bloque_sc16):
    """Formato 'auto': sc16 si cabe con margen; si no, sc8 (la mitad de tamaño)."""
    if pedido != "auto":
        return pedido, None
    necesario = bytes_bloque_sc16 if uno_a_la_vez else bytes_total_sc16
    if necesario * (1 + RESERVA_FRAC) + RESERVA_BYTES <= libre:
        return "sc16", "hay espacio suficiente para sc16 (mejor rango dinamico)"
    return "sc8", "poco espacio libre para sc16: se usa sc8 (mitad de tamaño)"


PATRON_GRAB = re.compile(r"^b(\d+)_(\d{8}T\d{6}Z)_blq\d+_.*\.(iq|json|log|cf32|cf32\.json)$")


def grabaciones_previas(salida):
    """Archivos de grabaciones anteriores en la carpeta (de cualquier banda), por sesion."""
    salida = Path(salida).expanduser()
    ses = {}
    if salida.is_dir():
        for f in salida.iterdir():
            m = PATRON_GRAB.match(f.name)
            if m and f.is_file():
                k = f"b{m.group(1)}_{m.group(2)}"
                d = ses.setdefault(k, {"id": k, "banda": int(m.group(1)), "archivos": [], "bytes": 0})
                d["archivos"].append(f)
                d["bytes"] += f.stat().st_size
    return sorted(ses.values(), key=lambda d: d["id"])


def resumen_previas(previas):
    return "; ".join(f"banda {d['banda']} ({d['id'].split('_')[1]}, {len(d['archivos'])} archivos, "
                     f"{fmt_bytes(d['bytes'])})" for d in previas)


def borrar_previas(previas):
    n = 0
    for d in previas:
        for f in d["archivos"]:
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def nombre_base(banda, sello, b, formato):
    return f"b{banda}_{sello}_blq{b['indice']:02d}_{b['fc_mhz']:.1f}MHz_{formato}"


def grabar_bloque(cfg, b, formato, salida, sello, on_stderr=None, on_progreso=None,
                  snapshot=None, on_proc=None, archivo=None, duracion=None):
    """Graba un bloque con rx_iq_grabador y escribe el .json.

    cfg: objeto con banda, args, ganancia, duracion, antena, subdev, ref.
    on_stderr(texto), on_progreso(recibidas, total, O, D): callbacks opcionales.
    snapshot: ruta donde el grabador deja las ultimas muestras (espectro en vivo).
    on_proc(proc): recibe el subprocess para poder detenerlo desde fuera.
    archivo: si se da (p.ej. /dev/null para monitorizar) no se escribe .json.
    Devuelve dict con stats (None si fallo), iq, json, log y codigo.
    """
    duracion = cfg.duracion if duracion is None else duracion
    base = nombre_base(cfg.banda, sello, b, formato)
    f_iq, f_json, f_log = (salida / (base + ext) for ext in (".iq", ".json", ".log"))
    if archivo:
        f_iq, f_log = Path(archivo), salida / "monitor.log"
    cmd = [str(GRABADOR), "--args", cfg.args, "--file", str(f_iq),
           "--freq", f"{b['fc_mhz']*1e6:.0f}", "--rate", f"{TASA_SPS:.0f}",
           "--gain", str(cfg.ganancia), "--duration", str(duracion),
           "--format", formato, "--ant", cfg.antena, "--subdev", cfg.subdev,
           "--ref", cfg.ref]
    if snapshot:
        cmd += ["--snapshot", str(snapshot)]
    stats = None
    with open(f_log, "w") as log:
        log.write("CMD: " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        if on_proc:
            on_proc(proc)

        def leer_stderr():
            for ch in iter(lambda: proc.stderr.read(1), ""):
                log.write(ch)
                if on_stderr:
                    on_stderr(ch)
        t = threading.Thread(target=leer_stderr, daemon=True)
        t.start()
        try:
            for linea in proc.stdout:
                if linea.startswith("PROGRESO "):
                    if on_progreso:
                        on_progreso(*map(int, linea.split()[1:5]))
                    continue
                log.write(linea)
                if linea.startswith("RESUMEN_JSON "):
                    stats = json.loads(linea[len("RESUMEN_JSON "):])
            proc.wait()
        except BaseException:
            proc.terminate()
            proc.wait()
            raise
        finally:
            t.join(timeout=2)

    if stats is not None and not archivo:
        meta = {
            "archivo_iq": f_iq.name,
            "banda": cfg.banda,
            "frecuencia_central_hz": b["fc_mhz"] * 1e6,
            "frecuencia_real_hz": stats["frecuencia_real_hz"],
            "rango_cubierto_mhz": [b["f_lo_mhz"], b["f_hi_mhz"]],
            "tasa_muestreo_sps": stats["tasa_real_sps"],
            "master_clock_rate_hz": MCR_HZ,
            "ganancia_db": stats["ganancia_real_db"],
            "formato": formato,
            "formato_descripcion": ("int16 I,Q intercalados, little-endian" if formato == "sc16"
                                    else "int8 I,Q intercalados"),
            "antena": stats["antena"],
            "subdev": cfg.subdev,
            "dispositivo": cfg.args,
            "hora_inicio_utc": stats["hora_inicio_utc"],
            "duracion_pedida_s": duracion,
            "duracion_real_s": stats["muestras_grabadas"] / stats["tasa_real_sps"],
            "muestras": stats["muestras_grabadas"],
            "earfcn_cubiertos": {"inicio": b["earfcn_ini"], "fin": b["earfcn_fin"],
                                 "cantidad": b["n_earfcn"]},
            "overflows_O": stats["overflows_O"],
            "paquetes_perdidos_D": stats["paquetes_perdidos_D"],
            "timeouts": stats["timeouts"],
            "lo_enganchado": stats["lo_enganchado"],
            "interrumpido": stats["interrumpido"],
        }
        with open(f_json, "w") as fj:
            json.dump(meta, fj, indent=2, ensure_ascii=False)
    return {"stats": stats, "iq": f_iq, "json": f_json, "log": f_log, "codigo": proc.returncode}


def main():
    p = argparse.ArgumentParser(
        description="Graba IQ crudo LTE (DL) por bloques con USRP X300. SOLO RECEPCION.",
        formatter_class=es_argparse.Formato)
    p.add_argument("--banda", type=int, required=True, help="banda LTE (p.ej. 2, 4, 28, 66)")
    p.add_argument("--earfcn-inicio", type=int, required=True, help="EARFCN DL inicial")
    p.add_argument("--earfcn-fin", type=int, required=True, help="EARFCN DL final")
    p.add_argument("--ancho", type=float, default=20.0, help="ancho de cada bloque (MHz)")
    p.add_argument("--solape", type=float, default=5.0, help="solape entre bloques (MHz)")
    p.add_argument("--duracion", type=float, default=180.0, help="duracion por bloque (s)")
    p.add_argument("--ganancia", type=float, default=30.0, help="ganancia RX (dB)")
    p.add_argument("--formato", choices=["auto", "sc16", "sc8"], default="auto",
                   help="auto = sc16 si hay espacio, si no sc8")
    p.add_argument("--salida", default=str(Path.home() / "capturas_iq"), help="carpeta de salida")
    p.add_argument("--antena", default="RX2", help="puerto de antena (RX2 o TX/RX)")
    p.add_argument("--subdev", default="A:0", help="subdispositivo / tarjeta RX (A:0 o B:0)")
    p.add_argument("--args", default="type=x300,addr=192.168.10.2",
                   help="argumentos del dispositivo UHD")
    p.add_argument("--ref", default="internal", help="referencia de reloj: internal|external|gpsdo")
    p.add_argument("--dry-run", action="store_true", help="solo mostrar el plan, no grabar")
    p.add_argument("--uno-a-la-vez", action="store_true",
                   help="grabar un bloque y esperar confirmacion antes del siguiente")
    p.add_argument("--borrar-anteriores", action="store_true",
                   help="borrar sin preguntar las grabaciones anteriores de la carpeta")
    a = p.parse_args()

    # --- Validaciones ---------------------------------------------------------
    try:
        if a.earfcn_fin < a.earfcn_inicio:
            a.earfcn_inicio, a.earfcn_fin = a.earfcn_fin, a.earfcn_inicio
        earfcn_a_mhz(a.banda, a.earfcn_inicio)
        earfcn_a_mhz(a.banda, a.earfcn_fin)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    if a.solape < 0 or a.solape >= a.ancho:
        sys.exit("ERROR: el solape debe ser >= 0 y menor que el ancho del bloque")
    if a.ancho > TASA_SPS / 1e6:
        sys.exit(f"ERROR: con {TASA_SPS/1e6:.0f} Msps el ancho de bloque no puede superar "
                 f"{TASA_SPS/1e6:.0f} MHz")
    if a.duracion <= 0:
        sys.exit("ERROR: la duracion debe ser > 0")
    if "master_clock_rate" not in a.args:
        a.args += f",master_clock_rate={MCR_HZ:.0f}"

    bloques = planificar(a.banda, a.earfcn_inicio, a.earfcn_fin, a.ancho, a.solape)
    for b in bloques:
        if not FREQ_MIN_MHZ <= b["fc_mhz"] <= FREQ_MAX_MHZ:
            sys.exit(f"ERROR: frecuencia {b['fc_mhz']} MHz fuera del rango del X300")

    salida = Path(a.salida).expanduser().resolve()
    salida.mkdir(parents=True, exist_ok=True)
    previas = grabaciones_previas(salida)
    bytes_previas = sum(d["bytes"] for d in previas)
    libre = shutil.disk_usage(salida).free + bytes_previas  # se borran antes de grabar

    n_muestras = int(round(TASA_SPS * a.duracion))
    bytes_bloque_sc16 = n_muestras * BYTES_POR_MUESTRA["sc16"]
    formato, motivo = elegir_formato(a.formato, bytes_bloque_sc16 * len(bloques), libre,
                                     a.uno_a_la_vez, bytes_bloque_sc16)
    bytes_bloque = n_muestras * BYTES_POR_MUESTRA[formato]
    bytes_total = bytes_bloque * len(bloques)

    # --- Plan -----------------------------------------------------------------
    f_ini = earfcn_a_mhz(a.banda, a.earfcn_inicio)
    f_fin = earfcn_a_mhz(a.banda, a.earfcn_fin)
    print("=" * 78)
    print(f" PLAN DE GRABACION  -  Banda {a.banda} DL, EARFCN {a.earfcn_inicio}-{a.earfcn_fin} "
          f"({f_ini:.1f}-{f_fin:.1f} MHz)")
    print("=" * 78)
    print(f" Dispositivo : {a.args}   (SOLO RX)")
    print(f" Antena/RX   : {a.antena}  subdev {a.subdev}   ganancia {a.ganancia} dB")
    print(f" Tasa        : {TASA_SPS/1e6:.0f} Msps ({MCR_HZ/1e6:.0f} MHz / 10)")
    print(f" Bloques     : {len(bloques)} x {a.ancho} MHz, solape {a.solape} MHz, "
          f"{a.duracion:.0f} s cada uno")
    print(f" Formato     : {formato}" + (f"  ({motivo})" if motivo else ""))
    print("-" * 78)
    print(f" {'#':>2}  {'Fc (MHz)':>10}  {'Cubre (MHz)':>21}  {'EARFCN cubiertos':>20}  {'Tamaño':>10}")
    for b in bloques:
        rango = (f"{b['earfcn_ini']}-{b['earfcn_fin']} ({b['n_earfcn']})"
                 if b["n_earfcn"] else "ninguno")
        print(f" {b['indice']:>2}  {b['fc_mhz']:>10.3f}  "
              f"{b['f_lo_mhz']:>9.3f}-{b['f_hi_mhz']:<10.3f}  {rango:>20}  {fmt_bytes(bytes_bloque):>10}")
    print("-" * 78)
    print(f" Tamaño total estimado : {fmt_bytes(bytes_total)}   "
          f"(sc16 serian {fmt_bytes(bytes_bloque_sc16*len(bloques))})")
    print(f" Espacio libre en {salida} : {fmt_bytes(libre)}"
          + (f"  (incluye {fmt_bytes(bytes_previas)} de grabaciones anteriores que se borraran)"
             if previas else ""))
    if previas:
        print(f" AVISO: antes de grabar se ELIMINARAN las grabaciones anteriores: "
              f"{resumen_previas(previas)}")
    print(f" Tiempo total de grabacion: ~{len(bloques)*a.duracion/60:.1f} min")

    necesario = bytes_bloque if a.uno_a_la_vez else bytes_total
    margen = necesario * RESERVA_FRAC + RESERVA_BYTES
    cabe = necesario + margen <= libre
    if a.uno_a_la_vez:
        print(" Modo uno-a-la-vez: se exige espacio para 1 bloque y se comprueba antes de cada uno.")
        if bytes_total + margen > libre:
            print(" AVISO: el total no cabe; tendras que procesar y borrar entre bloques.")
    print("=" * 78)

    if not cabe:
        print(f"\n*** ESPACIO INSUFICIENTE: hacen falta {fmt_bytes(necesario + margen)} "
              f"(incluye reserva) y hay {fmt_bytes(libre)}. No se empieza. ***")
        if formato == "sc16":
            print("    Prueba con --formato sc8, menos --duracion, o --uno-a-la-vez.")
        sys.exit(2)

    if a.dry_run:
        print("\n--dry-run: no se graba nada.")
        return

    if not GRABADOR.exists():
        sys.exit(f"ERROR: no existe {GRABADOR}. Ejecuta primero {DIR_SCRIPT/'compilar.sh'}")

    if previas:
        if not a.borrar_anteriores:
            try:
                r = input(f"\nSe eliminaran {sum(len(d['archivos']) for d in previas)} archivos "
                          f"({fmt_bytes(bytes_previas)}) de: {resumen_previas(previas)}.\n"
                          "¿Borrar y grabar? [s/N]: ")
            except EOFError:
                r = ""
            if r.strip().lower() not in ("s", "si", "sí", "y", "yes"):
                sys.exit("Cancelado: no se borro ni se grabo nada.")
        n = borrar_previas(previas)
        print(f"Borrados {n} archivos de grabaciones anteriores.")

    sello = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    resumen = []
    for b in bloques:
        libre = shutil.disk_usage(salida).free
        if bytes_bloque + RESERVA_BYTES > libre:
            print(f"\n*** ESPACIO INSUFICIENTE para el bloque {b['indice']} "
                  f"({fmt_bytes(libre)} libres). Se detiene. ***")
            break

        print(f"\n>>> Bloque {b['indice']}/{len(bloques)}: Fc {b['fc_mhz']:.3f} MHz, "
              f"EARFCN {b['earfcn_ini']}-{b['earfcn_fin']}, {a.duracion:.0f} s")

        def eco(ch):
            sys.stderr.write(ch)
            sys.stderr.flush()
        try:
            r = grabar_bloque(a, b, formato, salida, sello, on_stderr=eco)
        except KeyboardInterrupt:
            print("\nInterrumpido.")
            break
        stats, f_iq, f_json, f_log = r["stats"], r["iq"], r["json"], r["log"]
        if stats is None:
            print(f"*** ERROR: el grabador fallo (codigo {r['codigo']}). Mira {f_log}")
            break


        problemas = stats["overflows_O"] + stats["paquetes_perdidos_D"]
        if problemas:
            print("\n" + "!" * 78)
            print(f"!!! AVISO: bloque {b['indice']} con {stats['overflows_O']} OVERFLOWS (O) y "
                  f"{stats['paquetes_perdidos_D']} paquetes perdidos (D).")
            print("!!! Faltan muestras: la captura tiene huecos y puede fallar la decodificacion.")
            print("!!! Sugerencias: --formato sc8, disco mas rapido (SSD), cerrar programas,")
            print("!!!   o aumentar el buffer de red: sudo sysctl -w net.core.rmem_max=33554432")
            print("!" * 78)
        else:
            print(f"    OK: sin overflows. {fmt_bytes(f_iq.stat().st_size)} -> {f_json.name}")
        resumen.append((b, stats))

        if stats["interrumpido"]:
            print("Grabacion interrumpida por el usuario.")
            break
        if a.uno_a_la_vez and b is not bloques[-1]:
            try:
                r = input("\nBloque terminado. Procesa/borra lo que necesites y pulsa Enter "
                          "para el siguiente (q = salir): ")
            except EOFError:
                r = "q"
            if r.strip().lower() == "q":
                break

    print("\n" + "=" * 78)
    print(" RESUMEN")
    for b, s in resumen:
        estado = "OK" if s["overflows_O"] + s["paquetes_perdidos_D"] == 0 else \
            f"O={s['overflows_O']} D={s['paquetes_perdidos_D']}"
        print(f"  Bloque {b['indice']:>2}  {b['fc_mhz']:>10.3f} MHz  {estado}")
    print(f"  Archivos en {salida}")
    print("=" * 78)


if __name__ == "__main__":
    main()
