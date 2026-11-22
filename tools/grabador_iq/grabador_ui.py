#!/usr/bin/env python3
"""
Interfaz web local para grabar IQ LTE con el USRP X300 (SOLO RECEPCION).

Uso:
    python3 grabador_ui.py                # abre Brave en http://127.0.0.1:8770
    python3 grabador_ui.py --no-browser   # solo levanta el servidor

Escucha unicamente en 127.0.0.1. Usa grabar_lte.py (plan y grabacion) y
rx_iq_grabador (API C++ de UHD, solo rx_streamer: nunca transmite).

El espectro en vivo se calcula sobre las mismas muestras que se estan
escribiendo al .iq: el grabador deja cada ~200 ms las ultimas 16384 muestras
en /dev/shm y aqui se les hace la FFT.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grabar_lte as G  # noqa: E402

N_FFT = 1024
SNAPSHOT = Path("/dev/shm") / f"grabador_iq_snap_{os.getpid()}.bin"
DEF_SALIDA = str(Path.home() / "capturas_iq")


def info_bandas():
    out = []
    for b, (f_low, n_offs, n_min, n_max) in sorted(G.TABLA_EARFCN_DL.items()):
        out.append({"band": b, "f_low": f_low, "n_offs": n_offs, "n_min": n_min, "n_max": n_max,
                    "f_min": round(f_low + 0.1 * (n_min - n_offs), 1),
                    "f_max": round(f_low + 0.1 * (n_max - n_offs), 1)})
    return out


def leer_cfg(c):
    """Valida la configuracion que llega del navegador y la devuelve como objeto."""
    def num(k, tipo=float):
        try:
            return tipo(str(c[k]).strip())
        except (KeyError, ValueError):
            raise ValueError(f"Valor no valido en '{k}'")
    cfg = SimpleNamespace(
        banda=num("banda", int), earfcn_inicio=num("ini", int), earfcn_fin=num("fin", int),
        ancho=num("ancho"), solape=num("solape"), duracion=num("duracion"),
        ganancia=num("ganancia"), formato=str(c.get("formato", "auto")),
        salida=str(c.get("salida") or DEF_SALIDA), antena=str(c.get("antena") or "RX2"),
        subdev=str(c.get("subdev") or "A:0"), ref=str(c.get("ref") or "internal"),
        args=str(c.get("args") or "type=x300,addr=192.168.10.2"),
        uno_a_la_vez=bool(c.get("uno")))
    if cfg.formato not in ("auto", "sc16", "sc8"):
        raise ValueError("Formato no valido")
    if cfg.earfcn_fin < cfg.earfcn_inicio:
        cfg.earfcn_inicio, cfg.earfcn_fin = cfg.earfcn_fin, cfg.earfcn_inicio
    G.earfcn_a_mhz(cfg.banda, cfg.earfcn_inicio)
    G.earfcn_a_mhz(cfg.banda, cfg.earfcn_fin)
    if cfg.solape < 0 or cfg.solape >= cfg.ancho:
        raise ValueError("El solape debe ser >= 0 y menor que el ancho del bloque")
    if cfg.ancho > G.TASA_SPS / 1e6:
        raise ValueError(f"El ancho no puede superar {G.TASA_SPS/1e6:.0f} MHz (tasa de 20 Msps)")
    if not 0 < cfg.duracion <= 86400:
        raise ValueError("Duracion fuera de rango")
    if not 0 <= cfg.ganancia <= 49:
        raise ValueError("Ganancia fuera de rango (0-49 dB)")
    for campo in ("antena", "subdev", "ref", "args"):
        v = getattr(cfg, campo)
        if any(ch in v for ch in "\n\r\0"):
            raise ValueError(f"Caracter no valido en {campo}")
    if "master_clock_rate" not in cfg.args:
        cfg.args += f",master_clock_rate={G.MCR_HZ:.0f}"
    return cfg


def calcular_plan(cfg):
    bloques = G.planificar(cfg.banda, cfg.earfcn_inicio, cfg.earfcn_fin, cfg.ancho, cfg.solape)
    for b in bloques:
        if not G.FREQ_MIN_MHZ <= b["fc_mhz"] <= G.FREQ_MAX_MHZ:
            raise ValueError(f"Frecuencia {b['fc_mhz']} MHz fuera del rango del X300")
    salida = Path(cfg.salida).expanduser().resolve()
    salida.mkdir(parents=True, exist_ok=True)
    previas = G.grabaciones_previas(salida)
    bytes_previas = sum(d["bytes"] for d in previas)
    libre = shutil.disk_usage(salida).free + bytes_previas  # se borran antes de grabar
    n = int(round(G.TASA_SPS * cfg.duracion))
    b16 = n * G.BYTES_POR_MUESTRA["sc16"]
    formato, motivo = G.elegir_formato(cfg.formato, b16 * len(bloques), libre,
                                       cfg.uno_a_la_vez, b16)
    bb = n * G.BYTES_POR_MUESTRA[formato]
    total = bb * len(bloques)
    necesario = bb if cfg.uno_a_la_vez else total
    margen = necesario * G.RESERVA_FRAC + G.RESERVA_BYTES
    avisos = []
    cabe = necesario + margen <= libre
    if not cabe:
        avisos.append(f"Espacio insuficiente: hacen falta {G.fmt_bytes(necesario + margen)} "
                      f"(con reserva) y hay {G.fmt_bytes(libre)}. No se puede empezar.")
    elif cfg.uno_a_la_vez and total + margen > libre:
        avisos.append("El total no cabe: tendrás que procesar y borrar entre bloques.")
    if previas:
        avisos.insert(0, "Al grabar se ELIMINARÁN las grabaciones anteriores de esta carpeta: "
                      + G.resumen_previas(previas) + ".")
    return {
        "bloques": bloques, "formato": formato, "motivo": motivo,
        "bytes_bloque": bb, "bytes_total": total, "bytes_total_sc16": b16 * len(bloques),
        "txt_bloque": G.fmt_bytes(bb), "txt_total": G.fmt_bytes(total),
        "txt_total_sc16": G.fmt_bytes(b16 * len(bloques)),
        "libre": libre, "txt_libre": G.fmt_bytes(libre), "salida": str(salida),
        "cabe": cabe, "avisos": avisos, "minutos": len(bloques) * cfg.duracion / 60,
        "previas": [{"id": d["id"], "banda": d["banda"], "n": len(d["archivos"]),
                     "txt": G.fmt_bytes(d["bytes"])} for d in previas],
        "txt_previas": G.fmt_bytes(bytes_previas),
        "f_ini": G.earfcn_a_mhz(cfg.banda, cfg.earfcn_inicio),
        "f_fin": G.earfcn_a_mhz(cfg.banda, cfg.earfcn_fin),
    }


class Sesion:
    """Una grabacion (o monitor) a la vez, en un hilo aparte."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hilo = None
        self.proc = None
        self.parar = False
        self.continuar = threading.Event()
        self.log = []          # (n, texto, clase)
        self.n_log = 0
        self.linea = ""
        self.estado = {"modo": "inactivo"}
        self.plan = None
        self.bloques = []      # estado por bloque
        self.formato = "sc16"
        self.fc_mhz = None

    # --- registro -----------------------------------------------------------
    def _log(self, txt, clase=""):
        with self.lock:
            self.n_log += 1
            self.log.append((self.n_log, txt, clase))
            if len(self.log) > 3000:
                del self.log[:1000]

    def _stderr(self, ch):
        if ch in "\r\n":
            if self.linea.strip():
                self._log(self.linea.rstrip())
            self.linea = ""
        else:
            self.linea += ch

    def ocupado(self):
        return self.hilo is not None and self.hilo.is_alive()

    # --- acciones -----------------------------------------------------------
    def iniciar(self, cfg):
        if self.ocupado():
            raise ValueError("Ya hay una grabación o monitor en marcha")
        if not G.GRABADOR.exists():
            raise ValueError(f"No existe {G.GRABADOR}. Ejecuta compilar.sh")
        plan = calcular_plan(cfg)
        if not plan["cabe"]:
            raise ValueError(plan["avisos"][0])
        self.plan, self.formato, self.parar = plan, plan["formato"], False
        previas = G.grabaciones_previas(plan["salida"])
        if previas:
            n = G.borrar_previas(previas)
            self._log(f"Borradas las grabaciones anteriores ({n} archivos): "
                      f"{G.resumen_previas(previas)}", "warn")
        self.bloques = [dict(b, estado="pendiente") for b in plan["bloques"]]
        self.hilo = threading.Thread(target=self._grabar, args=(cfg, plan), daemon=True)
        self.hilo.start()
        return {"ok": True, "plan": plan}

    def monitor(self, cfg, fc_mhz):
        if self.ocupado():
            raise ValueError("Ya hay una grabación o monitor en marcha")
        if not G.GRABADOR.exists():
            raise ValueError(f"No existe {G.GRABADOR}. Ejecuta compilar.sh")
        if not G.FREQ_MIN_MHZ <= fc_mhz <= G.FREQ_MAX_MHZ:
            raise ValueError("Frecuencia fuera del rango del X300")
        self.formato = cfg.formato if cfg.formato != "auto" else "sc16"
        self.parar = False
        self.hilo = threading.Thread(target=self._monitor, args=(cfg, fc_mhz), daemon=True)
        self.hilo.start()
        return {"ok": True}

    def detener(self):
        self.parar = True
        self.continuar.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
        return self.ocupado()

    def siguiente(self):
        self.continuar.set()

    # --- hilos --------------------------------------------------------------
    def _set(self, **kw):
        with self.lock:
            self.estado.update(kw)

    def _set_proc(self, p):
        self.proc = p

    def _monitor(self, cfg, fc):
        self.estado = {"modo": "monitor", "fc_mhz": fc, "inicio": time.time(), "O": 0, "D": 0}
        self.fc_mhz = fc
        self._log(f"Monitor (sin grabar) en {fc:.3f} MHz, ganancia {cfg.ganancia} dB, "
                  f"{self.formato}", "head")
        b = {"indice": 0, "fc_mhz": fc, "f_lo_mhz": fc - 10, "f_hi_mhz": fc + 10,
             "earfcn_ini": None, "earfcn_fin": None, "n_earfcn": 0}
        salida = Path(cfg.salida).expanduser()
        salida.mkdir(parents=True, exist_ok=True)
        try:
            r = G.grabar_bloque(cfg, b, self.formato, salida, "monitor",
                                on_stderr=self._stderr, snapshot=SNAPSHOT, on_proc=self._set_proc,
                                on_progreso=lambda rec, tot, o, d: self._set(O=o, D=d),
                                archivo="/dev/null", duracion=86400)
            if r["stats"] is None:
                self._log(f"El grabador terminó con error (código {r['codigo']}). "
                          f"Mira {r['log']}", "err")
        except Exception as e:  # noqa
            self._log(f"Error: {e}", "err")
        self._stderr("\n")
        self._log("Monitor detenido.", "head")
        self._set(modo="inactivo")
        self.proc = None

    def _grabar(self, cfg, plan):
        import datetime as dt
        salida = Path(plan["salida"])
        sello = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        n = len(self.bloques)
        self.estado = {"modo": "grabando", "n": n, "i": 0, "inicio": time.time(), "O": 0, "D": 0,
                       "pct": 0.0}
        self._log(f"Inicio: banda {cfg.banda}, {n} bloque(s), {plan['formato']}, "
                  f"{cfg.duracion:.0f} s/bloque → {salida}", "head")
        for k, b in enumerate(self.bloques):
            if self.parar:
                break
            libre = shutil.disk_usage(salida).free
            if plan["bytes_bloque"] + G.RESERVA_BYTES > libre:
                self._log(f"Espacio insuficiente para el bloque {b['indice']} "
                          f"({G.fmt_bytes(libre)} libres). Se detiene.", "err")
                b["estado"] = "sin espacio"
                break
            self.fc_mhz = b["fc_mhz"]
            b["estado"] = "grabando"
            self._set(i=k + 1, fc_mhz=b["fc_mhz"], pct=0.0, O=0, D=0, t_bloque=time.time())
            self._log(f"Bloque {b['indice']}/{n}: Fc {b['fc_mhz']:.3f} MHz, EARFCN "
                      f"{b['earfcn_ini']}–{b['earfcn_fin']}", "head")

            def prog(rec, tot, o, d):
                self._set(pct=100.0 * rec / max(tot, 1), O=o, D=d)
            try:
                r = G.grabar_bloque(cfg, b, plan["formato"], salida, sello,
                                    on_stderr=self._stderr, on_progreso=prog, snapshot=SNAPSHOT,
                                    on_proc=self._set_proc)
            except Exception as e:  # noqa
                self._log(f"Error: {e}", "err")
                b["estado"] = "error"
                break
            self._stderr("\n")
            st = r["stats"]
            if st is None:
                b["estado"] = "error"
                self._log(f"El grabador terminó con error (código {r['codigo']}). "
                          f"Mira {r['log']}", "err")
                break
            b.update(O=st["overflows_O"], D=st["paquetes_perdidos_D"], archivo=r["iq"].name,
                     tam=G.fmt_bytes(r["iq"].stat().st_size) if r["iq"].exists() else "-",
                     hora=st["hora_inicio_utc"])
            if st["interrumpido"]:
                b["estado"] = "interrumpido"
                self._log(f"Bloque {b['indice']} interrumpido; se guardó lo grabado.", "warn")
                break
            if b["O"] or b["D"]:
                b["estado"] = "con overflows"
                self._log(f"¡AVISO! Bloque {b['indice']}: {b['O']} overflows (O) y {b['D']} "
                          f"paquetes perdidos (D). La captura tiene huecos.", "err")
            else:
                b["estado"] = "ok"
                self._log(f"Bloque {b['indice']} OK, sin overflows: {r['iq'].name} "
                          f"({b['tam']})", "ok")
            if cfg.uno_a_la_vez and k < n - 1 and not self.parar:
                self.continuar.clear()
                self._set(modo="esperando")
                self._log("Bloque terminado. Procesa o borra lo que necesites y pulsa "
                          "«Siguiente bloque».", "warn")
                self.continuar.wait()
                self._set(modo="grabando")
        for b in self.bloques:
            if b["estado"] == "pendiente" and self.parar:
                b["estado"] = "cancelado"
        self._log("Grabación terminada.", "head")
        self._set(modo="inactivo")
        self.proc = None

    # --- consulta -----------------------------------------------------------
    def poll(self, since):
        with self.lock:
            logs = [x for x in self.log if x[0] > since]
            est = dict(self.estado)
        try:
            salida = self.plan["salida"] if self.plan else DEF_SALIDA
            est["txt_libre"] = G.fmt_bytes(shutil.disk_usage(salida).free)
        except OSError:
            pass
        return {"estado": est, "log": logs, "bloques": self.bloques}

    def espectro(self):
        if self.estado.get("modo") == "inactivo" or not SNAPSHOT.exists():
            return {"ok": False}
        try:
            edad = time.time() - SNAPSHOT.stat().st_mtime
            raw = np.fromfile(SNAPSHOT, dtype=G_DTYPE[self.formato])
        except OSError:
            return {"ok": False}
        if raw.size < 2 * N_FFT:
            return {"ok": False}
        esc = 32768.0 if self.formato == "sc16" else 128.0
        iq = raw.astype(np.float32)
        x = (iq[0::2] + 1j * iq[1::2]) / esc
        m = (len(x) // N_FFT) * N_FFT
        seg = x[:m].reshape(-1, N_FFT)
        w = np.hanning(N_FFT).astype(np.float32)
        P = np.abs(np.fft.fft(seg * w, axis=1)) ** 2 / (w.sum() ** 2)
        db = 10 * np.log10(np.fft.fftshift(P.mean(axis=0)) + 1e-15)
        pico = float(np.max(np.abs(iq))) / esc
        lim = 32767 if self.formato == "sc16" else 127
        sat = float(np.mean(np.abs(iq) >= lim - 1)) * 100
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2)))
        return {"ok": True, "db": [round(float(v), 1) for v in db], "fc_mhz": self.fc_mhz,
                "fs": G.TASA_SPS, "pico_dbfs": round(20 * np.log10(pico + 1e-12), 1),
                "rms_dbfs": round(20 * np.log10(rms + 1e-12), 1), "sat_pct": round(sat, 3),
                "formato": self.formato, "edad": round(edad, 2)}


G_DTYPE = {"sc16": np.int16, "sc8": np.int8}
SES = Sesion()


class Handler(BaseHTTPRequestHandler):
    server_version = "GrabadorIQ/1.0"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host no permitido"})
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/info":
            return self._send(200, {"bandas": info_bandas(), "salida": DEF_SALIDA,
                                    "grabador": str(G.GRABADOR),
                                    "grabador_ok": G.GRABADOR.exists(),
                                    "tasa": G.TASA_SPS, "mcr": G.MCR_HZ})
        if u.path == "/api/poll":
            from urllib.parse import parse_qs
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            return self._send(200, SES.poll(since))
        if u.path == "/api/espectro":
            return self._send(200, SES.espectro())
        return self._send(404, {"error": "no encontrado"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "host no permitido"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "se requiere JSON"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "JSON invalido"})
        path = urlparse(self.path).path
        try:
            if path == "/api/plan":
                return self._send(200, {"ok": True, "plan": calcular_plan(leer_cfg(body))})
            if path == "/api/iniciar":
                return self._send(200, SES.iniciar(leer_cfg(body)))
            if path == "/api/monitor":
                return self._send(200, SES.monitor(leer_cfg(body), float(body.get("fc_mhz"))))
            if path == "/api/detener":
                return self._send(200, {"ok": True, "ocupado": SES.detener()})
            if path == "/api/siguiente":
                SES.siguiente()
                return self._send(200, {"ok": True})
        except (ValueError, TypeError, KeyError, OSError) as e:
            return self._send(200, {"ok": False, "error": str(e)})
        return self._send(404, {"error": "no encontrado"})


def abrir_navegador(url):
    for name in ("brave-browser", "brave", "brave-browser-stable"):
        exe = shutil.which(name)
        if exe:
            subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return
    webbrowser.open(url)


def main():
    ap = argparse.ArgumentParser(description="Interfaz web para grabar IQ LTE (solo RX)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"No hay puertos libres cerca de {a.port}")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Interfaz lista en {url}   (Ctrl+C para cerrar)")
    if not a.no_browser:
        threading.Timer(0.6, abrir_navegador, (url,)).start()
    import signal

    def _salir(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _salir)
    signal.signal(signal.SIGHUP, _salir)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SES.detener()
        if SES.hilo:
            SES.hilo.join(timeout=5)
        for p in (SNAPSHOT, Path(str(SNAPSHOT) + ".tmp")):
            try:
                p.unlink()
            except OSError:
                pass
        srv.server_close()


HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grabador IQ LTE</title>
<style>
:root{--bg:#030604;--panel:#09110c;--panel2:#0f1a13;--line:#1b2d21;--text:#e2f2e6;--muted:#7d9a85;
--accent:#22d37a;--ok:#8be9a8;--warn:#f5b94a;--err:#ff6b6b;--sat:#c792ff;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
header .sub{color:var(--muted);font-size:12px}
.pill{margin-left:auto;display:flex;align-items:center;gap:8px;padding:4px 12px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.pill.rec .dot{background:var(--err);animation:pul 1s infinite}.pill.mon .dot{background:var(--accent)}.pill.wait .dot{background:var(--warn)}
@keyframes pul{50%{opacity:.3}}
main{display:grid;grid-template-columns:350px 1fr;gap:12px;padding:12px;height:calc(100% - 50px)}
aside{overflow:auto;min-height:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);font-weight:650;display:flex;align-items:center;gap:8px}
label{display:block;font-size:12.5px;color:var(--muted);margin:9px 0 3px}
.hint{font-size:11.8px;color:var(--muted);margin:3px 0 0}
.hint b{color:var(--text);font-weight:600}
input,select{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 9px;font:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.chk{display:flex;align-items:center;gap:8px;margin:10px 0 0;color:var(--text);font-size:13px}
.chk input{width:auto}
button{font:inherit;cursor:pointer;border-radius:7px;border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:7px 12px}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
.btnrow{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.btn-go{background:var(--accent);border-color:var(--accent);color:#02130a;font-weight:650;flex:1}
.btn-stop{background:transparent;border-color:var(--err);color:var(--err)}
.btn-next{background:var(--warn);border-color:var(--warn);color:#1d1404;font-weight:650;flex:1}
.mini{font-size:12px;padding:3px 9px}
summary{cursor:pointer;color:var(--muted);font-size:12.5px;margin-top:10px}
.err{color:var(--err);font-size:12.5px;margin-top:6px;min-height:1em}
.fmt{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.fmt label{margin:0;display:block;border:1px solid var(--line);border-radius:7px;padding:6px 8px;cursor:pointer;color:var(--text);font-size:13px;text-align:center}
.fmt input{display:none}
.fmt label:has(input:checked){border-color:var(--accent);background:rgba(34,211,122,.12)}
.fmt small{display:block;color:var(--muted);font-size:11px}
.col{display:flex;flex-direction:column;gap:12px;min-height:0;min-width:0;overflow-y:auto;padding-right:4px}
.col>*{flex:none}
.status{display:grid;grid-template-columns:1fr auto;gap:6px 14px;align-items:center}
.now{font-size:15px;font-weight:600}
.now small{color:var(--muted);font-weight:400}
.bar{grid-column:1/-1;height:7px;background:var(--panel2);border-radius:99px;overflow:hidden}
.bar div{height:100%;width:0;background:var(--accent);transition:width .4s}
.stats{display:flex;gap:18px;color:var(--muted);font-size:12.5px;grid-column:1/-1;flex-wrap:wrap}
.stats b{color:var(--text);font-variant-numeric:tabular-nums}
.stats b.bad{color:var(--err)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.panel .bar2{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.panel .bar2 h2{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);font-weight:650}
.panel .bar2 input{width:70px;padding:3px 6px;font-size:12px}
.panel .bar2 .chk{margin:0;font-size:12px}
.sp{flex:1}
.meter{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.meter .m{width:120px;height:8px;background:var(--panel2);border-radius:99px;overflow:hidden;position:relative}
.meter .m div{height:100%;width:0;background:var(--ok)}
.meter b{color:var(--text);font-variant-numeric:tabular-nums}
.specwrap{position:relative;display:flex;flex-direction:column}
canvas{display:block;width:100%}
#spec{height:320px}
#wf{height:220px;image-rendering:pixelated}
#tip{position:absolute;pointer-events:none;background:rgba(14,18,23,.92);border:1px solid var(--line);border-radius:6px;padding:3px 7px;font:12px var(--mono);display:none;white-space:nowrap}
.specempty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);text-align:center;padding:20px;pointer-events:none}
table{border-collapse:collapse;width:100%;font-size:12.8px}
th,td{padding:5px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
td{font-variant-numeric:tabular-nums}
tr.act td{background:rgba(34,211,122,.08)}
.tbl{overflow:auto;max-height:420px}
.badge{display:inline-block;padding:0 7px;border-radius:99px;font-size:11.5px;font-weight:600;background:var(--panel2);color:var(--muted)}
.b-ok{background:rgba(139,233,168,.15);color:var(--ok)}.b-rec{background:rgba(255,107,107,.15);color:var(--err)}
.b-warn{background:rgba(245,185,74,.15);color:var(--warn)}.b-err{background:rgba(255,107,107,.25);color:var(--err)}
.log{overflow:auto;font:12.3px/1.5 var(--mono);padding:8px 12px;height:300px;resize:vertical}
.log div{white-space:pre-wrap;word-break:break-all}
.log .head{color:var(--accent);font-weight:600}.log .ok{color:var(--ok)}.log .warn{color:var(--warn)}.log .err{color:var(--err);font-weight:600}
.alert{background:rgba(255,107,107,.12);border:1px solid var(--err);color:var(--err);border-radius:8px;padding:8px 12px;font-weight:600;display:none}
.planbox{font-size:12.5px}
.planbox .kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
.planbox .kv span:nth-child(odd){color:var(--muted)}
.warnbox{color:var(--warn);font-size:12.5px;margin-top:6px}
.badbox{color:var(--err);font-size:12.5px;margin-top:6px;font-weight:600}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;max-width:780px;width:92%;padding:18px 22px}
dialog::backdrop{background:rgba(0,0,0,.6)}
dialog h3{margin:0 0 8px}
dialog h4{margin:16px 0 6px;color:var(--accent)}
dialog table td,dialog table th{white-space:normal}
dialog p,dialog li{font-size:13.5px;color:#cfd6df}
@media(max-width:980px){main{grid-template-columns:1fr;height:auto}aside,.col{overflow:visible}}

/* desplazamiento con rueda/panel tactil sin mostrar barras */
*{scrollbar-width:none}
*::-webkit-scrollbar{display:none;width:0;height:0}
.aviso{display:none;margin-top:4px;padding:4px 8px;border-radius:6px;font-size:12px;line-height:1.35;
  background:rgba(245,185,74,.12);border:1px solid rgba(245,185,74,.55);color:var(--warn)}
input.bad{border-color:var(--warn)}
</style>
</head>
<body>
<header>
  <h1>Grabador IQ LTE</h1><span class="sub">USRP X300 · solo recepción · 20 Msps</span>
  <div class="pill" id="pill"><span class="dot"></span><span id="pilltxt">Inactivo</span></div>
</header>
<main>
<aside>
  <div class="card">
    <h2>Rango a grabar</h2>
    <label for="banda">Banda LTE</label>
    <select id="banda"></select>
    <div class="hint" id="bandhint"></div>
    <div class="row2">
      <div><label for="ini">EARFCN inicio</label><input id="ini" type="number" step="1"><div class="hint" id="fini"></div></div>
      <div><label for="fin">EARFCN fin</label><input id="fin" type="number" step="1"><div class="hint" id="ffin"></div></div>
    </div>
    <button class="mini" id="todabanda" type="button" style="margin-top:8px">Toda la banda</button>
    <div class="row2">
      <div><label for="ancho">Ancho bloque (MHz)</label><input id="ancho" type="number" step="0.5" min="1" max="20"></div>
      <div><label for="solape">Solape (MHz)</label><input id="solape" type="number" step="0.5" min="0"></div>
    </div>
    <div class="hint"><b>Bloques:</b> la radio graba 20 MHz de golpe. Si el rango es más ancho se divide en bloques que se solapan para no perder lo que cae en los bordes (allí el filtro atenúa).</div>
    <div class="row2">
      <div><label for="duracion">Duración por bloque (s)</label><input id="duracion" type="number" step="1" min="1"></div>
      <div><label for="ganancia">Ganancia RX (dB)</label><input id="ganancia" type="number" step="1" min="0" max="49"></div>
    </div>
    <div class="hint"><b>Ganancia:</b> ajústala con el <b>Monitor</b> mirando el medidor de nivel: el pico debe quedar entre −20 y −6 dBFS, sin saturación.</div>
  </div>

  <div class="card">
    <h2>Formato <button class="mini" id="manual" type="button" style="margin-left:auto;text-transform:none;letter-spacing:0">¿sc16 o sc8? · manual</button></h2>
    <div class="fmt">
      <label><input type="radio" name="formato" value="auto">Auto<small>sc16 si cabe</small></label>
      <label><input type="radio" name="formato" value="sc16">sc16<small>4 B/muestra</small></label>
      <label><input type="radio" name="formato" value="sc8">sc8<small>2 B/muestra</small></label>
    </div>
    <div class="hint" id="fmthint"></div>
  </div>

  <div class="card">
    <h2>Radio y salida</h2>
    <div class="row2">
      <div><label for="antena">Antena</label><select id="antena"><option>RX2</option><option>TX/RX</option></select></div>
      <div><label for="subdev">Tarjeta RX</label><select id="subdev"><option value="A:0">A (A:0)</option><option value="B:0">B (B:0)</option></select></div>
    </div>
    <label for="salida">Carpeta de salida</label>
    <input id="salida" type="text">
    <label class="chk"><input id="uno" type="checkbox"> Uno a la vez (esperar entre bloques)</label>
    <div class="hint">Graba un bloque y se detiene hasta que pulses «Siguiente bloque», para que puedas procesar y borrar entre medio.</div>
    <details>
      <summary>Avanzado</summary>
      <label for="args">Dispositivo UHD</label>
      <input id="args" type="text">
      <label for="ref">Referencia de reloj</label>
      <select id="ref"><option value="internal">Interna</option><option value="external">Externa (10 MHz)</option><option value="gpsdo">GPSDO</option></select>
      <div class="hint" id="grabinfo"></div>
    </details>
  </div>

  <div class="card">
    <h2>Plan</h2>
    <div class="planbox" id="planbox"><span class="hint">Configura el rango y pulsa «Ver plan».</span></div>
    <div class="err" id="err"></div>
    <div class="btnrow">
      <button id="btnplan" type="button">Ver plan</button>
      <button class="btn-go" id="go" type="button">● Grabar</button>
    </div>
    <div class="btnrow">
      <button class="btn-next" id="next" type="button" style="display:none">▶ Siguiente bloque</button>
      <button class="btn-stop" id="stop" type="button" disabled>■ Detener</button>
    </div>
    <label for="monfc">Monitor sin grabar (MHz)</label>
    <div style="display:flex;gap:8px"><input id="monfc" type="number" step="0.1"><button id="mon" type="button">Monitor</button></div>
    <div class="hint">Muestra el espectro en vivo sin guardar nada en disco. Útil para ajustar la ganancia antes de grabar.</div>
  </div>
</aside>

<section class="col">
  <div class="card" style="margin:0">
    <div class="status">
      <div class="now" id="now">Sin grabación en curso</div>
      <div class="hint" id="clock">00:00</div>
      <div class="bar"><div id="barfill"></div></div>
      <div class="stats">
        <span>Bloque: <b id="s_blq">–</b></span>
        <span>Progreso: <b id="s_pct">–</b></span>
        <span>Overflows (O): <b id="s_o">0</b></span>
        <span>Perdidos (D): <b id="s_d">0</b></span>
        <span>Libre en disco: <b id="s_libre">–</b></span>
      </div>
    </div>
  </div>
  <div class="alert" id="alerta"></div>

  <div class="panel">
    <div class="bar2"><h2>Espectro en vivo</h2>
      <span class="hint" id="specinfo"></span><span class="sp"></span>
      <div class="meter" title="Nivel de la señal digitalizada respecto al máximo del formato (0 dBFS = saturación)">Pico <div class="m"><div id="mfill"></div></div><b id="mpico">–</b> <span id="msat"></span></div>
      <label class="chk"><input type="checkbox" id="maxhold"> máx.</label>
      <label class="chk">mín <input id="dbmin" type="number" value="-110" step="5"></label>
      <label class="chk">máx <input id="dbmax" type="number" value="-20" step="5"></label>
      <button class="mini" id="autoesc" type="button">Auto</button>
    </div>
    <div class="specwrap" id="specwrap">
      <canvas id="spec"></canvas>
      <canvas id="wf"></canvas>
      <div id="tip"></div>
      <div class="specempty" id="specempty">El espectro aparece aquí mientras se graba o en modo Monitor.<br>Es la FFT de las mismas muestras que se están guardando en el archivo.</div>
    </div>
  </div>

  <div class="panel">
    <div class="bar2"><h2>Bloques</h2><span class="sp"></span><span class="hint" id="blqhint"></span></div>
    <div class="tbl"><table><thead><tr><th>#</th><th>Fc (MHz)</th><th>Cubre (MHz)</th><th>EARFCN</th><th>Estado</th><th>O / D</th><th>Archivo</th></tr></thead><tbody id="tbody"><tr><td colspan="7" class="hint">Sin plan todavía.</td></tr></tbody></table></div>
  </div>

  <div class="panel">
    <div class="bar2"><h2>Registro</h2><span class="sp"></span>
      <label class="chk"><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
      <button class="mini" id="clrlog" type="button">Limpiar</button></div>
    <div class="log" id="log"></div>
  </div>
</section>
</main>

<dialog id="dlg">
  <h3>sc16 y sc8: qué son y cuál elegir</h3>
  <p>Cada muestra IQ son dos números (I y Q). El formato dice con cuántos bits se guarda cada uno, tanto en el cable de red como en el archivo.</p>
  <table>
    <tr><th></th><th>sc16</th><th>sc8</th></tr>
    <tr><td>Bits por I y por Q</td><td>16 (−32768 … 32767)</td><td>8 (−128 … 127)</td></tr>
    <tr><td>Bytes por muestra</td><td>4</td><td>2</td></tr>
    <tr><td>Bloque de 180 s a 20 Msps</td><td>13,4 GiB</td><td>6,7 GiB</td></tr>
    <tr><td>Tráfico por la red 1 GbE</td><td>~640 Mbit/s (cerca del límite)</td><td>~320 Mbit/s</td></tr>
    <tr><td>Rango dinámico</td><td>~84 dB útiles (ADC de 14 bits)</td><td>~48 dB</td></tr>
  </table>
  <h4>sc16: calidad máxima</h4>
  <p>Guarda prácticamente todo lo que entrega el conversor del X300. Celdas fuertes y débiles conviven en el mismo bloque sin problema y la ganancia no tiene que estar tan afinada. Es la opción por defecto cuando hay espacio.</p>
  <h4>sc8: la mitad de espacio y de tráfico</h4>
  <p>Úsalo si te falta disco o si en la prueba aparecen <b>paquetes perdidos (D)</b> por la red. Con 8 bits la <b>ganancia es crítica</b>:</p>
  <ul>
    <li>Demasiado alta → la señal satura (el medidor marca «SAT»). La captura queda distorsionada.</li>
    <li>Demasiado baja → las celdas débiles quedan en pocos niveles y se pierden en el ruido de cuantización.</li>
    <li>Ajusta con el <b>Monitor</b> hasta que el pico quede entre −12 y −6 dBFS.</li>
  </ul>
  <h4>Auto</h4>
  <p>Elige sc16 si el plan completo cabe en el disco con una reserva de seguridad, y sc8 si no. En modo «uno a la vez» basta con que quepa un bloque.</p>
  <h4>Para srsRAN</h4>
  <p>Los dos se convierten igual a cf32 con <code>iq_a_cf32.py</code>; el formato se lee del .json del bloque.</p>
  <div class="btnrow"><button onclick="document.getElementById('dlg').close()">Cerrar</button></div>
</dialog>

<script>
const ID_INI='ini', ID_FIN='fin', ID_GAN='ganancia';
const BANDA_ACTUAL = () => INFO && bandOf(document.getElementById('banda').value);

// ---------- avisos junto al campo y limites de rango ----------
function avisoEn(id, msg){
  const inp = document.getElementById(id);
  let el = document.getElementById('av_' + id);
  if(!el){ el = document.createElement('div'); el.id = 'av_' + id; el.className = 'aviso'; inp.insertAdjacentElement('afterend', el); }
  el.textContent = msg || ''; el.style.display = msg ? 'block' : 'none';
  inp.classList.toggle('bad', !!msg);
}
// vivo=true mientras escribe: solo avisa. vivo=false al salir del campo: corrige el valor.
function limitarCampo(id, min, max, desc, entero, vivo){
  const el = document.getElementById(id), raw = el.value.trim();
  if(raw === ''){ avisoEn(id, vivo ? '' : `Escribe un valor entre ${min} y ${max}.`); return; }
  let v = Number(raw);
  if(!Number.isFinite(v)){ if(!vivo){ el.value = min; } avisoEn(id, `Valor no válido. ${desc}: ${min}–${max}.`); return; }
  if(entero) v = Math.round(v);
  if(v < min || v > max){
    const lim = v < min ? min : max, tipo = v < min ? 'mínimo' : 'máximo';
    if(vivo){ avisoEn(id, `${raw} está fuera de rango (${desc}: ${min}–${max}). Al salir del campo se ajusta al ${tipo}, ${lim}.`); }
    else { el.value = lim; avisoEn(id, `${raw} estaba fuera de rango (${desc}: ${min}–${max}). Se ajustó al ${tipo}: ${lim}.`); }
    return;
  }
  if(!vivo && String(v) !== raw) el.value = v;
  avisoEn(id, '');
}
// solo: id del campo que se está editando (los avisos de los otros campos se mantienen)
function revisarRangos(vivo, solo){
  const b = BANDA_ACTUAL(), toca = id => !solo || solo === id;
  if(b){
    const d = `banda ${b.band}, EARFCN`;
    if(toca(ID_INI)) limitarCampo(ID_INI, b.n_min, b.n_max, d, true, vivo);
    if(toca(ID_FIN)) limitarCampo(ID_FIN, b.n_min, b.n_max, d, true, vivo);
    const s = parseInt(document.getElementById(ID_INI).value), e = parseInt(document.getElementById(ID_FIN).value);
    const avf = document.getElementById('av_' + ID_FIN);
    if((toca(ID_INI) || toca(ID_FIN)) && !isNaN(s) && !isNaN(e) && s > e && !(avf && avf.style.display === 'block'))
      avisoEn(ID_FIN, `El EARFCN final (${e}) es menor que el inicial (${s}).`);
  }
  if(toca(ID_GAN)) limitarCampo(ID_GAN, 0, 49, 'ganancia en dB', false, vivo);
}
function activarRangos(){
  for(const id of [ID_INI, ID_FIN, ID_GAN]){
    const el = document.getElementById(id);
    el.addEventListener('input', () => revisarRangos(true, id));
    el.addEventListener('change', () => { revisarRangos(false, id); el.dispatchEvent(new Event('rangochange')); });
  }
  document.getElementById(ID_GAN).min = 0; document.getElementById(ID_GAN).max = 49;
}
const $ = id => document.getElementById(id);
const LS = 'grabiq.v1';
let INFO=null, since=0, estado={}, plan=null, bloques=[], maxh=null, lastSpec=null;
const FIELDS=['banda','ini','fin','ancho','solape','duracion','ganancia','antena','subdev','salida','args','ref','monfc'];
const DEF={banda:'2',ini:'600',fin:'1199',ancho:'20',solape:'5',duracion:'180',ganancia:'30',antena:'RX2',
  subdev:'A:0',salida:'',args:'type=x300,addr=192.168.10.2',ref:'internal',monfc:'',formato:'auto',uno:true};

async function api(path, body){
  const opt = body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  const r = await fetch(path,opt); return r.json();
}
const bandOf = b => INFO.bandas.find(x => String(x.band)===String(b));
const e2f = (b,n) => { const x=bandOf(b); return x ? x.f_low + 0.1*(n-x.n_offs) : NaN; };
const f2e = (b,f) => { const x=bandOf(b); return x ? Math.round((f-x.f_low)/0.1 + x.n_offs) : NaN; };

function getCfg(){
  const c={}; FIELDS.forEach(f=>c[f]=$(f).value.trim());
  c.formato=document.querySelector('input[name=formato]:checked')?.value||'auto';
  c.uno=$('uno').checked; return c;
}
function saveCfg(){ try{localStorage.setItem(LS,JSON.stringify(getCfg()));}catch(e){} }
function loadCfg(){
  let c={}; try{c=JSON.parse(localStorage.getItem(LS)||'{}');}catch(e){}
  c=Object.assign({},DEF,c); if(!c.salida) c.salida=INFO.salida;
  FIELDS.forEach(f=>$(f).value=c[f]??'');
  document.querySelectorAll('input[name=formato]').forEach(r=>r.checked=(r.value===c.formato));
  $('uno').checked=!!c.uno;
}
function hints(){
  const b=bandOf($('banda').value);
  $('bandhint').textContent = b ? `DL ${b.f_min}–${b.f_max} MHz · EARFCN ${b.n_min}–${b.n_max}` : '';
  for(const [id,h] of [['ini','fini'],['fin','ffin']]){
    const n=parseInt($(id).value); const f=e2f($('banda').value,n);
    $(h).textContent = (b && n>=b.n_min && n<=b.n_max) ? f.toFixed(1)+' MHz' : '';
  }
  const f=document.querySelector('input[name=formato]:checked')?.value;
  $('fmthint').innerHTML = f==='sc8' ? '<b>sc8:</b> mitad de espacio. Ajusta bien la ganancia con el Monitor (pico −12…−6 dBFS).'
    : f==='sc16' ? '<b>sc16:</b> máxima calidad, 13,4 GiB por bloque de 180 s.'
    : '<b>Auto:</b> sc16 si el plan cabe en el disco; si no, sc8.';
}
function fmtT(s){ s=Math.max(0,Math.floor(s)); const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return (h?h+':':'')+String(m).padStart(2,'0')+':'+String(x).padStart(2,'0'); }
const esc = s => String(s??'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function showPlan(p){
  plan=p;
  let h=`<div class="kv">
    <span>Rango</span><span>${p.f_ini.toFixed(1)}–${p.f_fin.toFixed(1)} MHz</span>
    <span>Bloques</span><span>${p.bloques.length} · ~${p.minutos.toFixed(1)} min de grabación</span>
    <span>Formato</span><span><b>${p.formato}</b>${p.motivo?' <span class="hint">('+esc(p.motivo)+')</span>':''}</span>
    <span>Por bloque</span><span>${p.txt_bloque}</span>
    <span>Total</span><span>${p.txt_total}${p.formato==='sc8'?' <span class="hint">(sc16: '+p.txt_total_sc16+')</span>':''}</span>
    <span>Libre</span><span>${p.txt_libre}</span></div>`;
  p.avisos.forEach(a=> h+=`<div class="${p.cabe?'warnbox':'badbox'}">${esc(a)}</div>`);
  $('planbox').innerHTML=h;
  $('go').disabled = !p.cabe || busy();
  if(!estado.modo || estado.modo==='inactivo'){ bloques=p.bloques.map(b=>Object.assign({estado:'pendiente'},b)); renderBlq(); }
  if(!$('monfc').value && p.bloques.length) $('monfc').value=p.bloques[0].fc_mhz.toFixed(3);
}
async function doPlan(){
  $('err').textContent=''; saveCfg();
  const r=await api('/api/plan',getCfg());
  if(!r.ok){ $('err').textContent=r.error; $('planbox').innerHTML=''; $('go').disabled=true; return null; }
  showPlan(r.plan); return r.plan;
}
const BADGE={pendiente:'',grabando:'b-rec',ok:'b-ok','con overflows':'b-err',error:'b-err',interrumpido:'b-warn',cancelado:'b-warn','sin espacio':'b-err'};
function renderBlq(){
  if(!bloques.length){ $('tbody').innerHTML='<tr><td colspan="7" class="hint">Sin plan todavía.</td></tr>'; return; }
  $('tbody').innerHTML = bloques.map(b=>`<tr class="${b.estado==='grabando'?'act':''}">
    <td>${b.indice}</td><td>${b.fc_mhz.toFixed(3)}</td><td>${b.f_lo_mhz.toFixed(1)}–${b.f_hi_mhz.toFixed(1)}</td>
    <td>${b.n_earfcn? b.earfcn_ini+'–'+b.earfcn_fin+' <span class="hint">('+b.n_earfcn+')</span>':'–'}</td>
    <td><span class="badge ${BADGE[b.estado]||''}">${esc(b.estado)}</span></td>
    <td>${b.O!==undefined? b.O+' / '+b.D : '–'}</td>
    <td>${b.archivo? esc(b.archivo)+' <span class="hint">'+esc(b.tam)+'</span>':''}</td></tr>`).join('');
}
function busy(){ return ['grabando','esperando','monitor'].includes(estado.modo); }

function applyState(st){
  estado=st;
  const pill=$('pill'); pill.className='pill';
  const m=st.modo;
  if(m==='grabando'){ pill.classList.add('rec'); $('pilltxt').textContent='Grabando'; }
  else if(m==='esperando'){ pill.classList.add('wait'); $('pilltxt').textContent='Esperando confirmación'; }
  else if(m==='monitor'){ pill.classList.add('mon'); $('pilltxt').textContent='Monitor'; }
  else $('pilltxt').textContent='Inactivo';
  $('go').disabled = busy() || (plan && !plan.cabe);
  $('mon').disabled = busy(); $('btnplan').disabled = m==='grabando'||m==='esperando';
  $('stop').disabled = !busy();
  $('next').style.display = m==='esperando' ? '' : 'none';
  if(m==='grabando'||m==='esperando'){
    $('now').innerHTML = `Bloque ${st.i}/${st.n} · <small>${(st.fc_mhz||0).toFixed(3)} MHz</small>`;
    $('s_blq').textContent=`${st.i}/${st.n}`;
    $('s_pct').textContent=(st.pct||0).toFixed(1)+' %';
    $('barfill').style.width=(st.pct||0)+'%';
    $('clock').textContent = fmtT((Date.now()/1000)-st.inicio);
  } else if(m==='monitor'){
    $('now').innerHTML = `Monitor · <small>${(st.fc_mhz||0).toFixed(3)} MHz · no se guarda nada</small>`;
    $('s_blq').textContent='–'; $('s_pct').textContent='–'; $('barfill').style.width='0';
    $('clock').textContent = fmtT((Date.now()/1000)-st.inicio);
  } else { $('now').textContent='Sin grabación en curso'; }
  for(const [k,id] of [['O','s_o'],['D','s_d']]){ const v=st[k]||0; $(id).textContent=v; $(id).className=v?'bad':''; }
  if(st.txt_libre) $('s_libre').textContent=st.txt_libre;
  const al=$('alerta');
  if((st.O||0)+(st.D||0)>0 && m!=='inactivo'){ al.style.display='block';
    al.textContent=`⚠ Se están perdiendo muestras: ${st.O||0} overflows (O), ${st.D||0} paquetes perdidos (D). La captura tendrá huecos. Prueba sc8, cierra programas o sube net.core.rmem_max.`; }
  else al.style.display='none';
}
let polling=false;
async function poll(){
  if(polling) return; polling=true;   // evita dos consultas a la vez (líneas duplicadas)
  try{
    const r=await api('/api/poll?since='+since);
    applyState(r.estado);
    if(r.bloques && r.bloques.length){ bloques=r.bloques; renderBlq(); }
    const L=$('log');
    for(const [n,t,c] of r.log){ if(n<=since) continue; since=n; const d=document.createElement('div'); d.textContent=t; if(c) d.className=c; L.appendChild(d); }
    while(L.childElementCount>1500) L.firstChild.remove();
    if(r.log.length && $('autoscroll').checked) L.scrollTop=L.scrollHeight;
  }catch(e){ $('pilltxt').textContent='Sin conexión con el servidor'; }
  finally{ polling=false; }
}

// ---------- espectro + cascada ----------
const spec=$('spec'), wf=$('wf'), sctx=spec.getContext('2d'), wctx=wf.getContext('2d');
let wfImg=null;
function sizeCanvas(){
  const dpr=window.devicePixelRatio||1;
  for(const c of [spec]){ c.width=Math.round(c.clientWidth*dpr); c.height=Math.round(c.clientHeight*dpr); }
  const w=1024, h=Math.max(60,Math.round(wf.clientHeight));
  if(wf.width!==w || wf.height!==h){
    // conservar la cascada al cambiar el tamaño de la ventana
    const tmp=document.createElement('canvas'); tmp.width=wf.width; tmp.height=wf.height;
    if(wf.height>1) tmp.getContext('2d').drawImage(wf,0,0);
    wf.width=w; wf.height=h; wctx.fillStyle='#000'; wctx.fillRect(0,0,w,h);
    if(tmp.height>1) wctx.drawImage(tmp,0,0,tmp.width,Math.min(tmp.height,h),0,0,w,Math.min(tmp.height,h));
  }
  if(lastSpec) drawSpec(lastSpec);
}
function cmap(t){ // negro -> verde oscuro -> verde -> amarillo -> blanco
  t=Math.max(0,Math.min(1,t));
  const s=[[0,0,0,0],[.25,0,55,25],[.5,0,170,85],[.75,200,235,60],[1,255,255,255]];
  for(let i=1;i<s.length;i++) if(t<=s[i][0]){ const a=s[i-1],b=s[i],u=(t-a[0])/(b[0]-a[0]);
    return [a[1]+(b[1]-a[1])*u,a[2]+(b[2]-a[2])*u,a[3]+(b[3]-a[3])*u]; }
  return [255,255,255];
}
const PAD={l:46,r:10,t:10,b:24};
function drawSpec(s){
  const dpr=window.devicePixelRatio||1, W=spec.width, H=spec.height;
  const pl=PAD.l*dpr, pr=PAD.r*dpr, pt=PAD.t*dpr, pb=PAD.b*dpr, pw=W-pl-pr, ph=H-pt-pb;
  const lo=parseFloat($('dbmin').value), hi=parseFloat($('dbmax').value);
  const fc=s.fc_mhz, bw=s.fs/1e6, f0=fc-bw/2;
  const X=f=>pl+(f-f0)/bw*pw, Y=v=>pt+(1-(v-lo)/(hi-lo))*ph;
  sctx.clearRect(0,0,W,H);
  sctx.fillStyle='#020503'; sctx.fillRect(pl,pt,pw,ph);
  sctx.font=`${11*dpr}px ui-monospace,monospace`; sctx.fillStyle='#7d9a85'; sctx.strokeStyle='#13251a'; sctx.lineWidth=1;
  // zona del rango pedido y solapes
  const blq=bloques.find(b=>Math.abs(b.fc_mhz-fc)<1e-3);
  if(plan){ const a=Math.max(f0,plan.f_ini), b=Math.min(f0+bw,plan.f_fin);
    if(b>a){ sctx.fillStyle='rgba(34,211,122,.06)'; sctx.fillRect(X(a),pt,X(b)-X(a),ph); } }
  if(blq){ for(const o of bloques){ if(o===blq) continue;
      const a=Math.max(blq.f_lo_mhz,o.f_lo_mhz), b=Math.min(blq.f_hi_mhz,o.f_hi_mhz);
      if(b>a){ sctx.fillStyle='rgba(245,185,74,.08)'; sctx.fillRect(X(a),pt,X(b)-X(a),ph);} } }
  // rejilla
  for(let v=Math.ceil(lo/10)*10; v<=hi; v+=10){ const y=Y(v); sctx.beginPath(); sctx.moveTo(pl,y); sctx.lineTo(pl+pw,y); sctx.stroke();
    sctx.fillStyle='#7d9a85'; sctx.textAlign='right'; sctx.fillText(v,pl-5*dpr,y+4*dpr); }
  sctx.textAlign='center';
  const step = bw>15?2:1;
  for(let f=Math.ceil(f0/step)*step; f<=f0+bw; f+=step){ const x=X(f); sctx.beginPath(); sctx.moveTo(x,pt); sctx.lineTo(x,pt+ph); sctx.stroke();
    sctx.fillStyle='#7d9a85'; sctx.fillText(f.toFixed(0),x,H-7*dpr); }
  sctx.textAlign='left'; sctx.fillText('dBFS',pl+6*dpr,pt+14*dpr);
  // traza
  const d=s.db, n=d.length;
  const line=(arr,col,w)=>{ sctx.beginPath(); sctx.strokeStyle=col; sctx.lineWidth=w*dpr;
    for(let i=0;i<n;i++){ const x=pl+(i+.5)/n*pw, y=Math.min(pt+ph,Math.max(pt,Y(arr[i]))); i?sctx.lineTo(x,y):sctx.moveTo(x,y);} sctx.stroke(); };
  if($('maxhold').checked && maxh) line(maxh,'rgba(199,146,255,.7)',1);
  sctx.save(); sctx.beginPath(); sctx.rect(pl,pt,pw,ph); sctx.clip();
  const g=sctx.createLinearGradient(0,pt,0,pt+ph); g.addColorStop(0,'rgba(34,211,122,.35)'); g.addColorStop(1,'rgba(34,211,122,0)');
  sctx.beginPath(); for(let i=0;i<n;i++){ const x=pl+(i+.5)/n*pw, y=Y(d[i]); i?sctx.lineTo(x,y):sctx.moveTo(x,y);} sctx.lineTo(pl+pw,pt+ph); sctx.lineTo(pl,pt+ph); sctx.fillStyle=g; sctx.fill();
  sctx.restore();
  line(d,'#22d37a',1.3);
  sctx.fillStyle='#e2f2e6'; sctx.textAlign='right'; sctx.fillText(`Fc ${fc.toFixed(3)} MHz`,pl+pw-6*dpr,pt+14*dpr);
}
function pushWf(d){
  const w=wf.width, h=wf.height; if(!h) return;
  wctx.drawImage(wf,0,0,w,h-1,0,1,w,h-1);
  const lo=parseFloat($('dbmin').value), hi=parseFloat($('dbmax').value);
  const row=wctx.createImageData(w,1);
  for(let x=0;x<w;x++){ const v=d[Math.floor(x*d.length/w)], c=cmap((v-lo)/(hi-lo)); row.data.set([c[0],c[1],c[2],255],x*4); }
  wctx.putImageData(row,0,0);
}
async function specLoop(){
  try{
    if(busy()){
      const s=await api('/api/espectro');
      if(s.ok){
        $('specempty').style.display='none';
        if(lastSpec && lastSpec.fc_mhz!==s.fc_mhz) maxh=null;
        maxh = maxh ? maxh.map((v,i)=>Math.max(v,s.db[i])) : s.db.slice();
        lastSpec=s; drawSpec(s); pushWf(s.db);
        const p=s.pico_dbfs, pct=Math.max(0,Math.min(100,(p+60)/60*100));
        $('mfill').style.width=pct+'%';
        $('mfill').style.background = s.sat_pct>0.01||p>-1 ? 'var(--err)' : p>-6 ? 'var(--warn)' : p<-35 ? 'var(--muted)' : 'var(--ok)';
        $('mpico').textContent=p.toFixed(1)+' dBFS';
        $('msat').innerHTML = s.sat_pct>0.01 ? `<b style="color:var(--err)">SAT ${s.sat_pct.toFixed(2)} %</b>` : (p<-35?'<span>bajo</span>':'');
        $('specinfo').textContent=`${s.formato} · RMS ${s.rms_dbfs.toFixed(1)} dBFS · ${s.fs/1e6} Msps`;
      }
    }
  }catch(e){}
  setTimeout(specLoop, 250);
}
function autoEsc(){
  if(!lastSpec) return;
  const v=lastSpec.db.slice().sort((a,b)=>a-b);
  const piso=v[Math.floor(v.length*.1)], top=v[v.length-1];
  $('dbmin').value=Math.floor((piso-10)/5)*5; $('dbmax').value=Math.ceil((top+8)/5)*5;
  drawSpec(lastSpec);
}
const tip=$('tip');
spec.addEventListener('mousemove',ev=>{
  if(!lastSpec){ tip.style.display='none'; return; }
  const r=spec.getBoundingClientRect(), x=ev.clientX-r.left, pw=r.width-PAD.l-PAD.r;
  const u=(x-PAD.l)/pw; if(u<0||u>1){ tip.style.display='none'; return; }
  const bw=lastSpec.fs/1e6, f=lastSpec.fc_mhz-bw/2+u*bw, i=Math.min(lastSpec.db.length-1,Math.floor(u*lastSpec.db.length));
  const e=f2e($('banda').value,f), b=bandOf($('banda').value);
  tip.textContent=`${f.toFixed(3)} MHz · ${lastSpec.db[i].toFixed(1)} dBFS` + (b && e>=b.n_min && e<=b.n_max ? ` · EARFCN ${e}` : '');
  tip.style.display='block'; tip.style.left=Math.min(x+12,r.width-200)+'px'; tip.style.top=(ev.clientY-r.top+10)+'px';
});
spec.addEventListener('mouseleave',()=>tip.style.display='none');

// ---------- eventos ----------
async function init(){
  INFO=await api('/api/info');
  $('banda').innerHTML = INFO.bandas.map(b=>`<option value="${b.band}">Banda ${b.band} · ${b.f_min}–${b.f_max} MHz</option>`).join('');
  loadCfg(); hints();
  $('grabinfo').innerHTML = `Grabador: <code>${esc(INFO.grabador)}</code>` + (INFO.grabador_ok?'':' <b style="color:var(--err)">no existe: ejecuta compilar.sh</b>') +
    `<br>Tasa ${INFO.tasa/1e6} Msps (${INFO.mcr/1e6} MHz / 10). Solo se usa recepción: el programa nunca transmite.`;
  document.querySelectorAll('aside input, aside select').forEach(el=>el.addEventListener('change',()=>{ hints(); saveCfg(); if(el.id!=='monfc') plan=null; }));
  $('banda').addEventListener('change',()=>{ const b=bandOf($('banda').value); $('ini').value=b.n_min; $('fin').value=b.n_max; revisarRangos(false); hints(); saveCfg(); plan=null; doPlan(); });
  activarRangos(); ['ini','fin','ganancia'].forEach(id=>$(id).addEventListener('rangochange',()=>{ hints(); saveCfg(); plan=null; doPlan(); }));
  revisarRangos(false); saveCfg();
  $('todabanda').onclick=()=>{ const b=bandOf($('banda').value); $('ini').value=b.n_min; $('fin').value=b.n_max; revisarRangos(false); hints(); saveCfg(); doPlan(); };
  $('manual').onclick=()=>$('dlg').showModal();
  $('btnplan').onclick=doPlan;
  $('go').onclick=async()=>{
    const p=await doPlan(); if(!p||!p.cabe) return;
    let msg=`Se van a grabar ${p.bloques.length} bloque(s) en ${p.formato}, ${p.txt_total} en total (${p.minutos.toFixed(1)} min).`;
    if(p.previas && p.previas.length) msg += `\n\n⚠ ANTES SE ELIMINARÁN las grabaciones anteriores (${p.txt_previas}):\n` + p.previas.map(d=>`  • Banda ${d.banda} · ${d.id.split('_')[1]} · ${d.n} archivos · ${d.txt}`).join('\n');
    msg += '\n\n¿Empezar?';
    if(!confirm(msg)) return;
    maxh=null; const r=await api('/api/iniciar',getCfg()); if(!r.ok) $('err').textContent=r.error; else $('err').textContent='';
    poll();
  };
  $('mon').onclick=async()=>{
    $('err').textContent=''; saveCfg(); maxh=null;
    const r=await api('/api/monitor',Object.assign(getCfg(),{fc_mhz:$('monfc').value}));
    if(!r.ok) $('err').textContent=r.error; poll();
  };
  $('stop').onclick=async()=>{ if(estado.modo!=='monitor' && !confirm('¿Detener la grabación? Lo grabado del bloque actual se conserva.')) return; await api('/api/detener',{}); poll(); };
  $('next').onclick=async()=>{ await api('/api/siguiente',{}); poll(); };
  $('clrlog').onclick=()=>$('log').innerHTML='';
  $('autoesc').onclick=autoEsc;
  for(const id of ['dbmin','dbmax']) $(id).addEventListener('change',()=>lastSpec&&drawSpec(lastSpec));
  $('maxhold').addEventListener('change',()=>{ maxh=null; });
  window.addEventListener('resize',sizeCanvas); sizeCanvas();
  doPlan(); poll(); setInterval(poll,700); specLoop();
}
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
