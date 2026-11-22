#!/usr/bin/env python3
"""
iq_a_cf32.py - Convierte capturas sc16/sc8 de grabar_lte.py a complejo float32 (cf32),
el formato que lee srsRAN (pdsch_ue -i archivo).

Modo simple (misma tasa, 20 Msps):
  ./iq_a_cf32.py captura.iq

Modo celda (recomendado para pdsch_ue): centra un EARFCN en DC, filtra y
remuestrea a la tasa que srsRAN espera para ese numero de PRB:
  ./iq_a_cf32.py captura.iq --earfcn 1000 --prb 100
  pdsch_ue -i captura_e1000_100prb.cf32 -p 100 ...

Tasas srsRAN (este build esta compilado con USE_LTE_RATES=OFF -> tasas reducidas):
  PRB      6     15     25     50     75     100
  red.   1.92   3.84   5.76  11.52  15.36  23.04  Msps
  LTE    1.92   3.84   7.68  15.36  23.04  30.72  Msps  (--tasas-lte)

El formato se lee del .json que acompaña al .iq; si no existe, use --formato.
Se procesa por trozos, asi que no hace falta cargar el archivo entero en memoria.
"""

import argparse
import json
import math
import sys
from fractions import Fraction
from pathlib import Path

import es_argparse  # textos de argparse en español

import numpy as np

PRB_VALIDOS = (6, 15, 25, 50, 75, 100)
TASA_REDUCIDA = {6: 1.92e6, 15: 3.84e6, 25: 5.76e6, 50: 11.52e6, 75: 15.36e6, 100: 23.04e6}
TASA_LTE = {6: 1.92e6, 15: 3.84e6, 25: 7.68e6, 50: 15.36e6, 75: 23.04e6, 100: 30.72e6}
BW_MHZ = {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}
ESCALA = {"sc16": 1 / 32768.0, "sc8": 1 / 128.0}
DTYPE = {"sc16": np.int16, "sc8": np.int8}


def cargar_meta(f_iq: Path):
    f_json = f_iq.with_suffix(".json")
    if f_json.exists():
        with open(f_json) as f:
            return json.load(f)
    return None


def earfcn_a_hz(banda, earfcn):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grabar_lte import earfcn_a_mhz
    return earfcn_a_mhz(banda, earfcn) * 1e6


def main():
    p = argparse.ArgumentParser(description="Convierte .iq sc16/sc8 a cf32 para srsRAN.",
                                formatter_class=es_argparse.Formato)
    p.add_argument("entrada", nargs="+", help="archivo(s) .iq")
    p.add_argument("--formato", choices=["sc16", "sc8"], help="si no hay .json")
    p.add_argument("--salida", help="archivo de salida (solo con una entrada)")
    p.add_argument("--earfcn", type=int, help="centrar este EARFCN en DC (requiere .json)")
    p.add_argument("--offset-hz", type=float, help="desplazamiento manual: frecuencia de la "
                   "celda menos la frecuencia central de la captura (Hz)")
    p.add_argument("--prb", type=int, choices=PRB_VALIDOS,
                   help="PRB de la celda: filtra y remuestrea a la tasa de srsRAN")
    p.add_argument("--tasas-lte", action="store_true",
                   help="usar tasas LTE estandar (srsRAN compilado con USE_LTE_RATES=ON)")
    p.add_argument("--inicio", type=float, default=0.0, help="segundo inicial a convertir")
    p.add_argument("--segundos", type=float, help="convertir solo N segundos")
    p.add_argument("--borrar-original", action="store_true",
                   help="borrar el .iq si la conversion termina bien")
    a = p.parse_args()

    if a.salida and len(a.entrada) > 1:
        sys.exit("--salida solo se puede usar con un archivo de entrada")

    for nombre in a.entrada:
        convertir(Path(nombre), a)


def convertir(f_iq: Path, a):
    meta = cargar_meta(f_iq)
    formato = a.formato or (meta or {}).get("formato")
    if formato not in ESCALA:
        sys.exit(f"{f_iq}: no se sabe el formato (falta .json); use --formato sc16|sc8")
    tasa_in = float((meta or {}).get("tasa_muestreo_sps", 20e6))

    # Desplazamiento en frecuencia
    offset = 0.0
    if a.earfcn is not None:
        if not meta:
            sys.exit(f"{f_iq}: --earfcn necesita el .json con banda y frecuencia central")
        f_celda = earfcn_a_hz(meta["banda"], a.earfcn)
        offset = f_celda - meta["frecuencia_real_hz"]
    elif a.offset_hz is not None:
        offset = a.offset_hz

    # Remuestreo
    resample = None
    tasa_out = tasa_in
    if a.prb:
        tasa_out = (TASA_LTE if a.tasas_lte else TASA_REDUCIDA)[a.prb]
        bw_celda = BW_MHZ[a.prb] * 1e6
        if abs(offset) + bw_celda / 2 > tasa_in / 2:
            print(f"AVISO {f_iq.name}: la celda de {BW_MHZ[a.prb]} MHz desplazada "
                  f"{offset/1e6:+.2f} MHz no cabe entera en la captura de "
                  f"{tasa_in/1e6:.0f} Msps; tendra bordes cortados.")
        fr = Fraction(tasa_out / tasa_in).limit_denominator(1000)
        up, down = fr.numerator, fr.denominator
        from scipy import signal
        # Filtro paso bajo al ancho de la celda (sobre la tasa intermedia up*tasa_in)
        n_taps = 64 * max(up, down) + 1
        corte = min(bw_celda / 2 * 1.05, tasa_out / 2 * 0.98) / (tasa_in * up / 2)
        h = signal.firwin(n_taps, corte)
        resample = (up, down, h, signal)
    elif abs(offset) > tasa_in / 2:
        sys.exit(f"{f_iq}: el EARFCN pedido esta fuera de la captura ({offset/1e6:+.2f} MHz)")

    sufijo = ""
    if a.earfcn is not None:
        sufijo += f"_e{a.earfcn}"
    if a.prb:
        sufijo += f"_{a.prb}prb"
    f_out = Path(a.salida) if a.salida else f_iq.with_name(f_iq.stem + sufijo + ".cf32")

    bytes_muestra = 2 * np.dtype(DTYPE[formato]).itemsize
    total = f_iq.stat().st_size // bytes_muestra
    ini = int(a.inicio * tasa_in)
    fin = total if a.segundos is None else min(total, ini + int(a.segundos * tasa_in))
    n = max(0, fin - ini)
    print(f"{f_iq.name}: {formato}, {tasa_in/1e6:.2f} Msps, {n} muestras "
          f"({n/tasa_in:.1f} s) -> {f_out.name}")
    if offset:
        print(f"   desplazamiento {offset/1e6:+.4f} MHz a DC")
    if resample:
        print(f"   remuestreo {tasa_in/1e6:.2f} -> {tasa_out/1e6:.2f} Msps "
              f"(x{resample[0]}/{resample[1]}), filtro {BW_MHZ[a.prb]} MHz")

    datos = np.memmap(f_iq, dtype=DTYPE[formato], mode="r")
    w = 2 * math.pi * (-offset) / tasa_in  # rad/muestra (lleva la celda a DC)

    def leer(desde, hasta):
        """Muestras complejas [desde, hasta) del archivo, con ceros fuera de [0, total)."""
        out = np.zeros(hasta - desde, np.complex64)
        d, h_ = max(desde, 0), min(hasta, total)
        if h_ > d:
            raw = np.asarray(datos[2 * d: 2 * h_], dtype=np.float32) * ESCALA[formato]
            x = raw[0::2] + 1j * raw[1::2]
            if offset:
                idx = np.arange(d, h_, dtype=np.float64)
                x = x * np.exp(1j * np.mod(w * idx, 2 * math.pi))
            out[d - desde: h_ - desde] = x
        return out

    escrito = 0
    with open(f_out, "wb") as fo:
        if resample:
            up, down, h, signal = resample
            # trozos multiplo de 'down' -> cada trozo produce exactamente m*up/down muestras
            trozo = down * max(1, int(tasa_in) // down)
            ini_al = (ini // down) * down
            ctx = down * (len(h) // (up) // down + 2)  # contexto a cada lado
            pos = ini_al
            while pos < fin:
                m = min(trozo, fin - pos)
                m_al = int(math.ceil(m / down)) * down
                x = leer(pos - ctx, pos + m_al + ctx)
                y = signal.resample_poly(x, up, down, window=h)
                o0 = ctx * up // down
                n_out = (m * up) // down
                y[o0: o0 + n_out].astype(np.complex64).tofile(fo)
                escrito += n_out
                pos += m
                print(f"\r   {100*(pos-ini_al)/max(fin-ini_al,1):5.1f} %", end="", flush=True)
        else:
            trozo = int(tasa_in)
            pos = ini
            while pos < fin:
                m = min(trozo, fin - pos)
                leer(pos, pos + m).tofile(fo)
                escrito += m
                pos += m
                print(f"\r   {100*(pos-ini)/max(n,1):5.1f} %", end="", flush=True)
    print(f"\r   OK: {escrito} muestras cf32 ({f_out.stat().st_size/1024**3:.2f} GiB), "
          f"{tasa_out/1e6:.2f} Msps")

    meta_out = dict(meta or {})
    meta_out.update({"archivo_cf32": f_out.name, "origen": f_iq.name, "formato": "cf32",
                     "tasa_muestreo_sps": tasa_out, "desplazamiento_hz": offset,
                     "earfcn_centrado": a.earfcn, "prb": a.prb})
    with open(str(f_out) + ".json", "w") as f:
        json.dump(meta_out, f, indent=2, ensure_ascii=False)

    if a.borrar_original:
        f_iq.unlink()
        print(f"   borrado {f_iq.name}")


if __name__ == "__main__":
    main()
