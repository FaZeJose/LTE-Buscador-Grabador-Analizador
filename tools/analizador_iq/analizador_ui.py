#!/usr/bin/env python3
"""
Interfaz web local del analizador de grabaciones LTE.

Uso:
    python3 analizador_ui.py               # abre Brave en http://127.0.0.1:8780
    python3 analizador_ui.py --no-browser

Lee las grabaciones de grabador_iq (detecta la banda sola por los .json),
lanza analizar_grabacion.py y muestra las celdas, el espectrograma completo
de cada bloque y la presencia/Doppler de cada deteccion en el tiempo.
Escucha unicamente en 127.0.0.1.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import analizar_grabacion as A  # noqa: E402

DEF_CARPETA = str(Path.home() / "capturas_iq")


class Trabajo:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.eventos = []      # (n, evento) sin espectros
        self.n = 0
        self.espectros = {}    # bloque -> evento espectro
        self.estado = {"estado": "inactivo"}
        self.run = 0

    def ocupado(self):
        return self.proc is not None and self.proc.poll() is None

    def _add(self, ev):
        with self.lock:
            if ev.get("t") == "espectro":
                self.espectros[ev["i"]] = ev
                ev = {"t": "espectro_listo", "i": ev["i"]}
            elif ev.get("t") == "progreso":
                self.estado.update(fase=ev["fase"], pct=ev["pct"],
                                   velocidad=ev.get("velocidad", self.estado.get("velocidad")))
                return
            elif ev.get("t") == "bloque":
                self.estado.update(bloque=ev["i"], fc_mhz=ev["fc_mhz"])
            elif ev.get("t") == "inicio":
                self.estado.update(n_bloques=ev["n_bloques"], banda=ev["banda"])
            self.n += 1
            self.eventos.append((self.n, ev))
            # El registro completo queda en el .log del analisis: aqui solo los ultimos
            # mensajes, para que la memoria no crezca con analisis largos.
            if len(self.eventos) > 4000:
                corte = self.n - 2000
                self.eventos = [x for x in self.eventos if x[1].get("t") != "log" or x[0] > corte]

    def iniciar(self, c):
        if self.ocupado():
            raise ValueError("Ya hay un análisis en marcha")
        if not A.DECODER.exists():
            raise ValueError(f"Falta el decodificador {A.DECODER}. "
                             "Ejecuta ./install.sh en la carpeta del proyecto")
        carpeta = str(Path(c.get("carpeta") or DEF_CARPETA).expanduser())
        ses = A.listar_sesiones(carpeta)
        sesion = next((s for s in ses if s["id"] == c.get("sesion")), None)
        if not sesion:
            raise ValueError("Elige una grabación")
        banda = sesion["banda"]
        _, _, nmin, nmax = A.TABLA_EARFCN_DL[banda]
        lista = [x for x in str(c.get("earfcn", "")).replace(";", ",").replace(" ", ",").split(",")
                 if x.strip()]
        try:
            earfcns = [int(x) for x in lista]
        except ValueError:
            raise ValueError("Lista de EARFCN no válida")
        malos = [e for e in earfcns if not nmin <= e <= nmax]
        if malos:
            raise ValueError(f"EARFCN fuera de la banda {banda} ({nmin}–{nmax}): {malos}")
        modo = c.get("modo", "auto")
        if modo not in ("auto", "lista", "exhaustivo"):
            raise ValueError("Modo no válido")
        if modo == "lista" and not earfcns:
            raise ValueError("El modo «Solo EARFCN» necesita al menos un EARFCN")

        def num(k, lo, hi, tipo=float):
            try:
                v = tipo(c[k])
            except (KeyError, ValueError, TypeError):
                raise ValueError(f"Valor no válido en {k}")
            if not lo <= v <= hi:
                raise ValueError(f"{k} fuera de rango ({lo}–{hi})")
            return v
        cmd = [sys.executable, str(DIR / "analizar_grabacion.py"), "--eventos",
               "--carpeta", carpeta, "--sesion", sesion["id"], "--modo", modo,
               "--earfcn", ",".join(map(str, earfcns)),
               "--doppler", str(num("doppler", 0, 60)),
               "--paso-doppler", str(num("paso", 0.5, 15)),
               "--integracion", str(num("integracion", 5, 1000)),
               "--psr", str(num("psr", 1.2, 20)),
               "--ventanas-min", str(num("ventanas_min", 1, 50, int)),
               "--intentos", str(num("intentos", 1, 20, int)),
               "--ventana", str(num("ventana", 1, 10))]
        with self.lock:
            self.eventos, self.n, self.espectros = [], 0, {}
            self.run += 1
            self.estado = {"estado": "analizando", "inicio": time.time(), "sesion": sesion["id"],
                           "banda": banda, "n_bloques": len(sesion["bloques"])}
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._leer_err, args=(self.proc,), daemon=True).start()
        return {"ok": True, "run": self.run}

    def _leer(self, p):
        for line in p.stdout:
            try:
                self._add(json.loads(line))
            except ValueError:
                self._add({"t": "log", "txt": line.rstrip()})
        p.wait()
        with self.lock:
            self.estado["estado"] = "terminado" if p.returncode == 0 else (
                "detenido" if p.returncode in (-15, 143) else "error")
            self.estado["codigo"] = p.returncode
            self.estado["fin"] = time.time()

    def _leer_err(self, p):
        for line in p.stderr:
            line = line.rstrip()
            if line:
                self._add({"t": "log", "txt": line, "nivel": "err"})

    def detener(self):
        if self.ocupado():
            self.proc.send_signal(signal.SIGTERM)
        return True

    def poll(self, since, run):
        with self.lock:
            reset = run != self.run
            evs = [e for n, e in self.eventos if n > (0 if reset else since)]
            return {"run": self.run, "reset": reset, "next": self.n, "eventos": evs,
                    "estado": dict(self.estado)}


TRAB = Trabajo()


def resumen_sesiones(carpeta):
    out = []
    for s in A.listar_sesiones(carpeta):
        _, _, nmin, nmax = A.TABLA_EARFCN_DL.get(s["banda"], (0, 0, 0, 0))
        out.append({
            "id": s["id"], "banda": s["banda"], "sello": s["sello"], "formato": s["formato"],
            "bytes": s["bytes"], "duracion_s": s["duracion_s"], "rango_mhz": s["rango_mhz"],
            "n_min": nmin, "n_max": nmax,
            "bloques": [{"i": b["_indice"], "fc_mhz": round((b.get("frecuencia_real_hz") or
                                                             b["frecuencia_central_hz"]) / 1e6, 3),
                         "rango": b.get("rango_cubierto_mhz"),
                         "earfcn": b.get("earfcn_cubiertos"),
                         "dur": round(b.get("duracion_real_s", 0), 1),
                         "O": b.get("overflows_O", 0), "D": b.get("paquetes_perdidos_D", 0),
                         "hora": b.get("hora_inicio_utc", "")} for b in s["bloques"]],
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "AnalizadorIQ/1.0"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8")
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
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/info":
            return self._send(200, {"carpeta": DEF_CARPETA, "decoder": str(A.DECODER),
                                    "decoder_ok": A.DECODER.exists(), "cpus": os.cpu_count()})
        if u.path == "/api/sesiones":
            carpeta = q.get("carpeta", [DEF_CARPETA])[0] or DEF_CARPETA
            carpeta = str(Path(carpeta).expanduser())
            try:
                libre = shutil.disk_usage(carpeta).free if os.path.isdir(carpeta) else 0
            except OSError:
                libre = 0
            return self._send(200, {"sesiones": resumen_sesiones(carpeta),
                                    "existe": os.path.isdir(carpeta), "libre": libre})
        if u.path == "/api/poll":
            return self._send(200, TRAB.poll(int(q.get("since", ["0"])[0]),
                                             int(q.get("run", ["-1"])[0])))
        if u.path == "/api/espectro":
            ev = TRAB.espectros.get(int(q.get("i", ["0"])[0]))
            return self._send(200, ev or {})
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
            return self._send(400, {"error": "JSON inválido"})
        path = urlparse(self.path).path
        try:
            if path == "/api/iniciar":
                return self._send(200, TRAB.iniciar(body))
            if path == "/api/detener":
                return self._send(200, {"ok": TRAB.detener()})
        except (ValueError, OSError) as e:
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
    global DEF_CARPETA
    ap = argparse.ArgumentParser(description="Interfaz del analizador de grabaciones LTE")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--carpeta", default=DEF_CARPETA, help="carpeta de grabaciones por defecto")
    a = ap.parse_args()
    DEF_CARPETA = str(Path(a.carpeta).expanduser())
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("No hay puertos libres")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Analizador listo en {url}   (Ctrl+C para cerrar)")
    if not a.no_browser:
        threading.Timer(0.6, abrir_navegador, (url,)).start()

    def _salir(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _salir)
    signal.signal(signal.SIGHUP, _salir)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        TRAB.detener()
        if TRAB.proc:
            try:
                TRAB.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                TRAB.proc.kill()
        srv.server_close()


HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analizador de grabaciones LTE</title>
<style>
:root{--bg:#eef6fd;--panel:#ffffff;--panel2:#f3f9ff;--line:#cfe2f5;--text:#0b2545;--muted:#5d7b99;
--accent:#1565c0;--accent2:#0d47a1;--cel:#29b6f6;--cel2:#b3e5fc;--ok:#0277bd;--warn:#d98e04;--err:#d32f2f;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace}
*{box-sizing:border-box;scrollbar-width:none}
*::-webkit-scrollbar{display:none;width:0;height:0}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:11px 18px;background:linear-gradient(90deg,var(--accent2),var(--accent) 60%,#1e88e5);color:#fff;box-shadow:0 2px 8px rgba(13,71,161,.25)}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
header .sub{color:#cfe8ff;font-size:12px}
.pill{margin-left:auto;display:flex;align-items:center;gap:8px;padding:4px 12px;border-radius:99px;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.35);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;background:#cfe8ff}
.pill.run .dot{background:var(--cel);animation:pul 1.1s infinite}
.pill.err .dot{background:#ff8a80}
@keyframes pul{50%{opacity:.35}}
main{display:grid;grid-template-columns:360px minmax(0,1fr);gap:14px;padding:14px 18px;height:calc(100% - 52px)}
aside,.col{min-height:0;min-width:0;overflow-y:auto}
.col{display:flex;flex-direction:column;gap:12px}
.col>*{flex:none}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(13,71,161,.06)}
.card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);font-weight:700;display:flex;align-items:center;gap:8px}
label{display:block;font-size:12.5px;color:var(--muted);margin:9px 0 3px}
.hint{font-size:11.8px;color:var(--muted);margin:3px 0 0}
.hint b{color:var(--text);font-weight:600}
input,select{width:100%;background:#fff;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 9px;font:inherit}
input:focus,select:focus{outline:2px solid var(--cel2);border-color:var(--cel)}
input.bad{border-color:var(--warn)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--accent2);padding:7px 12px}
button:hover:not(:disabled){border-color:var(--cel);background:#e1f5fe}
button:disabled{opacity:.45;cursor:not-allowed}
.btnrow{display:flex;gap:10px;margin-top:12px}
.btn-go{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:650;flex:1}
.btn-go:hover:not(:disabled){background:var(--accent2)!important;border-color:var(--accent2)!important}
.btn-stop{background:#fff;border-color:var(--err);color:var(--err)}
.mini{font-size:12px;padding:3px 9px}
.aviso{display:none;margin-top:4px;padding:4px 8px;border-radius:6px;font-size:12px;background:#fff6e0;border:1px solid #f3c969;color:#8a5a00}
.err{color:var(--err);font-size:12.5px;margin-top:6px;min-height:1em}
.ses{border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-top:8px;cursor:pointer;background:#fff}
.ses:hover{border-color:var(--cel)}
.ses.sel{border-color:var(--accent);background:#e8f3ff;box-shadow:inset 3px 0 0 var(--accent)}
.ses b{color:var(--accent2)}
.ses .hint{margin:0}
.modos{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.modos label{margin:0;border:1px solid var(--line);border-radius:8px;padding:6px;cursor:pointer;color:var(--text);font-size:12.5px;text-align:center;background:#fff}
.modos input{display:none}
.modos label:has(input:checked){border-color:var(--accent);background:#e3f2fd;color:var(--accent2);font-weight:600}
.modos small{display:block;color:var(--muted);font-size:10.5px;font-weight:400}
details{margin-top:10px}
summary{cursor:pointer;color:var(--accent);font-size:12.5px}
.status{display:grid;grid-template-columns:1fr auto;gap:6px 18px;align-items:center}
.now{font-size:15px;font-weight:650;color:var(--accent2)}
.now small{color:var(--muted);font-weight:400}
.bar{height:8px;border-radius:6px;background:#e3f2fd;overflow:hidden;grid-column:1/-1}
.bar>div{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--cel));transition:width .3s}
.stats{display:flex;gap:18px;color:var(--muted);font-size:12.5px;grid-column:1/-1;flex-wrap:wrap}
.stats b{color:var(--text);font-variant-numeric:tabular-nums}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 1px 3px rgba(13,71,161,.06)}
.panel .bar2{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);background:var(--panel2);flex-wrap:wrap}
.panel .bar2 h2{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);font-weight:700}
.sp{flex:1}
.tbl{overflow:auto;max-height:440px}
table{border-collapse:collapse;width:max-content;min-width:100%;font-size:12.8px}
th,td{padding:6px 10px;border-bottom:1px solid #e4eef8;white-space:nowrap;text-align:left}
th{position:sticky;top:0;background:#e3f2fd;color:var(--accent2);font-weight:650;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;z-index:1}
td{font-variant-numeric:tabular-nums}
tbody tr{cursor:pointer}
tbody tr:hover td{background:#f1f8ff}
tbody tr.sel td{background:#dcefff}
tr.sat td:first-child{box-shadow:inset 3px 0 0 var(--cel)}
.badge{display:inline-block;padding:0 8px;border-radius:99px;font-size:11.5px;font-weight:650}
.b-full{background:var(--accent);color:#fff}.b-sib{background:#1e88e5;color:#fff}.b-mib{background:var(--cel2);color:var(--accent2)}
.b-pss{background:#eef3f8;color:var(--muted);border:1px dashed #b7cbe0}
.op-sat{color:var(--accent);font-weight:650}
.empty{color:var(--muted);padding:26px;text-align:center}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tabs button{padding:3px 10px;font-size:12px}
.tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
canvas{display:block;width:100%}
#cvSpec{height:150px}#cvWf{height:300px}#cvTl{height:200px}
.wrap{position:relative}
#tip{position:absolute;pointer-events:none;background:rgba(11,37,69,.92);color:#fff;border-radius:6px;padding:3px 8px;font:12px var(--mono);display:none;white-space:nowrap;z-index:2}
.log{overflow:auto;font:12.3px/1.5 var(--mono);padding:8px 12px;height:220px}
.log div{white-space:pre-wrap;word-break:break-all}
.log .head{color:var(--accent);font-weight:650}.log .ok{color:#0277bd}.log .warn{color:#8a5a00}.log .err{color:var(--err)}
.det{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:4px 16px;padding:10px 14px;font-size:12.5px}
.det span{color:var(--muted)}
.det b{color:var(--text);font-weight:600}
dialog{background:#fff;color:var(--text);border:1px solid var(--line);border-radius:12px;max-width:760px;width:92%;padding:18px 22px}
dialog::backdrop{background:rgba(11,37,69,.45)}
dialog h3{margin:0 0 8px;color:var(--accent2)}dialog h4{margin:14px 0 4px;color:var(--accent)}
dialog p,dialog li{font-size:13.5px}
@media(max-width:980px){main{grid-template-columns:1fr;height:auto}aside,.col{overflow:visible}}
</style>
</head>
<body>
<header>
  <h1>Analizador de grabaciones LTE</h1><span class="sub">búsqueda en toda la grabación · Doppler · srsRAN</span>
  <div class="pill" id="pill"><span class="dot"></span><span id="pilltxt">Inactivo</span></div>
</header>
<main>
<aside>
  <div class="card">
    <h2>Grabaciones</h2>
    <label for="carpeta">Carpeta</label>
    <div style="display:flex;gap:8px"><input id="carpeta" type="text"><button id="recargar" type="button">↻</button></div>
    <div id="sesiones"><div class="hint">Buscando…</div></div>
    <div class="hint" id="seshint"></div>
  </div>

  <div class="card">
    <h2>Búsqueda <button class="mini" id="ayuda" type="button" style="margin-left:auto;text-transform:none;letter-spacing:0">¿Cómo funciona?</button></h2>
    <div class="modos">
      <label><input type="radio" name="modo" value="auto">Auto<small>portadoras visibles + lista</small></label>
      <label><input type="radio" name="modo" value="lista">Solo EARFCN<small>rápido</small></label>
      <label><input type="radio" name="modo" value="exhaustivo">Exhaustivo<small>todos · lento</small></label>
    </div>
    <label for="earfcn">EARFCN a revisar siempre</label>
    <input id="earfcn" type="text" placeholder="p. ej. 1175, 1176">
    <button class="mini" id="preset" type="button" style="margin-top:6px">Entel D2C · banda 2 · 1175, 1176</button>
    <div class="hint" id="earfcnhint"></div>
    <div class="row2">
      <div><label for="doppler">Doppler ± kHz</label><input id="doppler" type="number" step="1" min="0" max="60"></div>
      <div><label for="paso">Paso kHz</label><input id="paso" type="number" step="0.5" min="0.5" max="15"></div>
    </div>
    <div class="hint"><b>Doppler:</b> cada EARFCN se prueba desplazado en ese rango. La PSS tolera ~±2,5 kHz por hipótesis, así que un paso de 5 kHz no pierde nada.</div>
    <div class="row2">
      <div><label for="integracion">Integración (ms)</label><input id="integracion" type="number" step="5" min="5" max="1000"></div>
      <div><label for="psr">PSR integrado mín.</label><input id="psr" type="number" step="0.1" min="1.2" max="20"></div>
    </div>
    <div class="hint"><b>Integración:</b> suma varias PSS seguidas (cada 5 ms) para sacar señales débiles del ruido. 100 ms = 20 PSS. Más = más sensible, pero si el satélite se mueve mucho el pico se ensancha.</div>
    <details>
      <summary>Avanzado</summary>
      <div class="row3">
        <div><label for="ventanas_min">Ventanas mín.</label><input id="ventanas_min" type="number" min="1" max="50"></div>
        <div><label for="intentos">Intentos/celda</label><input id="intentos" type="number" min="1" max="20"></div>
        <div><label for="ventana">Seg/intento</label><input id="ventana" type="number" step="0.5" min="1" max="10"></div>
      </div>
      <div class="hint"><b>Ventanas mín.:</b> una detección debe superar el umbral en al menos ese número de ventanas (filtra ruido). <b>Intentos/celda:</b> en cuántos momentos distintos (los de PSS más fuerte) se intenta decodificar MIB/SIB1.</div>
      <div class="hint" id="decinfo" style="margin-top:6px"></div>
    </details>
    <div class="hint" id="coste" style="margin-top:8px"></div>
    <div class="err" id="err"></div>
    <div class="btnrow">
      <button class="btn-go" id="go" type="button">▶ Analizar grabación</button>
      <button class="btn-stop" id="stop" type="button" disabled>■ Detener</button>
    </div>
  </div>
</aside>

<section class="col">
  <div class="card" style="margin:0">
    <div class="status">
      <div class="now" id="now">Sin análisis en curso</div>
      <div class="hint" id="clock">00:00</div>
      <div class="bar"><div id="barfill"></div></div>
      <div class="stats">
        <span>Bloque: <b id="s_blq">–</b></span>
        <span>Fase: <b id="s_fase">–</b></span>
        <span>Velocidad: <b id="s_vel">–</b></span>
        <span>Detecciones PSS: <b id="s_det">0</b></span>
        <span>Con MIB: <b id="s_mib">0</b></span>
        <span>Con SIB1: <b id="s_sib">0</b></span>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="bar2"><h2>Celdas encontradas</h2><span class="sp"></span>
      <span class="hint">Pulsa una fila para ver su presencia y Doppler en el tiempo</span>
      <button class="mini" id="csv" type="button">Exportar CSV</button></div>
    <div class="tbl" id="tbl"><div class="empty">Aún no hay celdas. Elige una grabación y pulsa «Analizar».</div></div>
  </div>

  <div class="panel" id="pdet" style="display:none">
    <div class="bar2"><h2 id="dettitle">Detalle</h2><span class="sp"></span>
      <span class="hint"><span style="color:#29b6f6">■</span> PSR por ventana &nbsp; <span style="color:#0d47a1">●</span> Doppler estimado</span></div>
    <div class="wrap"><canvas id="cvTl"></canvas></div>
    <div class="det" id="detgrid"></div>
  </div>

  <div class="panel">
    <div class="bar2"><h2>Espectrograma de la grabación</h2><div class="tabs" id="tabs"></div><span class="sp"></span>
      <span class="hint" id="wfinfo"></span></div>
    <div class="wrap" id="wfwrap">
      <canvas id="cvSpec"></canvas><canvas id="cvWf"></canvas><div id="tip"></div>
      <div class="empty" id="wfempty">Aparece al analizar cada bloque: tiempo hacia abajo, frecuencia a lo ancho. Las líneas marcan las celdas detectadas.</div>
    </div>
  </div>

  <div class="panel">
    <div class="bar2"><h2>Registro</h2><span class="sp"></span><button class="mini" id="clrlog" type="button">Limpiar</button></div>
    <div class="log" id="log"></div>
  </div>
</section>
</main>

<dialog id="dlg">
  <h3>Cómo busca este analizador</h3>
  <p>El buscador en vivo escucha ~0,5 s cada EARFCN y pasa al siguiente: si el satélite aparece justo después, se lo pierde. Aquí se tiene la grabación completa, así que <b>cada EARFCN se revisa durante toda la grabación</b> (por ejemplo los 180 s de cada bloque), sin esperar en tiempo real.</p>
  <h4>1 · Espectrograma</h4>
  <p>Se calcula el espectro de toda la grabación. En modo <b>Auto</b> sirve para elegir los EARFCN donde hay portadoras visibles.</p>
  <h4>2 · Detección de la PSS</h4>
  <p>En cada EARFCN candidato y en cada hipótesis de Doppler se busca la señal de sincronismo (PSS) en toda la grabación. Se suman varias PSS seguidas (integración), lo que permite detectar celdas <b>más débiles que el ruido</b>. El resultado es la presencia de cada celda en el tiempo y su Doppler: la curva típica de un satélite que pasa.</p>
  <h4>3 · Decodificación con srsRAN</h4>
  <p>En los momentos de PSS más fuerte se extraen unos segundos, se corrige el Doppler y se decodifican MIB, SIB1 (PLMN, operador, TAC, ECI) y SIB2 con la misma cadena de srsRAN que el buscador en vivo.</p>
  <h4>Modos</h4>
  <ul>
    <li><b>Solo EARFCN</b>: solo la lista (p. ej. 1175, 1176 de Entel D2C). Es el más rápido y sensible para una señal conocida.</li>
    <li><b>Auto</b>: portadoras visibles en el espectro más la lista. Buen punto de partida.</li>
    <li><b>Exhaustivo</b>: todos los EARFCN grabados. Encuentra señales bajo el ruido en cualquier canal, pero tarda mucho más.</li>
  </ul>
  <p>La banda, la frecuencia de cada bloque y el formato se leen de los .json del grabador: no hay que configurarlos.</p>
  <div class="btnrow"><button onclick="document.getElementById('dlg').close()">Cerrar</button></div>
</dialog>

<script>
const $=id=>document.getElementById(id);
const LS='analiq.v1';
let INFO=null, SES=[], sel=null, since=0, run=-1, estado={}, celdas={}, dets=[], espect={}, wfSel=null, filaSel=null, polling=false;
const FIELDS=['carpeta','earfcn','doppler','paso','integracion','psr','ventanas_min','intentos','ventana'];
const DEF={carpeta:'',earfcn:'',doppler:'15',paso:'5',integracion:'100',psr:'1.9',ventanas_min:'2',intentos:'4',ventana:'3',modo:'auto'};
const LIM={doppler:[0,60,'Doppler ± kHz'],paso:[0.5,15,'paso kHz'],integracion:[5,1000,'integración ms'],psr:[1.2,20,'PSR'],
  ventanas_min:[1,50,'ventanas mínimas'],intentos:[1,20,'intentos por celda'],ventana:[1,10,'segundos por intento']};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,b){ const o=b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}; return (await fetch(p,o)).json(); }

// ---------- avisos junto al campo ----------
function aviso(id,msg){ const inp=$(id); let el=$('av_'+id);
  if(!el){ el=document.createElement('div'); el.id='av_'+id; el.className='aviso'; inp.insertAdjacentElement('afterend',el); }
  el.textContent=msg||''; el.style.display=msg?'block':'none'; inp.classList.toggle('bad',!!msg); }
function limitar(id,vivo){ const [lo,hi,desc]=LIM[id], el=$(id), raw=el.value.trim();
  if(raw===''){ aviso(id, vivo?'':`Escribe un valor entre ${lo} y ${hi}.`); if(!vivo) el.value=DEF[id]; return; }
  const v=Number(raw);
  if(!Number.isFinite(v)){ aviso(id,`Valor no válido (${desc}: ${lo}–${hi}).`); if(!vivo) el.value=DEF[id]; return; }
  if(v<lo||v>hi){ const lim=v<lo?lo:hi, t=v<lo?'mínimo':'máximo';
    if(vivo) aviso(id,`${raw} está fuera de rango (${desc}: ${lo}–${hi}). Al salir del campo se ajusta al ${t}, ${lim}.`);
    else { el.value=lim; aviso(id,`${raw} estaba fuera de rango (${desc}: ${lo}–${hi}). Se ajustó al ${t}: ${lim}.`); }
    return; }
  aviso(id,'');
}
function revisarEarfcn(corrige){
  const s=SES.find(x=>x.id===sel), el=$('earfcn'); if(!s){ aviso('earfcn',''); return; }
  const toks=el.value.split(/[,;\s]+/).filter(Boolean), malos=[], buenos=[];
  for(const t of toks){ const n=Number(t);
    if(!Number.isInteger(n)) malos.push(t);
    else if(n<s.n_min||n>s.n_max) malos.push(t); else buenos.push(n); }
  if(malos.length){
    if(corrige){ el.value=buenos.join(', '); aviso('earfcn',`Quité ${malos.join(', ')}: fuera de la banda ${s.banda} (${s.n_min}–${s.n_max}) o no numéricos.`); }
    else aviso('earfcn',`${malos.join(', ')} fuera de la banda ${s.banda} (EARFCN ${s.n_min}–${s.n_max}). Al salir del campo se quitarán.`);
  } else aviso('earfcn','');
  // EARFCN que no quedaron grabados
  const fuera=buenos.filter(n=>{ const f=e2f(s.banda,n); return !s.bloques.some(b=>b.rango && f>=b.rango[0]+0.7 && f<=b.rango[1]-0.7); });
  $('earfcnhint').innerHTML = fuera.length ? `<span style="color:#8a5a00">${fuera.join(', ')} no quedó dentro de lo grabado: se ignorará.</span>` :
    (buenos.length ? buenos.map(n=>`${n} → ${e2f(s.banda,n).toFixed(1)} MHz`).join(' · ') : '');
}
const BANDAS={1:[2110,0],2:[1930,600],3:[1805,1200],4:[2110,1950],5:[869,2400],7:[2620,2750],8:[925,3450],12:[729,5010],13:[746,5180],14:[758,5280],17:[734,5730],20:[791,6150],25:[1930,8040],26:[859,8690],28:[758,9210],38:[2570,37750],40:[2300,38650],41:[2496,39650],66:[2110,66436],71:[617,68586]};
const e2f=(b,n)=>BANDAS[b][0]+0.1*(n-BANDAS[b][1]);
const f2e=(b,f)=>Math.round((f-BANDAS[b][0])/0.1+BANDAS[b][1]);

function getCfg(){ const c={}; FIELDS.forEach(f=>c[f]=$(f).value.trim()); c.modo=document.querySelector('input[name=modo]:checked')?.value||'auto'; c.sesion=sel; return c; }
function saveCfg(){ try{ localStorage.setItem(LS,JSON.stringify(Object.assign(getCfg(),{sesion:sel}))); }catch(e){} }
function loadCfg(){ let c={}; try{c=JSON.parse(localStorage.getItem(LS)||'{}');}catch(e){}
  c=Object.assign({},DEF,c); if(!c.carpeta) c.carpeta=INFO.carpeta;
  FIELDS.forEach(f=>$(f).value=c[f]??''); document.querySelectorAll('input[name=modo]').forEach(r=>r.checked=r.value===c.modo); return c; }

// ---------- sesiones ----------
function fmtB(n){ const u=['B','KiB','MiB','GiB','TiB']; let i=0; while(n>=1024&&i<4){n/=1024;i++;} return n.toFixed(i?1:0)+' '+u[i]; }
async function cargarSesiones(prefer){
  const r=await api('/api/sesiones?carpeta='+encodeURIComponent($('carpeta').value));
  SES=r.sesiones;
  if(!r.existe){ $('sesiones').innerHTML='<div class="hint" style="color:var(--err)">La carpeta no existe.</div>'; sel=null; coste(); return; }
  if(!SES.length){ $('sesiones').innerHTML='<div class="hint">No hay grabaciones del grabador en esta carpeta.</div>'; sel=null; coste(); return; }
  if(!SES.find(s=>s.id===sel)) sel = SES.find(s=>s.id===prefer)?.id || SES[0].id;
  $('sesiones').innerHTML=SES.map(s=>{
    const f=s.sello.replace(/(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z/,'$3/$2/$1 $4:$5 UTC');
    const ov=s.bloques.reduce((a,b)=>a+b.O+b.D,0);
    return `<div class="ses ${s.id===sel?'sel':''}" data-id="${esc(s.id)}">
      <b>Banda ${s.banda}</b> · ${s.bloques.length} bloque(s) · ${s.formato} · ${fmtB(s.bytes)}
      <div class="hint">${f} · ${s.rango_mhz?s.rango_mhz[0].toFixed(1)+'–'+s.rango_mhz[1].toFixed(1)+' MHz':''} · ${(s.duracion_s/60).toFixed(1)} min${ov?` · <span style="color:var(--err)">⚠ ${ov} overflows</span>`:''}</div></div>`; }).join('');
  document.querySelectorAll('.ses').forEach(d=>d.onclick=()=>{ sel=d.dataset.id; cargarSesiones(sel); saveCfg(); });
  const s=SES.find(x=>x.id===sel);
  $('seshint').innerHTML = s ? `Banda detectada automáticamente: <b>${s.banda}</b> (EARFCN ${s.n_min}–${s.n_max}). Bloques: ` +
     s.bloques.map(b=>`#${b.i} ${b.fc_mhz.toFixed(2)} MHz`).join(' · ') : '';
  $('preset').style.display = s && s.banda===2 ? '' : 'none';
  revisarEarfcn(false); coste();
}
function coste(){
  const s=SES.find(x=>x.id===sel), c=getCfg();
  if(!s){ $('coste').textContent=''; $('go').disabled=true; return; }
  const nd=Math.max(1,Math.floor(2*parseFloat(c.doppler||0)/Math.max(0.5,parseFloat(c.paso||5)))+1);
  const ne=c.earfcn.split(/[,;\s]+/).filter(Boolean).length;
  const dur=s.duracion_s;
  let txt;
  if(c.modo==='lista') txt=`${ne} EARFCN × ${nd} Doppler en ${(dur/60).toFixed(1)} min de grabación. Estimado: ~${Math.max(1,Math.round(dur/1.5/60))} min o menos.`;
  else if(c.modo==='exhaustivo'){ const tot=s.n_max-s.n_min+1; txt=`Hasta ${tot} EARFCN × ${nd} Doppler. Puede tardar mucho (varias veces la duración de la grabación). Para Starlink usa «Solo EARFCN».`; }
  else txt=`Portadoras visibles (±2 EARFCN) + ${ne} de la lista, × ${nd} Doppler. El tiempo depende de cuántas portadoras haya.`;
  $('coste').innerHTML='<b>Coste:</b> '+txt;
  $('go').disabled = estado.estado==='analizando';
}

// ---------- resultados ----------
const COLS=[['earfcn','EARFCN'],['freq_mhz','MHz'],['pci','PCI'],['bw','BW'],['estado','Estado'],['operador','Operador'],['plmn','PLMN'],['tac','TAC'],['enb','eNB / celda'],
  ['psr_max','PSR'],['presencia_pct','Presencia'],['visible','Visible (s)'],['doppler','Doppler (kHz)'],['snr_db','SNR'],['bloque','Bloque']];
function valor(f,k){
  switch(k){
    case 'bw': return f.prb?`${f.bw_mhz} MHz (${f.prb} PRB)`:'–';
    case 'estado': { const e=f.estado||'';
      const cls=e==='SIB1+SIB2'?'b-full':e==='SIB1'?'b-sib':e==='MIB'?'b-mib':'b-pss';
      return `<span class="badge ${cls}">${esc(e||'–')}</span>`; }
    case 'operador': return f.operador?`<span class="${/SATELITAL|posible satelital/.test(f.operador)?'op-sat':''}">${esc(f.operador)}</span>`:'–';
    case 'enb': return f.enb_id!=null?`${f.enb_id} / ${f.cell_local}`:'–';
    case 'presencia_pct': return f.presencia_pct!=null?f.presencia_pct.toFixed(1)+' %':'–';
    case 'visible': return f.visible_s?`${f.visible_s[0].toFixed(1)} – ${f.visible_s[1].toFixed(1)}`:'–';
    case 'doppler': return f.doppler_rango_hz?`${(f.doppler_rango_hz[0]/1e3).toFixed(1)} … ${(f.doppler_rango_hz[1]/1e3).toFixed(1)}`:'–';
    case 'snr_db': return f.snr_db!=null?f.snr_db.toFixed(1)+' dB':'–';
    default: return esc(f[k]??'–');
  }
}
function clave(f){ return f.earfcn+'|'+f.pci; }
function render(){
  const filas=Object.values(celdas).sort((a,b)=>(a.earfcn-b.earfcn)||String(a.pci).localeCompare(String(b.pci)));
  $('s_mib').textContent=filas.filter(f=>f.mib==='OK').length; $('s_sib').textContent=filas.filter(f=>f.sib1==='OK').length;
  if(!filas.length){ $('tbl').innerHTML='<div class="empty">Aún no hay celdas.</div>'; return; }
  const keep=$('tbl').scrollTop;
  $('tbl').innerHTML=`<table><thead><tr>${COLS.map(c=>`<th>${c[1]}</th>`).join('')}</tr></thead><tbody>`+
    filas.map(f=>`<tr data-k="${esc(clave(f))}" class="${clave(f)===filaSel?'sel':''} ${/SATELITAL|posible satelital/.test(f.operador||'')?'sat':''}">${COLS.map(c=>`<td>${valor(f,c[0])}</td>`).join('')}</tr>`).join('')+'</tbody></table>';
  $('tbl').scrollTop=keep;
  document.querySelectorAll('#tbl tbody tr').forEach(tr=>tr.onclick=()=>{ filaSel=tr.dataset.k; render(); detalle(); });
  if(filaSel) detalle();
}
function detOf(f){ const n2=typeof f.pci==='number'? f.pci%3 : parseInt(String(f.pci).replace(/\D+/g,''));
  return dets.find(d=>d.earfcn===f.earfcn && d.nid2===n2); }
function detalle(){
  const f=celdas[filaSel]; if(!f){ $('pdet').style.display='none'; return; }
  $('pdet').style.display='';
  $('dettitle').textContent=`EARFCN ${f.earfcn} · ${f.freq_mhz} MHz · PCI ${f.pci}`;
  const campos=[['Estado',f.estado],['Operador',f.operador],['PLMN',f.plmn],['MCC / MNC',f.mcc?f.mcc+' / '+f.mnc:null],['TAC',f.tac],['ECI',f.eci],
    ['eNB ID',f.enb_id],['Celda local',f.cell_local],['PRB / BW',f.prb?`${f.prb} / ${f.bw_mhz} MHz`:null],['Puertos',f.puertos],['CP',f.cp],['PHICH',f.phich],
    ['Banda (SIB1)',f.freq_band],['Barrada',f.cell_barred],['Qrxlevmin',f.q_rx_lev_min_dbm!=null?f.q_rx_lev_min_dbm+' dBm':null],['P-max',f.p_max],
    ['UL EARFCN',f.ul_earfcn],['UL BW',f.ul_bw_prb],['AC barring',f.ac_barring],['SIB programados',f.sched],
    ['CFO total',f.cfo_hz!=null?(f.cfo_hz/1e3).toFixed(2)+' kHz':null],['SNR',f.snr_db!=null?f.snr_db+' dB':null],
    ['PSR srsRAN',f.psr_srsran],['Decodificado en',f.t_decodificado_s!=null?f.t_decodificado_s+' s':null],['Bloque',f.bloque]];
  $('detgrid').innerHTML=campos.filter(c=>c[1]!=null&&c[1]!=='').map(c=>`<div><span>${c[0]}</span><br><b>${esc(c[1])}</b></div>`).join('');
  dibujarTl(detOf(f), f);
}

// ---------- graficos ----------
function ajustar(c){ const d=window.devicePixelRatio||1; c.width=Math.round(c.clientWidth*d); c.height=Math.round(c.clientHeight*d); return d; }
function dibujarTl(d, f){
  const c=$('cvTl'), dpr=ajustar(c), x=c.getContext('2d'), W=c.width, H=c.height;
  x.clearRect(0,0,W,H); x.font=`${11*dpr}px ui-monospace,monospace`;
  const pl=48*dpr, pr=56*dpr, pt=22*dpr, pb=24*dpr, pw=W-pl-pr, ph=H-pt-pb;
  x.fillStyle='#f6fbff'; x.fillRect(pl,pt,pw,ph);
  if(!d){ x.fillStyle='#5d7b99'; x.fillText('Sin datos de detección para esta fila.',pl+10*dpr,pt+20*dpr); return; }
  const dur=(espect[d.bloque]&&espect[d.bloque].dur_s)||Math.max(...d.t_s)+1;
  const pmax=Math.max(4,...d.psr)*1.1, dmin=Math.min(...d.doppler_hz)-1000, dmax=Math.max(...d.doppler_hz)+1000;
  const X=t=>pl+t/dur*pw, Yp=v=>pt+ph-(v/pmax)*ph, Yd=v=>pt+ph-((v-dmin)/(dmax-dmin))*ph;
  x.strokeStyle='#dbe9f7'; x.lineWidth=1; x.fillStyle='#5d7b99';
  for(let i=0;i<=4;i++){ const y=pt+ph*i/4; x.beginPath(); x.moveTo(pl,y); x.lineTo(pl+pw,y); x.stroke();
    x.textAlign='right'; x.fillText((pmax*(1-i/4)).toFixed(1),pl-5*dpr,y+4*dpr);
    x.textAlign='left'; x.fillStyle='#0d47a1'; x.fillText(((dmax-(dmax-dmin)*i/4)/1e3).toFixed(1),pl+pw+5*dpr,y+4*dpr); x.fillStyle='#5d7b99'; }
  x.textAlign='center'; const st=dur>120?30:dur>40?10:dur>15?5:1;
  for(let t=0;t<=dur;t+=st) x.fillText(t+' s',X(t),H-7*dpr);
  x.textAlign='left'; x.fillText('PSR',4*dpr,12*dpr); x.fillStyle='#0d47a1'; x.textAlign='right'; x.fillText('Doppler kHz',W-4*dpr,12*dpr);
  // umbral
  const th=parseFloat($('psr').value)||1.9; x.strokeStyle='#f3c969'; x.setLineDash([4*dpr,4*dpr]); x.beginPath(); x.moveTo(pl,Yp(th)); x.lineTo(pl+pw,Yp(th)); x.stroke(); x.setLineDash([]);
  const bw=Math.max(1.5*dpr,pw/(dur/0.1)*0.9);
  x.fillStyle='#29b6f6'; d.t_s.forEach((t,i)=>{ x.fillRect(X(t),Yp(d.psr[i]),bw,pt+ph-Yp(d.psr[i])); });
  x.fillStyle='#0d47a1'; d.t_s.forEach((t,i)=>{ x.beginPath(); x.arc(X(t)+bw/2,Yd(d.doppler_hz[i]),2.2*dpr,0,7); x.fill(); });
  if(f && f.t_decodificado_s!=null){ x.strokeStyle='#1565c0'; x.lineWidth=2*dpr; const xx=X(f.t_decodificado_s); x.beginPath(); x.moveTo(xx,pt); x.lineTo(xx,pt+ph); x.stroke();
    x.fillStyle='#1565c0'; x.textAlign='left'; x.fillText('SIB1',xx+4*dpr,pt+12*dpr); }
}
function cmap(t){ t=Math.max(0,Math.min(1,t)); const s=[[0,11,37,69],[.35,13,71,161],[.65,41,182,246],[.85,179,229,252],[1,255,255,255]];
  for(let i=1;i<s.length;i++) if(t<=s[i][0]){ const a=s[i-1],b=s[i],u=(t-a[0])/(b[0]-a[0]); return [a[1]+(b[1]-a[1])*u,a[2]+(b[2]-a[2])*u,a[3]+(b[3]-a[3])*u]; } return [255,255,255]; }
function tabs(){ const ks=Object.keys(espect).map(Number).sort((a,b)=>a-b);
  $('tabs').innerHTML=ks.map(k=>`<button type="button" class="${k===wfSel?'on':''}" data-i="${k}">Bloque ${k}</button>`).join('');
  document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{ wfSel=+b.dataset.i; tabs(); dibujarWf(); }); }
function dibujarWf(){
  const e=espect[wfSel]; if(!e){ return; } $('wfempty').style.display='none';
  const s=SES.find(x=>x.id===estado.sesion)||SES.find(x=>x.id===sel), banda=s?s.banda:2;
  const filas=e.filas_db, nr=filas.length, nb=filas[0].length;
  const all=filas.flat().sort((a,b)=>a-b), lo=all[Math.floor(all.length*0.05)], hi=all[all.length-1];
  $('wfinfo').textContent=`${e.f_mhz[0].toFixed(2)} – ${e.f_mhz[1].toFixed(2)} MHz · ${e.dur_s.toFixed(0)} s · ${(e.dur_s/nr).toFixed(2)} s/fila`;
  // espectro max/medio
  const c1=$('cvSpec'), dpr=ajustar(c1), x=c1.getContext('2d'), W=c1.width, H=c1.height; x.clearRect(0,0,W,H);
  const pl=40*dpr, pr=8*dpr, pw=W-pl-pr, pt=6*dpr, ph=H-pt-18*dpr;
  const mx=Math.max(...e.max_db)+3, mn=Math.min(...e.mean_db)-3;
  const Y=v=>pt+ph-(v-mn)/(mx-mn)*ph;
  x.fillStyle='#f6fbff'; x.fillRect(pl,pt,pw,ph); x.font=`${10.5*dpr}px ui-monospace,monospace`;
  const F0=e.f_mhz[0], F1=e.f_mhz[1], X=f=>pl+(f-F0)/(F1-F0)*pw;
  x.fillStyle='#5d7b99'; x.textAlign='center';
  for(let f=Math.ceil(F0);f<=F1;f+=2){ x.fillText(f.toFixed(0),X(f),H-4*dpr); x.strokeStyle='#e1edf8'; x.beginPath(); x.moveTo(X(f),pt); x.lineTo(X(f),pt+ph); x.stroke(); }
  x.textAlign='right'; x.fillText('dB',pl-6*dpr,pt+10*dpr);
  const linea=(arr,col)=>{ x.beginPath(); x.strokeStyle=col; x.lineWidth=1.2*dpr; arr.forEach((v,i)=>{ const xx=pl+(i+.5)/nb*pw, yy=Y(v); i?x.lineTo(xx,yy):x.moveTo(xx,yy); }); x.stroke(); };
  linea(e.max_db,'#29b6f6'); linea(e.mean_db,'#0d47a1');
  // cascada
  const c2=$('cvWf'); ajustar(c2); const y=c2.getContext('2d'), W2=c2.width, H2=c2.height;
  const img=y.createImageData(nb,nr);
  for(let r=0;r<nr;r++) for(let k=0;k<nb;k++){ const col=cmap((filas[r][k]-lo)/(hi-lo)); img.data.set([col[0],col[1],col[2],255],(r*nb+k)*4); }
  const tmp=document.createElement('canvas'); tmp.width=nb; tmp.height=nr; tmp.getContext('2d').putImageData(img,0,0);
  y.imageSmoothingEnabled=false; y.clearRect(0,0,W2,H2); y.drawImage(tmp,0,0,nb,nr,pl,0,pw,H2);
  y.fillStyle='#5d7b99'; y.font=`${10.5*dpr}px ui-monospace,monospace`; y.textAlign='right';
  for(let t=0;t<=e.dur_s;t+=Math.max(5,Math.round(e.dur_s/6/5)*5)) y.fillText(t+' s',pl-4*dpr,Math.min(H2-3*dpr,t/e.dur_s*H2+10*dpr));
  // marcas de celdas del bloque
  for(const f of Object.values(celdas)){ if(f.bloque!==wfSel) continue; const xx=X(f.freq_mhz);
    y.strokeStyle='rgba(255,255,255,.85)'; y.setLineDash([3*dpr,3*dpr]); y.beginPath(); y.moveTo(xx,0); y.lineTo(xx,H2); y.stroke(); y.setLineDash([]);
    const et=`PCI ${f.pci}`, tw=y.measureText(et).width+8*dpr;
    y.fillStyle='rgba(11,37,69,.85)'; y.fillRect(xx+3*dpr,2*dpr,tw,15*dpr);
    y.fillStyle='#fff'; y.textAlign='left'; y.fillText(et,xx+7*dpr,13*dpr);
    if(f.visible_s){ y.fillStyle='rgba(41,182,246,.9)'; y.fillRect(xx-2*dpr,f.visible_s[0]/e.dur_s*H2,4*dpr,Math.max(2,(f.visible_s[1]-f.visible_s[0]+0.1)/e.dur_s*H2)); } }
}
$('cvWf').addEventListener('mousemove',ev=>{ const e=espect[wfSel]; if(!e) return;
  const r=$('cvWf').getBoundingClientRect(), pl=40, pw=r.width-48, u=(ev.clientX-r.left-pl)/pw; if(u<0||u>1){ $('tip').style.display='none'; return; }
  const f=e.f_mhz[0]+u*(e.f_mhz[1]-e.f_mhz[0]), t=(ev.clientY-r.top)/r.height*e.dur_s, s=SES.find(x=>x.id===(estado.sesion||sel));
  $('tip').textContent=`${f.toFixed(3)} MHz`+(s?` · EARFCN ${f2e(s.banda,f)}`:'')+` · ${t.toFixed(1)} s`;
  $('tip').style.display='block'; $('tip').style.left=Math.min(ev.clientX-r.left+12,r.width-230)+'px'; $('tip').style.top=(ev.clientY-$('wfwrap').getBoundingClientRect().top+12)+'px'; });
$('cvWf').addEventListener('mouseleave',()=>$('tip').style.display='none');

// ---------- sondeo ----------
const FASE={espectro:'espectrograma',pss:'detección PSS',decodificando:'decodificando (srsRAN)'};
function fmtT(s){ s=Math.max(0,Math.floor(s)); return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); }
function logl(t,c){ const L=$('log'), d=document.createElement('div'); d.textContent=t; if(c) d.className=c; L.appendChild(d); while(L.childElementCount>3000) L.firstChild.remove(); L.scrollTop=L.scrollHeight; }
async function poll(){
  if(polling) return; polling=true;
  try{
    const r=await api(`/api/poll?since=${since}&run=${run}`);
    if(r.reset){ run=r.run; celdas={}; dets=[]; espect={}; wfSel=null; filaSel=null; $('log').innerHTML=''; $('tabs').innerHTML=''; render(); $('pdet').style.display='none'; $('wfempty').style.display=''; }
    let cambio=false;
    for(const ev of r.eventos){
      if(ev.t==='log') logl(ev.txt,ev.nivel);
      else if(ev.t==='celda'){ celdas[clave(ev.fila)]=ev.fila; for(const k of Object.keys(celdas)) if(k.startsWith(ev.fila.earfcn+'|?') && typeof ev.fila.pci==='number' && parseInt(k.split('=')[1])===ev.fila.pci%3) delete celdas[k]; cambio=true; }
      else if(ev.t==='deteccion'){ dets.push(ev.det); $('s_det').textContent=dets.length; }
      else if(ev.t==='espectro_listo'){ const e=await api('/api/espectro?i='+ev.i); if(e.i){ espect[e.i]=e; wfSel=e.i; tabs(); dibujarWf(); } }
      else if(ev.t==='fin'){ for(const f of ev.celdas) celdas[clave(f)]=f; cambio=true; }
    }
    if(cambio){ render(); if(wfSel) dibujarWf(); }
    since=r.next; estado=r.estado; pintarEstado();
  }catch(e){ $('pilltxt').textContent='Sin conexión con el servidor'; }
  finally{ polling=false; }
}
function pintarEstado(){
  const st=estado, p=$('pill'); p.className='pill';
  const L={inactivo:'Inactivo',analizando:'Analizando…',terminado:'Terminado',detenido:'Detenido',error:'Terminó con error'};
  $('pilltxt').textContent=L[st.estado]||'Inactivo';
  if(st.estado==='analizando') p.classList.add('run'); if(st.estado==='error') p.classList.add('err');
  const an=st.estado==='analizando';
  $('go').disabled=an||!sel; $('stop').disabled=!an;
  if(st.inicio) $('clock').textContent=fmtT((an?Date.now()/1000:(st.fin||Date.now()/1000))-st.inicio);
  if(st.bloque){ $('s_blq').textContent=`${st.bloque}/${st.n_bloques}`;
    $('now').innerHTML=`Banda ${st.banda} · bloque ${st.bloque}/${st.n_bloques} <small>· ${st.fc_mhz?st.fc_mhz.toFixed(2)+' MHz':''}</small>`; }
  else if(!an) $('now').textContent= st.estado==='inactivo'?'Sin análisis en curso':(L[st.estado]||'');
  $('s_fase').textContent=an?(FASE[st.fase]||'–'):(st.estado==='inactivo'?'–':(L[st.estado]||'–'));
  $('s_vel').textContent=st.velocidad?`${st.velocidad.toFixed(2)}× tiempo real`:'–';
  const nb=st.n_bloques||1, bi=(st.bloque||1)-1, pf={espectro:[0,.15],pss:[.15,.8],decodificando:[.8,1]}[st.fase]||[0,0];
  const pct=st.estado==='terminado'?100:100*(bi+pf[0]+(pf[1]-pf[0])*(st.pct||0)/100)/nb;
  $('barfill').style.width=pct+'%';
}
function exportCsv(){
  const filas=Object.values(celdas); if(!filas.length) return;
  const cols=['earfcn','freq_mhz','pci','prb','bw_mhz','estado','operador','plmn','mcc','mnc','tac','eci','enb_id','cell_local','psr_max','presencia_pct','visible_s','doppler_rango_hz','cfo_hz','snr_db','bloque','banda','cell_barred','freq_band','ul_earfcn','sched'];
  const q=v=>'"'+String(Array.isArray(v)?v.join(' a '):(v??'')).replace(/"/g,'""')+'"';
  const txt='﻿'+cols.join(',')+'\n'+filas.map(f=>cols.map(c=>q(f[c])).join(',')).join('\n');
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([txt],{type:'text/csv'})); a.download='celdas_grabacion.csv'; a.click();
}

// ---------- inicio ----------
(async function(){
  INFO=await api('/api/info'); const c=loadCfg();
  $('decinfo').innerHTML=`Decodificador: <code>${esc(INFO.decoder)}</code>`+(INFO.decoder_ok?'':' <b style="color:var(--err)">no existe: compila con make analizar_celda_iq</b>')+` · ${INFO.cpus} núcleos`;
  for(const id of Object.keys(LIM)){ $(id).addEventListener('input',()=>{ limitar(id,true); coste(); }); $(id).addEventListener('change',()=>{ limitar(id,false); coste(); saveCfg(); }); }
  $('earfcn').addEventListener('input',()=>{ revisarEarfcn(false); coste(); });
  $('earfcn').addEventListener('change',()=>{ revisarEarfcn(true); coste(); saveCfg(); });
  document.querySelectorAll('input[name=modo]').forEach(r=>r.addEventListener('change',()=>{ coste(); saveCfg(); }));
  $('carpeta').addEventListener('change',()=>{ cargarSesiones(); saveCfg(); });
  $('recargar').onclick=()=>cargarSesiones(sel);
  $('preset').onclick=()=>{ $('earfcn').value='1175, 1176'; document.querySelector('input[name=modo][value=lista]').checked=true; revisarEarfcn(true); coste(); saveCfg(); };
  $('ayuda').onclick=()=>$('dlg').showModal();
  $('go').onclick=async()=>{ $('err').textContent=''; Object.keys(LIM).forEach(id=>limitar(id,false)); revisarEarfcn(true); saveCfg();
    const r=await api('/api/iniciar',getCfg()); if(!r.ok){ $('err').textContent=r.error; return; } since=0; poll(); };
  $('stop').onclick=()=>api('/api/detener',{});
  $('csv').onclick=exportCsv; $('clrlog').onclick=()=>$('log').innerHTML='';
  window.addEventListener('resize',()=>{ if(wfSel) dibujarWf(); if(filaSel) detalle(); });
  await cargarSesiones(c.sesion); poll(); setInterval(poll,600); setInterval(pintarEstado,1000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
