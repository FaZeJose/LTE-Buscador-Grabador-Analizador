#!/usr/bin/env python3
"""
analizar_grabacion.py - Busca y decodifica celdas LTE en las grabaciones IQ de grabador_iq.

Pensado para senales intermitentes y con Doppler (p. ej. Starlink Direct-to-Cell):
en vez de escuchar ~0,5 s por EARFCN como un buscador en vivo, revisa TODA la
grabacion (los 180 s de cada bloque) en cada EARFCN candidato.

Etapas por bloque:
  0) Espectrograma de la grabacion completa (para ver y para elegir candidatos).
  1) Deteccion PSS en todos los EARFCN candidatos x hipotesis de Doppler a la vez:
     una FFT grande por trama, y por canal solo una IFFT corta con el filtro adaptado
     a la PSS. Se integra de forma no coherente en ventanas (100 ms por defecto),
     lo que gana mucha sensibilidad frente a mirar una sola trama.
  2) En los mejores momentos de cada deteccion se extraen ~3 s a 1,92 Msps y se
     decodifica el MIB con analizar_celda_iq (cadena de srsRAN).
  3) Con el MIB (PRB) se extraen ~3 s a la tasa de la celda y se decodifican
     SIB1 (PLMN, TAC, ECI...) y SIB2.

La banda, la frecuencia de cada bloque, el formato y la tasa se leen de los .json
que escribe el grabador: no hay que configurar nada a mano.

Uso:
  ./analizar_grabacion.py --carpeta ~/capturas_iq                      # analiza la ultima sesion
  ./analizar_grabacion.py --earfcn 1175,1176 --doppler 20              # solo esos EARFCN
  ./analizar_grabacion.py --modo exhaustivo                            # todos los EARFCN (lento)
"""
import argparse
import concurrent.futures as cf
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from scipy import fft as sfft
import ctypes

DIR = Path(__file__).resolve().parent
REPO = DIR.parent.parent
DECODER = Path(os.environ.get("ANALIZAR_CELDA_IQ", REPO / "build/lib/examples/analizar_celda_iq"))
sys.path.insert(0, str(DIR.parent / "grabador_iq"))
try:
    import es_argparse  # noqa: F401  (textos de argparse en español)
    FORMATO_AYUDA = es_argparse.Formato
except ImportError:
    FORMATO_AYUDA = argparse.ArgumentDefaultsHelpFormatter

# 3GPP TS 36.101 tabla 5.7.3-1 (bajada)
TABLA_EARFCN_DL = {
    1: (2110.0, 0, 0, 599), 2: (1930.0, 600, 600, 1199), 3: (1805.0, 1200, 1200, 1949),
    4: (2110.0, 1950, 1950, 2399), 5: (869.0, 2400, 2400, 2649), 7: (2620.0, 2750, 2750, 3449),
    8: (925.0, 3450, 3450, 3799), 12: (729.0, 5010, 5010, 5179), 13: (746.0, 5180, 5180, 5279),
    14: (758.0, 5280, 5280, 5379), 17: (734.0, 5730, 5730, 5849), 20: (791.0, 6150, 6150, 6449),
    25: (1930.0, 8040, 8040, 8689), 26: (859.0, 8690, 8690, 9039), 28: (758.0, 9210, 9210, 9659),
    38: (2570.0, 37750, 37750, 38249), 40: (2300.0, 38650, 38650, 39649),
    41: (2496.0, 39650, 39650, 41589), 66: (2110.0, 66436, 66436, 67335),
    71: (617.0, 68586, 68586, 68935),
}

# Tasas de srsRAN compiladas con USE_LTE_RATES=OFF (tasas reducidas)
TASA_PRB = {6: 1.92e6, 15: 3.84e6, 25: 5.76e6, 50: 11.52e6, 75: 15.36e6, 100: 23.04e6}
BW_PRB = {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}

# Canalizador: FFT de N muestras a 20 Msps, solape OV (descartado), avance H
N_FFT = 128000          # 6,4 ms a 20 Msps; 312,5 Hz por bin
OV = 4000
H = N_FFT - OV          # 124000: multiplo de 125 -> indices enteros a todas las tasas
DET_RATE = 0.96e6       # tasa de la correlacion PSS (cubre los 62 subportadores de la PSS)
PERIODO_PSS = 4800      # 5 ms a 0,96 Msps
EXCL_LOBULO = 3         # muestras excluidas alrededor del pico al calcular PSR

# Operadores conocidos (MCC, MNC) -> nombre. Se puede ampliar con operadores.json
OPERADORES = {
    ("716", "06"): "Movistar Peru (terrestre)",
    ("716", "10"): "Claro Peru (terrestre)",
    ("716", "20"): "Claro Peru (terrestre)",
    ("716", "15"): "Bitel Peru (terrestre)",
    ("716", "07"): "Entel Peru (terrestre o posible satelital)",
    ("716", "17"): "Entel Peru (terrestre o posible satelital)",
    ("730", "29"): "Entel SpaceX Chile (SATELITAL)",
}
OP_DESCONOCIDO = "DESCONOCIDO - posible satelital"


def cargar_operadores():
    f = DIR / "operadores.json"
    if f.exists():
        try:
            for e in json.load(open(f)):
                OPERADORES[(str(e["mcc"]), str(e["mnc"]))] = e["nombre"]
        except (ValueError, KeyError, TypeError) as ex:
            print(f"AVISO: operadores.json no valido: {ex}", file=sys.stderr)


def nombre_operador(plmns):
    mejor = None
    for p in plmns:
        n = OPERADORES.get((p["mcc"], p["mnc"]), OP_DESCONOCIDO)
        if mejor is None or mejor == OP_DESCONOCIDO or "SATELITAL" in n:
            mejor = n
    return mejor or OP_DESCONOCIDO


def earfcn_a_hz(banda, n):
    f_low, n_offs, _, _ = TABLA_EARFCN_DL[banda]
    return (f_low + 0.1 * (n - n_offs)) * 1e6


def hz_a_earfcn(banda, f):
    f_low, n_offs, _, _ = TABLA_EARFCN_DL[banda]
    return (f / 1e6 - f_low) / 0.1 + n_offs


# ------------------------------------------------------------------ sesiones
PATRON = re.compile(r"^b(\d+)_(\d{8}T\d{6}Z)_blq(\d+)_.*\.json$")


def listar_sesiones(carpeta):
    """Agrupa los .json del grabador por sesion (banda + sello de hora)."""
    carpeta = Path(carpeta).expanduser()
    ses = {}
    if not carpeta.is_dir():
        return []
    for fj in sorted(carpeta.glob("b*_*_blq*_*.json")):
        m = PATRON.match(fj.name)
        if not m or fj.name.endswith(".cf32.json"):
            continue
        try:
            meta = json.load(open(fj))
        except ValueError:
            continue
        fiq = fj.with_name(meta.get("archivo_iq", fj.stem + ".iq"))
        if not fiq.exists():
            continue
        clave = f"b{m.group(1)}_{m.group(2)}"
        s = ses.setdefault(clave, {"id": clave, "banda": int(meta.get("banda", m.group(1))),
                                   "sello": m.group(2), "bloques": []})
        meta["_iq"] = str(fiq)
        meta["_json"] = str(fj)
        meta["_bytes"] = fiq.stat().st_size
        meta["_indice"] = int(m.group(3))
        s["bloques"].append(meta)
    out = []
    for s in ses.values():
        s["bloques"].sort(key=lambda b: b["_indice"])
        s["formato"] = s["bloques"][0].get("formato")
        s["bytes"] = sum(b["_bytes"] for b in s["bloques"])
        s["duracion_s"] = sum(b.get("duracion_real_s", 0) for b in s["bloques"])
        fr = [r for b in s["bloques"] for r in b.get("rango_cubierto_mhz", [])]
        s["rango_mhz"] = [min(fr), max(fr)] if fr else None
        out.append(s)
    out.sort(key=lambda s: s["sello"], reverse=True)
    return out


# ------------------------------------------------------------------ lectura
class Grabacion:
    """Acceso a un bloque .iq como muestras complejas (memmap, sin cargar todo)."""

    def __init__(self, meta):
        self.meta = meta
        self.fmt = meta["formato"]
        self.fs = float(meta["tasa_muestreo_sps"])
        self.fc = float(meta.get("frecuencia_real_hz") or meta["frecuencia_central_hz"])
        dt = np.int16 if self.fmt == "sc16" else np.int8
        self.esc = np.float32(1 / 32768 if self.fmt == "sc16" else 1 / 128)
        self.raw = np.memmap(meta["_iq"], dtype=dt, mode="r")
        self.n = self.raw.size // 2
        if abs(self.fs - 20e6) > 1:
            raise ValueError(f"Tasa {self.fs} no soportada (se espera 20 Msps)")

    def leer(self, s, n):
        """Muestras [s, s+n) como complex64; ceros fuera del archivo."""
        out = np.zeros(n, np.complex64)
        a, b = max(s, 0), min(s + n, self.n)
        if b > a:
            r = np.asarray(self.raw[2 * a:2 * b], dtype=np.float32) * self.esc
            out[a - s:b - s].real = r[0::2]
            out[a - s:b - s].imag = r[1::2]
        return out


_LIB = None


def lib_det():
    """Carga (y compila si hace falta) el nucleo C de deteccion PSS."""
    global _LIB
    if _LIB is None:
        so, src = DIR / "libpss_det.so", DIR / "pss_det.c"
        if not so.exists() or so.stat().st_mtime < src.stat().st_mtime:
            subprocess.run(["gcc", "-O3", "-march=native", "-fopenmp", "-shared", "-fPIC", str(src),
                            "-o", str(so), "-lfftw3f", "-lm"], check=True)
        L = ctypes.CDLL(str(so))
        L.det_crear.restype = ctypes.c_void_p
        L.det_crear.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int]
        L.det_trama.restype = ctypes.c_int
        L.det_trama.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_int]
        L.det_liberar.argtypes = [ctypes.c_void_p]
        _LIB = L
    return _LIB


def plantilla_pss(M, df):
    """Espectro (centrado, M bins de df Hz) de las 3 PSS como pulso OFDM de duracion 1/15 kHz."""
    T = 1 / 15000.0
    f = (np.arange(M) - M // 2) * df
    n = np.arange(62)
    k = np.where(n < 31, n - 31, n - 30)
    P = np.zeros((3, M), np.complex64)
    for nid2, u in enumerate((25, 29, 34)):
        d = np.where(n < 31, np.exp(-1j * np.pi * u * n * (n + 1) / 63),
                     np.exp(-1j * np.pi * u * (n + 1) * (n + 2) / 63))
        x = (f[:, None] - k[None, :] * 15000.0) * T
        P[nid2] = (np.sinc(x) * np.exp(-1j * np.pi * x) * d[None, :]).sum(1) * T
    P /= np.sqrt((np.abs(P) ** 2).sum(1, keepdims=True))
    return P


def extraer(g, t0, dur, f_off, rate):
    """Extrae [t0, t0+dur) s desplazando f_off Hz a DC y remuestreando a 'rate' (canalizador FFT)."""
    M = int(round(N_FFT * rate / g.fs))
    df = g.fs / N_FFT
    kk = int(round(f_off / df))
    frac = f_off - kk * df
    s0 = int(t0 * g.fs)
    n_out = int(round(dur * rate))
    q = rate / g.fs
    out = np.zeros(n_out + int(H * q) + 10, np.complex64)
    nb = min(M, N_FFT)
    m = np.arange(nb) - nb // 2
    # transicion suave en los bordes (acorta la respuesta del filtro "ladrillo")
    tap = np.ones(nb, np.float32)
    tw = max(8, nb // 40)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, tw))
    tap[:tw] = ramp
    tap[-tw:] = ramp[::-1]
    idx = (kk + m) % N_FFT
    pos = 0
    s = s0 - OV // 2
    while pos < n_out:
        X = sfft.fft(g.leer(s, N_FFT), workers=-1)
        Y = np.zeros(M, np.complex64)
        Y[(m + M) % M] = X[idx] * tap
        y = sfft.ifft(Y, workers=-1) * (M / N_FFT)
        a, b = int(OV // 2 * q), int((OV // 2 + H) * q)
        y = y[a:b] * np.exp(-2j * np.pi * kk * (s + OV // 2) / N_FFT)
        out[pos:pos + len(y)] = y
        pos += len(y)
        s += H
    out = out[:n_out]
    if frac:
        out *= np.exp(-2j * np.pi * frac * (s0 / g.fs + np.arange(n_out) / rate)).astype(np.complex64)
    return out


# ------------------------------------------------------------------ analisis
class Analizador:
    def __init__(self, cfg, emitir):
        self.cfg = cfg
        self.emitir = emitir
        self.parar = False
        self.celdas = {}        # (earfcn, pci) -> fila
        self.tmp = Path(tempfile.mkdtemp(prefix="analizador_iq_", dir="/dev/shm"
                                         if os.path.isdir("/dev/shm") else None))

    def log(self, txt, nivel=""):
        self.emitir({"t": "log", "txt": txt, "nivel": nivel})

    # --- candidatos ---
    def asignar_earfcn(self, sesion):
        """Cada EARFCN del rango se asigna al bloque donde queda mas centrado."""
        banda = sesion["banda"]
        _, _, nmin, nmax = TABLA_EARFCN_DL[banda]
        asign = {}
        for i, b in enumerate(sesion["bloques"]):
            fc = float(b.get("frecuencia_real_hz") or b["frecuencia_central_hz"])
            lo, hi = b["rango_cubierto_mhz"]
            for e in range(nmin, nmax + 1):
                f = earfcn_a_hz(banda, e) / 1e6
                if lo + 0.7 <= f <= hi - 0.7:
                    d = abs(f * 1e6 - fc)
                    if e not in asign or d < asign[e][1]:
                        asign[e] = (i, d)
        return {e: v[0] for e, v in asign.items()}

    def candidatos_espectro(self, banda, g, esp):
        """EARFCN candidatos a partir de las portadoras visibles en el espectro."""
        mx, med = esp["max_db"], esp["mean_db"]
        nb = len(mx)
        df = g.fs / nb
        piso = np.percentile(med, 20)
        sm = np.convolve(mx, np.ones(5) / 5, mode="same")
        mask = sm > piso + self.cfg.umbral_espectro_db
        regiones, i = [], 0
        while i < nb:
            if mask[i]:
                j = i
                while j < nb and mask[j]:
                    j += 1
                ancho = (j - i) * df
                if ancho >= 0.9e6:
                    regiones.append((i, j))
                i = j
            else:
                i += 1
        cand = set()
        for i, j in regiones:
            fa = g.fc + (i - nb / 2) * df
            fb = g.fc + (j - nb / 2) * df
            centros = [(fa + fb) / 2]
            if fb - fa > 6e6:  # varias portadoras pegadas
                for bw in (1.4, 3, 5, 10, 15, 20):
                    centros += [fa + bw * 0.5e6, fb - bw * 0.5e6]
            for c in centros:
                e0 = int(round(hz_a_earfcn(banda, c)))
                for d in range(-2, 3):
                    cand.add(e0 + d)
        return cand, regiones

    # --- etapa 0: espectrograma ---
    def espectrograma(self, g, paso_tramas=1, filas_s=0.25):
        nb = 1024
        n_tramas = max(1, (g.n - OV) // H)
        filas, fila_acc, fila_n = [], np.zeros(nb), 0
        tramas_fila = max(1, int(round(filas_s * g.fs / H)))
        mean_acc = np.zeros(nb)
        max_acc = np.full(nb, -1e9)
        w = np.hanning(N_FFT).astype(np.float32)
        for t in range(0, n_tramas, paso_tramas):
            if self.parar:
                break
            X = sfft.fft(g.leer(t * H, N_FFT) * w, workers=-1)
            p = np.fft.fftshift(np.abs(X) ** 2).reshape(nb, -1).mean(1)
            fila_acc += p
            fila_n += 1
            if fila_n * paso_tramas >= tramas_fila:
                filas.append(fila_acc / fila_n)
                fila_acc, fila_n = np.zeros(nb), 0
            if t % 200 == 0:
                self.emitir({"t": "progreso", "fase": "espectro", "pct": 100 * t / n_tramas})
        if fila_n:
            filas.append(fila_acc / fila_n)
        F = 10 * np.log10(np.array(filas) / (w.sum() ** 2) + 1e-20)
        mean_acc = 10 * np.log10(np.mean(np.array(filas), 0) / (w.sum() ** 2) + 1e-20)
        max_acc = F.max(0)
        return {"filas": F, "mean_db": mean_acc, "max_db": max_acc, "fila_s": filas_s}

    # --- etapa 1: deteccion PSS ---
    def detectar(self, banda, g, earfcns, dopplers):
        df = g.fs / N_FFT
        M = int(round(N_FFT * DET_RATE / g.fs))  # 6144
        P = plantilla_pss(M, df)
        Pc = np.ascontiguousarray(np.conj(np.fft.ifftshift(P, axes=1)), np.complex64)
        canales = [(e, d) for e in earfcns for d in dopplers]
        C = len(canales)
        koff = np.array([int(round((earfcn_a_hz(banda, e) + d - g.fc) / df)) % N_FFT
                         for e, d in canales], np.int32)
        K = max(1, int(round(self.cfg.integracion_ms / 5)))
        W = PERIODO_PSS * K
        n_val = int(H * M / N_FFT)
        n_tramas = max(1, (g.n - OV) // H)
        n_win = n_tramas * n_val // W + 1
        psr = np.zeros((n_win, C * 3), np.float32)
        pico = np.zeros((n_win, C * 3), np.int32)
        L = lib_det()
        h = L.det_crear(N_FFT, M, C, koff.ctypes.data, Pc.ctypes.data, n_val, PERIODO_PSS, K,
                        EXCL_LOBULO, os.cpu_count() or 4)
        maxv = n_val // W + 2
        o_psr = np.zeros((maxv, C * 3), np.float32)
        o_pico = np.zeros((maxv, C * 3), np.int32)
        win = 0
        t0 = time.time()
        try:
            for t in range(n_tramas):
                if self.parar:
                    break
                X = np.ascontiguousarray(sfft.fft(g.leer(t * H, N_FFT), workers=-1), np.complex64)
                nv = L.det_trama(h, X.ctypes.data, o_psr.ctypes.data, o_pico.ctypes.data, maxv)
                if nv:
                    k = min(nv, n_win - win)
                    psr[win:win + k] = o_psr[:k]
                    pico[win:win + k] = o_pico[:k]
                    win += k
                if t % 50 == 0:
                    vel = (t + 1) * H / g.fs / max(time.time() - t0, 1e-6)
                    self.emitir({"t": "progreso", "fase": "pss", "pct": 100 * t / n_tramas,
                                 "velocidad": round(vel, 2)})
        finally:
            L.det_liberar(h)
        psr = psr[:win].T.reshape(C, 3, win)
        pico = pico[:win].T.reshape(C, 3, win)
        return canales, psr, pico, K * 5e-3

    # --- etapa 2/3: decodificacion ---
    def decodificar(self, args):
        cmd = [str(DECODER)] + [str(a) for a in args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return []
        res = []
        for line in r.stdout.splitlines():
            if line.startswith("RESULTADO "):
                try:
                    res.append(json.loads(line[10:]))
                except ValueError:
                    pass
        return res

    def intentar_celda(self, banda, bloque_i, g, e, nid2, ventanas, t_win):
        """Prueba a decodificar en los mejores momentos de una deteccion."""
        cfg = self.cfg
        f_e = earfcn_a_hz(banda, e)
        hechos = []
        for (w, ps, d) in ventanas[:cfg.intentos]:
            if self.parar:
                break
            t_ini = max(0.0, w * t_win - 0.3)
            dur = min(cfg.ventana_s, g.n / g.fs - t_ini)
            if dur < 0.5:
                continue
            x = extraer(g, t_ini, dur, f_e + d - g.fc, 1.92e6)
            f1 = self.tmp / f"e{e}_n{nid2}_w{w}_mib.cf32"
            x.tofile(f1)
            rm = self.decodificar(["-f", f1, "-m", "mib", "-p", cfg.psr_srsran, "-n", 200,
                                   "-x", cfg.tramas_mib])
            f1.unlink(missing_ok=True)
            for r in rm:
                if r.get("pci", -1) < 0:
                    continue
                if r["pci"] % 3 != nid2:
                    continue
                fila = self.fila(banda, bloque_i, e, r["pci"])
                fila["psr_srsran"] = max(fila.get("psr_srsran", 0), r["psr"])
                if not r["mib_ok"]:
                    fila["estado"] = fila.get("estado") or "PCI (sin MIB)"
                    continue
                fila.update(prb=r["prb"], bw_mhz=BW_PRB.get(r["prb"]), puertos=r["ports"],
                            cp=r["cp"], phich=r.get("phich"), mib="OK")
                if fila.get("sib1") == "OK":
                    hechos.append(r["pci"])
                    continue
                rate = TASA_PRB.get(r["prb"])
                if not rate:
                    continue
                cfo_tot = d + r.get("cfo_hz", 0.0)
                x2 = extraer(g, t_ini, dur, f_e + cfo_tot - g.fc, rate)
                f2 = self.tmp / f"e{e}_p{r['pci']}_w{w}_sib.cf32"
                x2.tofile(f2)
                args = ["-f", f2, "-m", "sib", "-c", r["pci"], "-r", r["prb"],
                        "-t", max(1, r["ports"]), "-s", cfg.intentos_sib1]
                if r["cp"] == "ext":
                    args.append("-e")
                rs = self.decodificar(args)
                f2.unlink(missing_ok=True)
                for s in rs:
                    fila["cfo_hz"] = round(cfo_tot + s.get("cfo_hz", 0.0), 1)
                    if s.get("snr_db", -99) > -99:
                        fila["snr_db"] = s["snr_db"]
                    if s.get("sib1_ok"):
                        plmn = s.get("plmn", [])
                        fila.update(
                            sib1="OK", plmn=", ".join(f"{p['mcc']}-{p['mnc']}" for p in plmn),
                            mcc=plmn[0]["mcc"] if plmn else "", mnc=plmn[0]["mnc"] if plmn else "",
                            operador=nombre_operador(plmn), tac=s.get("tac"), eci=s.get("eci"),
                            enb_id=(s["eci"] >> 8) if s.get("eci") is not None else None,
                            cell_local=(s["eci"] & 0xFF) if s.get("eci") is not None else None,
                            cell_barred=s.get("cell_barred"), freq_band=s.get("freq_band"),
                            q_rx_lev_min_dbm=s.get("q_rx_lev_min_dbm"), p_max=s.get("p_max"),
                            sched=s.get("sched"), t_decodificado_s=round(t_ini, 2))
                        if s.get("sib2_ok"):
                            fila.update(sib2="OK", ul_earfcn=s.get("ul_earfcn"),
                                        ul_bw_prb=s.get("ul_bw_prb"), ac_barring=s.get("ac_barring"))
                    fila["estado"] = ("SIB1+SIB2" if fila.get("sib2") == "OK" else
                                      "SIB1" if fila.get("sib1") == "OK" else "MIB")
                self.emitir({"t": "celda", "fila": fila})
                if fila.get("sib1") == "OK":
                    hechos.append(r["pci"])
            if hechos:
                break
        return hechos

    def fila(self, banda, bloque_i, e, pci):
        k = (e, pci)
        if k not in self.celdas:
            self.celdas[k] = {"earfcn": e, "freq_mhz": round(earfcn_a_hz(banda, e) / 1e6, 1),
                              "pci": pci, "banda": banda, "bloque": bloque_i + 1,
                              "mib": "-", "sib1": "-", "sib2": "-", "operador": "",
                              "estado": ""}
        return self.celdas[k]

    # --- sesion completa ---
    def ejecutar(self, sesion):
        cfg = self.cfg
        banda = sesion["banda"]
        cargar_operadores()
        self.log(f"Sesión {sesion['id']}: banda {banda}, {len(sesion['bloques'])} bloque(s), "
                 f"{sesion['formato']}", "head")
        asign = self.asignar_earfcn(sesion)
        lista = set(cfg.earfcn or [])
        fuera = sorted(e for e in lista if e not in asign)
        if fuera:
            self.log(f"EARFCN fuera de lo grabado (se ignoran): {fuera}", "warn")
        dopplers = np.arange(-cfg.doppler_khz, cfg.doppler_khz + 1e-6, cfg.paso_doppler_khz) * 1e3
        if len(dopplers) == 0:
            dopplers = np.array([0.0])
        self.emitir({"t": "inicio", "sesion": sesion["id"], "banda": banda,
                     "n_bloques": len(sesion["bloques"]), "dopplers_hz": dopplers.tolist()})
        t_total = time.time()
        for i, meta in enumerate(sesion["bloques"]):
            if self.parar:
                break
            g = Grabacion(meta)
            dur = g.n / g.fs
            self.emitir({"t": "bloque", "i": i + 1, "fc_mhz": g.fc / 1e6, "dur_s": dur,
                         "rango_mhz": meta.get("rango_cubierto_mhz")})
            self.log(f"Bloque {i + 1}: Fc {g.fc / 1e6:.3f} MHz, {dur:.1f} s, {meta.get('formato')}"
                     + (f"  ⚠ grabado con {meta.get('overflows_O', 0)} overflows" if meta.get(
                         "overflows_O") else ""), "head")
            propios = sorted(e for e, b in asign.items() if b == i)
            # Etapa 0
            paso = 1 if cfg.modo != "auto" else 4
            esp = self.espectrograma(g, paso_tramas=paso)
            f_axis = [round((g.fc + (k - 512) * g.fs / 1024) / 1e6, 4) for k in (0, 1023)]
            self.emitir({"t": "espectro", "i": i + 1, "f_mhz": f_axis, "fila_s": esp["fila_s"] * 1,
                         "max_db": np.round(esp["max_db"], 1).tolist(),
                         "mean_db": np.round(esp["mean_db"], 1).tolist(),
                         "filas_db": np.round(esp["filas"][::max(1, len(esp["filas"]) // 400)], 0)
                         .astype(np.int16).tolist(),
                         "dur_s": dur})
            # Candidatos
            if cfg.modo == "exhaustivo":
                cand = set(propios)
            elif cfg.modo == "lista":
                cand = set()
            else:
                c_esp, reg = self.candidatos_espectro(banda, g, esp)
                cand = c_esp & set(propios)
                if reg:
                    self.log("Portadoras visibles: " + ", ".join(
                        f"{(g.fc + ((a + b) / 2 - 512) * g.fs / 1024) / 1e6:.2f} MHz "
                        f"(~{(b - a) * g.fs / 1024 / 1e6:.1f} MHz)" for a, b in reg))
            cand |= (lista & set(propios))
            cand = sorted(cand)
            if not cand:
                self.log("Sin EARFCN candidatos en este bloque.", "warn")
                continue
            self.log(f"Buscando PSS en {len(cand)} EARFCN × {len(dopplers)} Doppler "
                     f"({len(cand) * len(dopplers) * 3} correlaciones) durante {dur:.0f} s "
                     f"con integración de {cfg.integracion_ms:.0f} ms…")
            canales, psr, pico, t_win = self.detectar(banda, g, cand, dopplers)
            if self.parar:
                break
            # Agrupar por EARFCN y N_id2: mejor Doppler por ventana
            nD = len(dopplers)
            psr_e = psr.reshape(len(cand), nD, 3, -1)
            best_d = psr_e.argmax(1)               # (E,3,W)
            best = psr_e.max(1)                    # (E,3,W)
            detecciones = []
            for ei, e in enumerate(cand):
                for nid2 in range(3):
                    serie = best[ei, nid2]
                    ok = serie >= cfg.psr
                    # ruido: exigir varias ventanas salvo que el pico sea muy claro
                    if ok.sum() < cfg.ventanas_min and serie.max() < cfg.psr * 1.6:
                        continue
                    if not ok.any():
                        continue
                    ws = np.nonzero(ok)[0]
                    # Doppler refinado (interpolacion parabolica entre hipotesis)
                    dop = []
                    for w in ws:
                        bd = best_d[ei, nid2, w]
                        v = psr_e[ei, :, nid2, w]
                        dd = dopplers[bd]
                        if 0 < bd < nD - 1:
                            den = v[bd - 1] - 2 * v[bd] + v[bd + 1]
                            if den < 0:
                                dd += 0.5 * (v[bd - 1] - v[bd + 1]) / den * (dopplers[1] - dopplers[0])
                        dop.append(float(dd))
                    orden = sorted(zip(ws, serie[ws], dop), key=lambda z: -z[1])
                    # ventanas separadas al menos 2 s entre intentos
                    elegidas = []
                    for w, ps, dd in orden:
                        if all(abs(w - w2) * t_win >= 2.0 for w2, _, _ in elegidas):
                            elegidas.append((int(w), float(ps), dd))
                    det = {"earfcn": e, "nid2": nid2, "psr_max": round(float(serie.max()), 2),
                           "t_s": [round(float(w * t_win), 2) for w in ws],
                           "psr": [round(float(x), 2) for x in serie[ws]],
                           "doppler_hz": [round(x) for x in dop],
                           "presencia_pct": round(100 * len(ws) / len(serie), 1),
                           "bloque": i + 1, "ventanas": elegidas}
                    detecciones.append(det)
            # La PSS de una celda fuerte tambien "aparece" en EARFCN vecinos (ambiguedad
            # tiempo-frecuencia de la secuencia Zadoff-Chu) y con otro N_id2 en el mismo EARFCN.
            # Dos portadoras LTE no pueden estar a menos de ~1 MHz: se queda la mas fuerte.
            detecciones.sort(key=lambda d: -d["psr_max"])
            aceptadas, ecos = [], 0
            for d in detecciones:
                eco = False
                for a in aceptadas:
                    dist = abs(a["earfcn"] - d["earfcn"])
                    if 0 < dist <= 10 and d["psr_max"] < a["psr_max"]:
                        eco = True
                    elif dist == 0 and d["nid2"] != a["nid2"] and d["psr_max"] < 0.25 * a["psr_max"]:
                        eco = True
                if eco:
                    ecos += 1
                else:
                    aceptadas.append(d)
            if ecos:
                self.log(f"Descartados {ecos} ecos de celdas más fuertes en EARFCN vecinos.")
            detecciones = aceptadas
            self.log(f"Detecciones PSS en el bloque {i + 1}: {len(detecciones)}",
                     "ok" if detecciones else "")
            for d in detecciones:
                self.emitir({"t": "deteccion", "det": {k: v for k, v in d.items() if k != "ventanas"}})
            # Decodificar (en paralelo)
            vistos = set()
            trabajos = []
            for d in detecciones:
                vec = (d["nid2"], round(earfcn_a_hz(banda, d["earfcn"]) / 1e5))
                clave_vecina = [(d["nid2"], d["earfcn"] + k) for k in (-2, -1, 1, 2)]
                if any(cv in vistos for cv in clave_vecina) and d["psr_max"] < 3:
                    continue  # eco de una deteccion vecina mas fuerte
                vistos.add((d["nid2"], d["earfcn"]))
                trabajos.append(d)
            self.emitir({"t": "progreso", "fase": "decodificando", "pct": 0})
            with cf.ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) // 2)) as ex:
                fut = {ex.submit(self.intentar_celda, banda, i, g, d["earfcn"], d["nid2"],
                                 d["ventanas"], t_win): d for d in trabajos}
                hechos = 0
                for f in cf.as_completed(fut):
                    d = fut[f]
                    hechos += 1
                    try:
                        pcis = f.result()
                    except Exception as ex_:  # noqa
                        self.log(f"Error decodificando EARFCN {d['earfcn']}: {ex_}", "err")
                        pcis = []
                    # anotar presencia/Doppler en las filas de esa deteccion
                    for (e, pci), fila in self.celdas.items():
                        if e == d["earfcn"] and isinstance(pci, int) and pci % 3 == d["nid2"]:
                            fila.update(psr_max=d["psr_max"], presencia_pct=d["presencia_pct"],
                                        visible_s=[d["t_s"][0], d["t_s"][-1]],
                                        doppler_rango_hz=[min(d["doppler_hz"]), max(d["doppler_hz"])],
                                        bloque=d["bloque"])
                            self.emitir({"t": "celda", "fila": fila})
                    if not any(e == d["earfcn"] and isinstance(pci, int) and pci % 3 == d["nid2"]
                               for (e, pci) in self.celdas):
                        fila = {"earfcn": d["earfcn"],
                                "freq_mhz": round(earfcn_a_hz(banda, d["earfcn"]) / 1e6, 1),
                                "pci": f"?(N_id2={d['nid2']})", "banda": banda, "bloque": d["bloque"],
                                "mib": "-", "sib1": "-", "sib2": "-", "operador": "",
                                "estado": "solo PSS", "psr_max": d["psr_max"],
                                "presencia_pct": d["presencia_pct"],
                                "visible_s": [d["t_s"][0], d["t_s"][-1]],
                                "doppler_rango_hz": [min(d["doppler_hz"]), max(d["doppler_hz"])]}
                        self.celdas[(d["earfcn"], fila["pci"])] = fila
                        self.emitir({"t": "celda", "fila": fila})
                    self.emitir({"t": "progreso", "fase": "decodificando",
                                 "pct": 100 * hechos / max(1, len(trabajos))})
        self.log(f"Análisis terminado en {time.time() - t_total:.0f} s. Celdas: "
                 f"{sum(1 for f in self.celdas.values() if f.get('mib') == 'OK')} con MIB, "
                 f"{sum(1 for f in self.celdas.values() if f.get('sib1') == 'OK')} con SIB1.", "head")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.emitir({"t": "fin", "celdas": list(self.celdas.values())})


def main():
    ap = argparse.ArgumentParser(description="Analiza grabaciones IQ buscando celdas LTE "
                                 "(pensado para Starlink D2C con Doppler).",
                                 formatter_class=FORMATO_AYUDA)
    ap.add_argument("--carpeta", default=str(Path.home() / "capturas_iq"))
    ap.add_argument("--sesion", help="id de sesion (bN_FECHA); por defecto la mas reciente")
    ap.add_argument("--listar", action="store_true", help="solo listar sesiones")
    ap.add_argument("--modo", choices=["auto", "lista", "exhaustivo"], default="auto",
                    help="auto: portadoras visibles + --earfcn; lista: solo --earfcn; "
                         "exhaustivo: todos los EARFCN grabados")
    ap.add_argument("--earfcn", default="", help="EARFCN a revisar siempre, p.ej. 1175,1176")
    ap.add_argument("--doppler", type=float, default=15.0, help="barrido Doppler ± kHz")
    ap.add_argument("--paso-doppler", type=float, default=5.0, help="paso Doppler kHz")
    ap.add_argument("--integracion", type=float, default=100.0, help="integracion PSS (ms)")
    ap.add_argument("--psr", type=float, default=1.9, help="PSR integrado minimo para detectar")
    ap.add_argument("--ventanas-min", type=int, default=2,
                    help="ventanas minimas sobre el umbral (filtra ruido)")
    ap.add_argument("--psr-srsran", type=float, default=1.2, help="PSR minimo en srsRAN (MIB)")
    ap.add_argument("--intentos", type=int, default=4, help="momentos distintos a probar por deteccion")
    ap.add_argument("--ventana", type=float, default=3.0, help="segundos por intento de decodificacion")
    ap.add_argument("--tramas-mib", type=int, default=60, help="tramas maximas buscando MIB")
    ap.add_argument("--intentos-sib1", type=int, default=60)
    ap.add_argument("--umbral-espectro", type=float, default=6.0, help="dB sobre el piso (modo auto)")
    ap.add_argument("--eventos", action="store_true", help="salida JSON por linea (para la interfaz)")
    a = ap.parse_args()

    ses = listar_sesiones(a.carpeta)
    if a.listar or not ses:
        for s in ses:
            print(f"{s['id']}: banda {s['banda']}, {len(s['bloques'])} bloques, {s['formato']}, "
                  f"{s['bytes'] / 1024 ** 3:.1f} GiB")
        if not ses:
            sys.exit(f"No hay grabaciones en {a.carpeta}")
        return
    sesion = next((s for s in ses if s["id"] == a.sesion), None) if a.sesion else ses[0]
    if not sesion:
        sys.exit(f"No existe la sesion {a.sesion}")
    if not DECODER.exists():
        sys.exit(f"No existe {DECODER}. Ejecuta ./install.sh en la carpeta del proyecto")

    cfg = argparse.Namespace(
        modo=a.modo, earfcn=[int(x) for x in re.split(r"[,\s]+", a.earfcn) if x.strip()],
        doppler_khz=a.doppler, paso_doppler_khz=max(0.5, a.paso_doppler),
        integracion_ms=a.integracion, psr=a.psr, ventanas_min=a.ventanas_min, psr_srsran=a.psr_srsran, intentos=a.intentos,
        ventana_s=a.ventana, tramas_mib=a.tramas_mib, intentos_sib1=a.intentos_sib1,
        umbral_espectro_db=a.umbral_espectro)
    if cfg.modo == "lista" and not cfg.earfcn:
        sys.exit("El modo lista necesita --earfcn")

    lock = threading.Lock()

    def emitir(ev):
        with lock:
            if a.eventos:
                print(json.dumps(ev, ensure_ascii=False), flush=True)
            elif ev["t"] == "log":
                print(ev["txt"], flush=True)
            elif ev["t"] == "progreso":
                print(f"\r  {ev['fase']}: {ev['pct']:5.1f} %"
                      + (f"  ({ev['velocidad']:.2f}x tiempo real)" if ev.get("velocidad") else ""),
                      end="", file=sys.stderr, flush=True)
            elif ev["t"] == "fin":
                print("\n" + "=" * 100)
                print(f"{'EARFCN':>6} {'MHz':>7} {'PCI':>12} {'PRB':>4} {'Estado':>10} "
                      f"{'PLMN':>10} {'PSR':>5} {'Pres%':>6}  Operador")
                for f in sorted(ev["celdas"], key=lambda f: (f["earfcn"], str(f["pci"]))):
                    print(f"{f['earfcn']:>6} {f['freq_mhz']:>7} {str(f['pci']):>12} "
                          f"{str(f.get('prb', '-')):>4} {f.get('estado', ''):>10} "
                          f"{f.get('plmn', '-') or '-':>10} {f.get('psr_max', 0):>5} "
                          f"{f.get('presencia_pct', 0):>6}  {f.get('operador', '')}")

    an = Analizador(cfg, emitir)
    import signal

    def _stop(*_):
        an.parar = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        an.ejecutar(sesion)
    finally:
        shutil.rmtree(an.tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
