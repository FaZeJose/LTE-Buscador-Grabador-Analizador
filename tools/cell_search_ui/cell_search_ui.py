#!/usr/bin/env python3
"""
Interfaz web local para cell_search_plmn (srsRAN_4G).

Uso:
    python3 cell_search_ui.py                 # abre Brave en http://127.0.0.1:8765
    python3 cell_search_ui.py --bin RUTA      # otra ruta del binario
    python3 cell_search_ui.py --no-browser    # solo levanta el servidor

Solo usa la libreria estandar de Python 3. Escucha unicamente en 127.0.0.1.
La tabla de celdas se arma leyendo el "cuadro" que imprime el programa
(lineas que empiezan con ┌ │ ├ └), con columnas dinamicas: si agregas campos
nuevos al cuadro en C++, aparecen solos como columnas nuevas.
"""
import argparse
import csv
import errno
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_BIN = os.environ.get("CELL_SEARCH_PLMN", str(REPO / "build/lib/examples/cell_search_plmn"))
LOG_DIR = Path.home() / "cell_search_logs"
MAX_EARFCN = 1000  # MAX_EARFCN del programa en C++

# Bandas LTE FDD (3GPP TS 36.101): banda -> (F_DL_low MHz, N_offs-DL, N_DL min, N_DL max)
BANDS = {
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
    18: (860.0, 5850, 5850, 5999),
    19: (875.0, 6000, 6000, 6149),
    20: (791.0, 6150, 6150, 6449),
    25: (1930.0, 8040, 8040, 8689),
    26: (859.0, 8690, 8690, 9039),
    28: (758.0, 9210, 9210, 9659),
    66: (2110.0, 66436, 66436, 67335),
    71: (617.0, 68586, 68586, 68935),
}


def band_info():
    out = []
    for b, (flow, noff, nmin, nmax) in sorted(BANDS.items()):
        out.append({
            "band": b,
            "n_min": nmin,
            "n_max": nmax,
            "f_min": round(flow + 0.1 * (nmin - noff), 1),
            "f_max": round(flow + 0.1 * (nmax - noff), 1),
            "f_low": flow,
            "n_offs": noff,
        })
    return out


# ---------------------------------------------------------------- parser de la salida
PROG = re.compile(
    r"^\[\s*(\d+)/(\d+)\]: EARFCN (\d+) Freq\. ([\d.]+) MHz looking for PSS"
    r"(?: \(offset ([+-]?[\d.]+) kHz\))?")
BOX_TOP = re.compile(r"^┌─ (CELDA #(\d+)) ─*┐\s*$")
BOX_SEC = re.compile(r"^├─ (.+?) ─*┤\s*$")
BOX_END = re.compile(r"^└─*┘\s*$")
BOX_LINE = re.compile(r"^│ (.*) │\s*$")
KEYVAL = re.compile(r"^([^:]{1,40}):\s+(.*)$")


class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.run_id = 0
        self.events = []
        self.rows = []
        self.proc = None
        self.state = "idle"  # idle | running | stopping | finished | error
        self.cmd = ""
        self.started = 0.0
        self.exit_code = None
        self.progress = {}
        self.box = None
        self.logf = None
        self.csv_path = None

    # ---- construir comando
    @staticmethod
    def build_cmd(cfg):
        binp = os.path.expanduser((cfg.get("bin") or DEFAULT_BIN).strip())
        if not os.path.isfile(binp):
            raise ValueError("No existe el programa: %s" % binp)
        if not os.access(binp, os.X_OK):
            raise ValueError("El programa no es ejecutable: %s" % binp)
        a = [binp]

        def has(k):
            return cfg.get(k) not in (None, "")

        band = int(cfg["band"])
        if band not in BANDS:
            raise ValueError("Banda no soportada: %d" % band)
        nmin, nmax = BANDS[band][2], BANDS[band][3]
        for k, nombre in (("start", "inicio"), ("end", "fin")):
            if has(k) and not nmin <= int(cfg[k]) <= nmax:
                raise ValueError("EARFCN de %s fuera de la banda %d (%d-%d)" % (nombre, band, nmin, nmax))
        if has("gain") and not 0 <= float(cfg["gain"]) <= 49:
            raise ValueError("Ganancia fuera de rango (0-49 dB)")
        a += ["-b", str(band)]
        if has("start"):
            a += ["-s", str(int(cfg["start"]))]
        if has("end"):
            a += ["-e", str(int(cfg["end"]))]
        if has("gain"):
            a += ["-g", str(float(cfg["gain"]))]
        if has("n"):
            a += ["-n", str(int(cfg["n"]))]
        if has("off") and float(cfg["off"]) > 0:
            a += ["-o", str(float(cfg["off"]))]
            if has("step") and float(cfg["step"]) > 0:
                a += ["-k", str(float(cfg["step"]))]
        if has("mib"):
            a += ["-m", str(int(cfg["mib"]))]
        if has("sib1"):
            a += ["-r", str(int(cfg["sib1"]))]
        if has("psr"):
            a += ["-p", str(float(cfg["psr"]))]
        if cfg.get("json"):
            a += ["-j"]
        if cfg.get("verbose"):
            a += ["-v"]
        if has("rfargs"):
            a += ["-a", str(cfg["rfargs"]).strip()]
        if has("extra"):
            a += shlex.split(str(cfg["extra"]))
        return a

    # ---- iniciar / detener
    def start(self, cfg):
        with self.lock:
            if self.state in ("running", "stopping"):
                raise ValueError("Ya hay una busqueda en curso.")
        cmd = self.build_cmd(cfg)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))
        except OSError:
            pass
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                                start_new_session=True, close_fds=True)
        os.close(slave)
        with self.lock:
            self.run_id += 1
            self.events = []
            self.rows = []
            self.proc = proc
            self.state = "running"
            self.cmd = " ".join(shlex.quote(c) for c in cmd)
            self.started = time.time()
            self.exit_code = None
            self.progress = {}
            self.box = None
            self.csv_path = LOG_DIR / ("celdas_%s.csv" % stamp)
            self.logf = open(LOG_DIR / ("log_%s.txt" % stamp), "w", encoding="utf-8")
            run = self.run_id
        self._line("$ " + self.cmd)
        threading.Thread(target=self._reader, args=(master, proc, run), daemon=True).start()
        return {"ok": True, "run": run, "cmd": self.cmd}

    def stop(self):
        with self.lock:
            p = self.proc
            if not p or p.poll() is not None or self.state not in ("running", "stopping"):
                return False
            self.state = "stopping"
        try:
            os.killpg(p.pid, signal.SIGINT)
        except ProcessLookupError:
            return True
        threading.Timer(12, self._force, (p, signal.SIGTERM)).start()
        threading.Timer(25, self._force, (p, signal.SIGKILL)).start()
        return True

    @staticmethod
    def _force(p, sig):
        if p.poll() is None:
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                pass

    # ---- lectura de la salida
    def _reader(self, master, proc, run):
        buf = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._line(line.decode("utf-8", "replace").rstrip("\r"))
        if buf:
            self._line(buf.decode("utf-8", "replace").rstrip("\r"))
        try:
            os.close(master)
        except OSError:
            pass
        rc = proc.wait()
        with self.lock:
            if self.run_id != run:
                return
            self.exit_code = rc
            self.state = "finished" if rc in (0, -signal.SIGINT, 130) or self.state == "stopping" else "error"
            if self.logf:
                self.logf.close()
                self.logf = None

    def _emit(self, ev):
        self.events.append(ev)

    def _line(self, line):
        with self.lock:
            self._emit({"t": "log", "line": line})
            if self.logf:
                self.logf.write(line + "\n")
                self.logf.flush()
            m = PROG.match(line)
            if m:
                self.progress = {"cur": int(m.group(1)), "total": int(m.group(2)),
                                 "earfcn": int(m.group(3)), "freq": float(m.group(4)),
                                 "offset": float(m.group(5)) if m.group(5) else 0.0}
            self._box_line(line)

    # ---- armado de filas a partir del cuadro
    def _box_line(self, line):
        m = BOX_TOP.match(line)
        if m:
            self.box = {"n": int(m.group(2)), "sec": "FISICO", "kv": [], "last": None,
                        "failed": {}, "seen": {}}
            return
        b = self.box
        if b is None:
            return
        if BOX_END.match(line):
            self._finish_box()
            self.box = None
            return
        m = BOX_SEC.match(line)
        if m:
            b["sec"] = m.group(1).strip()
            b["last"] = None
            return
        m = BOX_LINE.match(line)
        if not m:
            return
        raw = m.group(1).rstrip()
        if raw.startswith("   ") and b["last"] is not None:  # continuacion de linea larga
            k, v = b["kv"][b["last"]]
            b["kv"][b["last"]] = (k, (v + " " + raw.strip()).strip())
            return
        for seg in re.split(r"\s{3,}", raw.strip()):
            km = KEYVAL.match(seg)
            if km:
                key = km.group(1).strip()
                val = re.sub(r"\s{2,}", " ", km.group(2).strip())
                if key in b["seen"]:
                    key = "%s %s" % (b["sec"], key)
                b["seen"][key] = True
                b["kv"].append((key, val))
                b["last"] = len(b["kv"]) - 1
            elif seg.lower().startswith("no decodificado"):
                b["failed"][b["sec"]] = True

    def _finish_box(self):
        b = self.box
        row = {"#": b["n"], "Hora": time.strftime("%H:%M:%S"),
               "SIB1": "no" if b["failed"].get("SIB1") else "OK",
               "SIB2": "no" if b["failed"].get("SIB2") else "OK"}
        for k, v in b["kv"]:
            row[k] = v
        self.rows = [r for r in self.rows if r["#"] != row["#"]] + [row]
        self._emit({"t": "cell", "row": row})
        self._write_csv()

    def _write_csv(self):
        if not self.csv_path or not self.rows:
            return
        cols = []
        for r in self.rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        try:
            with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(self.rows)
        except OSError:
            pass

    # ---- consulta
    def poll(self, since, run):
        with self.lock:
            reset = run != self.run_id
            if reset:
                since = 0
            evs = self.events[since:since + 3000]
            return {
                "run": self.run_id, "reset": reset, "events": evs, "next": since + len(evs),
                "status": {
                    "state": self.state, "cmd": self.cmd, "started": self.started,
                    "exit_code": self.exit_code, "progress": self.progress,
                    "n_cells": len(self.rows),
                    "n_sib1": sum(1 for r in self.rows if r.get("SIB1") == "OK"),
                    "n_sib2": sum(1 for r in self.rows if r.get("SIB2") == "OK"),
                    "csv": str(self.csv_path) if self.csv_path else "",
                },
            }


RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    server_version = "CellSearchUI/1.0"

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
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/info":
            binp = os.path.expanduser(self.server.default_bin)
            return self._send(200, {"bands": band_info(), "default_bin": self.server.default_bin,
                                    "bin_exists": os.path.isfile(binp), "log_dir": str(LOG_DIR),
                                    "max_earfcn": MAX_EARFCN})
        if u.path == "/api/poll":
            since = int(q.get("since", ["0"])[0])
            run = int(q.get("run", ["-1"])[0])
            return self._send(200, RUNNER.poll(since, run))
        if u.path == "/api/usage":
            binp = os.path.expanduser(q.get("bin", [self.server.default_bin])[0].strip())
            if not (os.path.isfile(binp) and os.access(binp, os.X_OK)):
                return self._send(200, {"text": "No existe o no es ejecutable: %s" % binp})
            try:
                r = subprocess.run([binp], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
                return self._send(200, {"text": (r.stdout + r.stderr).strip()})
            except Exception as e:  # noqa
                return self._send(200, {"text": "Error: %s" % e})
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
        if path == "/api/start":
            try:
                return self._send(200, RUNNER.start(body))
            except KeyError as e:
                return self._send(200, {"ok": False, "error": "Falta el campo: %s" % e})
            except (ValueError, TypeError) as e:
                return self._send(200, {"ok": False, "error": str(e)})
            except OSError as e:
                return self._send(200, {"ok": False, "error": "No se pudo ejecutar: %s" % e})
        if path == "/api/stop":
            return self._send(200, {"ok": RUNNER.stop()})
        return self._send(404, {"error": "no encontrado"})


def open_browser(url):
    for name in ("brave-browser", "brave", "brave-browser-stable"):
        exe = shutil.which(name)
        if exe:
            subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return
    webbrowser.open(url)


def main():
    global DEFAULT_BIN
    ap = argparse.ArgumentParser(description="Interfaz web para cell_search_plmn")
    ap.add_argument("--bin", default=DEFAULT_BIN, help="ruta del binario cell_search_plmn")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    DEFAULT_BIN = args.bin

    srv = None
    for port in range(args.port, args.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("No hay puertos libres cerca de %d" % args.port)
    srv.default_bin = args.bin
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("Interfaz lista en %s   (Ctrl+C para cerrar)" % url)
    print("Logs y CSV se guardan en %s" % LOG_DIR)
    if not args.no_browser:
        threading.Timer(0.6, open_browser, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNER.stop()
        srv.server_close()


HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buscador de celdas LTE</title>
<style>
:root{--bg:#030604;--panel:#09110c;--panel2:#0f1a13;--line:#1b2d21;--text:#e2f2e6;--muted:#7d9a85;
--accent:#22d37a;--ok:#8be9a8;--warn:#f5b94a;--err:#ff6b6b;--sat:#c792ff;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:10px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
header .sub{color:var(--muted);font-size:12px}
.pill{margin-left:auto;display:flex;align-items:center;gap:8px;padding:4px 12px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);font-size:12.5px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.pill.running .dot{background:var(--ok);box-shadow:0 0 0 0 rgba(139,233,168,.6);animation:pulse 1.4s infinite}
.pill.stopping .dot{background:var(--warn)}
.pill.error .dot{background:var(--err)}
.pill.finished .dot{background:var(--accent)}
@keyframes pulse{70%{box-shadow:0 0 0 8px rgba(139,233,168,0)}100%{box-shadow:0 0 0 0 rgba(139,233,168,0)}}
main{display:grid;grid-template-columns:350px minmax(0,1fr);gap:14px;padding:14px 18px;height:calc(100% - 53px)}
aside,.col{min-height:0;min-width:0}
aside{overflow:auto;padding-right:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);font-weight:650}
label{display:block;font-size:12.5px;color:var(--muted);margin:9px 0 3px}
.hint{font-size:11.8px;color:var(--muted);margin:3px 0 0}
.hint b{color:var(--text);font-weight:600}
input,select{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 9px;font:inherit}
input:focus,select:focus{outline:2px solid rgba(34,211,122,.45);border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.chk{display:flex;align-items:center;gap:8px;margin:8px 0 0;color:var(--text);font-size:13px}
.chk input{width:auto}
button{font:inherit;cursor:pointer;border-radius:7px;border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:7px 12px}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
.btn-go{background:var(--accent);border-color:var(--accent);color:#02130a;font-weight:650;flex:1}
.btn-stop{background:transparent;border-color:var(--err);color:var(--err);flex:1}
.btnrow{display:flex;gap:10px;margin-top:12px}
.preset{font-size:12px;padding:3px 9px;margin-top:8px}
details{margin-top:10px}
summary{cursor:pointer;color:var(--muted);font-size:12.5px}
code.cmd{display:block;white-space:pre-wrap;word-break:break-all;font:12px/1.5 var(--mono);background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:#b6d6c0}
.err{color:var(--err);font-size:12.5px;margin-top:6px;min-height:1em}
.col{display:flex;flex-direction:column;gap:12px;overflow-y:auto}
.col>*{flex:none}
.status{display:grid;grid-template-columns:1fr auto;gap:6px 18px;align-items:center}
.now{font-size:15px;font-weight:600}
.now small{color:var(--muted);font-weight:400}
.bar{height:8px;border-radius:6px;background:var(--bg);border:1px solid var(--line);overflow:hidden;grid-column:1/-1}
.bar>div{height:100%;width:0;background:linear-gradient(90deg,var(--accent),#8ff0b8);transition:width .3s}
.stats{display:flex;gap:18px;color:var(--muted);font-size:12.5px;grid-column:1/-1;flex-wrap:wrap}
.stats b{color:var(--text);font-variant-numeric:tabular-nums}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;display:flex;flex-direction:column;min-height:0}
.panel .bar2{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line)}
.panel .bar2 h2{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);font-weight:650}
.panel .bar2 .sp{flex:1}
.panel .bar2 input[type=text],.panel .bar2 input[type=search]{width:170px;padding:4px 8px;font-size:12.5px}
.panel .bar2 button{padding:3px 9px;font-size:12px}
.tbl{overflow:auto;flex:1;min-height:0}
table{border-collapse:collapse;width:max-content;min-width:100%;font-size:12.8px}
th,td{padding:6px 11px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}
th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;z-index:1}
td{font-variant-numeric:tabular-nums}
tr.sat td{background:rgba(199,146,255,.10)}
tr.unk td{background:rgba(245,185,74,.09)}
tr.new td{animation:flash 1.6s ease-out}
@keyframes flash{from{background:rgba(34,211,122,.35)}to{background:transparent}}
.badge{display:inline-block;padding:0 7px;border-radius:99px;font-size:11.5px;font-weight:600}
.badge.ok{background:rgba(139,233,168,.15);color:var(--ok)}
.badge.no{background:rgba(255,107,107,.13);color:var(--err)}
.empty{color:var(--muted);padding:26px;text-align:center}
.log{overflow:auto;flex:1;min-height:0;font:12.3px/1.5 var(--mono);padding:8px 12px}
.log div{white-space:pre-wrap;word-break:break-all}
.log .prog{color:#7f8b9a}
.log .ok{color:var(--ok)}
.log .no{color:#6f7a89}
.log .det{color:var(--accent);font-weight:600}
.log .err{color:var(--err)}
.log .head{color:var(--warn)}
.log .cmd{color:var(--sat)}
.log .hide{display:none}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:10px;max-width:760px;width:90%}
dialog pre{font:12.5px/1.5 var(--mono);white-space:pre-wrap;max-height:60vh;overflow:auto}
dialog::backdrop{background:rgba(0,0,0,.6)}
@media(max-width:980px){main{grid-template-columns:1fr;height:auto}aside,.col{overflow:visible}.panel{min-height:300px}}

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
  <h1>Buscador de celdas LTE</h1><span class="sub">cell_search_plmn · srsRAN_4G</span>
  <div class="pill" id="pill"><span class="dot"></span><span id="pilltxt">Inactivo</span></div>
</header>
<main>
<aside>
  <div class="card">
    <h2>Configuración</h2>
    <label for="band">Banda LTE</label>
    <select id="band"></select>
    <div class="hint" id="bandhint"></div>
    <button class="preset" id="preset" type="button">Entel D2C · banda 2 · EARFCN 1175–1176</button>

    <div class="row2">
      <div><label for="start">EARFCN inicio</label><input id="start" type="number" step="1"><div class="hint" id="fstart"></div></div>
      <div><label for="end">EARFCN fin</label><input id="end" type="number" step="1"><div class="hint" id="fend"></div></div>
    </div>

    <div class="row2">
      <div><label for="gain">Ganancia RX (dB) <small>-g</small></label><input id="gain" type="number" step="0.5" min="0" max="49"></div>
      <div><label for="n">Frames PSS <small>-n</small></label><input id="n" type="number" step="1" min="1"></div>
    </div>
    <div class="hint"><b>Ganancia:</b> amplificación del receptor. Más ganancia = ves señales débiles, pero puede saturar si hay señales fuertes. <b>Frames (-n):</b> cuántos tramos de 5 ms escucha en cada frecuencia buscando la señal de sincronismo (PSS) antes de rendirse. 100 ≈ 0,5 s por frecuencia; más = más sensible pero más lento.</div>

    <div class="row2">
      <div><label for="off">Barrido ± kHz <small>-o</small></label><input id="off" type="number" step="1" min="0"></div>
      <div><label for="step">Paso kHz <small>-k</small></label><input id="step" type="number" step="1" min="1"></div>
    </div>
    <div class="hint"><b>-o / -k:</b> cada EARFCN se prueba también desplazando la frecuencia central ±o kHz en pasos de k kHz. Sirve para captar señales con Doppler (satélites) o con error de reloj. <b>0 = desactivado</b> (solo la frecuencia exacta).</div>

    <label for="rfargs">Dispositivo RF <small>-a</small></label>
    <input id="rfargs" type="text" placeholder="type=x300,addr=192.168.10.2">
    <div class="hint"><b>-a:</b> argumentos de UHD para encontrar tu USRP: el tipo (<code>x300</code>) y su IP (<code>192.168.10.2</code>).</div>

    <details>
      <summary>Avanzado</summary>
      <div class="row2">
        <div><label for="mib">Intentos MIB <small>-m</small></label><input id="mib" type="number" min="1" placeholder="15"></div>
        <div><label for="sib1">Intentos SIB1 <small>-r</small></label><input id="sib1" type="number" min="1" placeholder="30"></div>
      </div>
      <label for="psr">PSR mínimo <small>-p</small></label><input id="psr" type="number" step="0.1" placeholder="2.0">
      <div class="hint">Vacío = no se pasa la opción (usa el valor por defecto del programa). Requieren el parche v2.</div>
      <label class="chk"><input id="json" type="checkbox"> Volcar SIB2 completo en JSON (-j)</label>
      <label class="chk"><input id="verbose" type="checkbox"> Verbose (-v)</label>
      <label for="extra">Opciones extra</label>
      <input id="extra" type="text" placeholder="p. ej. -v">
      <div class="hint">Se agregan tal cual al comando. Útil para opciones nuevas que agregues al programa.</div>
      <label for="bin">Programa</label>
      <input id="bin" type="text">
      <button type="button" id="usage" style="margin-top:8px">Ver opciones del programa</button>
    </details>
  </div>

  <div class="card">
    <h2>Comando</h2>
    <code class="cmd" id="cmdprev"></code>
    <div class="hint" id="est"></div>
    <div class="err" id="err"></div>
    <div class="btnrow">
      <button class="btn-go" id="go">▶ Iniciar búsqueda</button>
      <button class="btn-stop" id="stop" disabled>■ Detener</button>
    </div>
    <div class="hint" id="saved" style="margin-top:8px"></div>
  </div>
</aside>

<section class="col">
  <div class="card" style="margin:0">
    <div class="status">
      <div class="now" id="now">Sin búsqueda en curso</div>
      <div class="hint" id="clock">00:00</div>
      <div class="bar"><div id="barfill"></div></div>
      <div class="stats">
        <span>Celdas con MIB: <b id="s_cells">0</b></span>
        <span>SIB1 OK: <b id="s_sib1">0</b></span>
        <span>SIB2 OK: <b id="s_sib2">0</b></span>
        <span id="s_prog"></span>
      </div>
    </div>
  </div>

  <div class="panel" style="height:470px">
    <div class="bar2"><h2>Celdas decodificadas</h2><span class="sp"></span>
      <button id="csv" type="button">Exportar CSV</button>
      <button id="clrtbl" type="button">Limpiar</button></div>
    <div class="tbl" id="tbl"><div class="empty">Aún no hay celdas. Aparecerán aquí apenas se decodifiquen.</div></div>
  </div>

  <div class="panel" style="height:430px">
    <div class="bar2"><h2>Registro</h2><span class="sp"></span>
      <input type="search" id="filter" placeholder="Filtrar…">
      <label class="chk" style="margin:0"><input type="checkbox" id="onlyimp"> sin líneas de barrido</label>
      <label class="chk" style="margin:0"><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
      <button id="dllog" type="button">Guardar</button>
      <button id="clrlog" type="button">Limpiar</button></div>
    <div class="log" id="log"></div>
  </div>
</section>
</main>

<dialog id="dlg"><h3 style="margin-top:0">Opciones del programa</h3><pre id="dlgtxt"></pre>
<button onclick="document.getElementById('dlg').close()">Cerrar</button></dialog>

<script>
const ID_INI='start', ID_FIN='end', ID_GAN='gain';
const BANDA_ACTUAL = () => INFO && bandOf(document.getElementById('band').value);

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
const LS = 'csui.v1';
let INFO = null, since = 0, runId = -1, rows = [], status = {}, lastRowN = null, serverOk = true;
const FIELDS = ['band','start','end','gain','n','off','step','rfargs','mib','sib1','psr','extra','bin'];
const CHECKS = ['json','verbose'];
const DEFAULTS = {band:'2', start:'1175', end:'1176', gain:'30', n:'100', off:'10', step:'10',
                  rfargs:'type=x300,addr=192.168.10.2', mib:'', sib1:'', psr:'', extra:'', bin:''};

async function api(path, body){
  const opt = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  return r.json();
}
const bandOf = b => INFO.bands.find(x => String(x.band) === String(b));
const earfcnToMHz = (b, n) => { const x = bandOf(b); return x ? x.f_low + 0.1 * (n - x.n_offs) : NaN; };

function getCfg(){
  const c = {};
  FIELDS.forEach(f => c[f] = $(f).value.trim());
  CHECKS.forEach(f => c[f] = $(f).checked);
  return c;
}
function saveCfg(){ try{ localStorage.setItem(LS, JSON.stringify(getCfg())); }catch(e){} }
function loadCfg(){
  let c = {}; try{ c = JSON.parse(localStorage.getItem(LS) || '{}'); }catch(e){}
  c = Object.assign({}, DEFAULTS, c);
  if(!c.bin) c.bin = INFO.default_bin;
  FIELDS.forEach(f => $(f).value = c[f] ?? '');
  CHECKS.forEach(f => $(f).checked = !!c[f]);
}

function q(s){ return /[^\w@%+=:,./-]/.test(s) ? "'" + s.replace(/'/g, "'\\''") + "'" : s; }
function buildArgs(c){
  const a = [c.bin || INFO.default_bin, '-b', c.band];
  if(c.start !== '') a.push('-s', c.start);
  if(c.end !== '') a.push('-e', c.end);
  if(c.gain !== '') a.push('-g', c.gain);
  if(c.n !== '') a.push('-n', c.n);
  if(c.off !== '' && parseFloat(c.off) > 0){ a.push('-o', c.off); if(c.step !== '' && parseFloat(c.step) > 0) a.push('-k', c.step); }
  if(c.mib !== '') a.push('-m', c.mib);
  if(c.sib1 !== '') a.push('-r', c.sib1);
  if(c.psr !== '') a.push('-p', c.psr);
  if(c.json) a.push('-j');
  if(c.verbose) a.push('-v');
  if(c.rfargs !== '') a.push('-a', c.rfargs);
  if(c.extra !== '') a.push(c.extra);
  return a;
}

function validate(c){
  const x = bandOf(c.band); if(!x) return 'Elige una banda.';
  const s = parseInt(c.start), e = parseInt(c.end);
  if(isNaN(s) || isNaN(e)) return 'Indica EARFCN de inicio y fin.';
  if(s < x.n_min || e > x.n_max) return `EARFCN fuera de la banda ${x.band} (${x.n_min}–${x.n_max}): revisa el aviso junto al campo.`;
  const g = parseFloat(c.gain); if(c.gain !== '' && (isNaN(g) || g < 0 || g > 49)) return 'Ganancia fuera de rango (0–49 dB).';
  if(s > e) return 'El EARFCN de inicio debe ser menor o igual que el de fin.';
  if(e - s + 1 > INFO.max_earfcn) return `Máximo ${INFO.max_earfcn} EARFCN por búsqueda (límite del programa).`;
  if(!c.rfargs) return 'Falta el dispositivo RF (-a).';
  return '';
}

function refresh(){
  const c = getCfg(), x = bandOf(c.band);
  if(x){
    $('bandhint').innerHTML = `Límites EARFCN: <b>${x.n_min} – ${x.n_max}</b> · ${x.f_min.toFixed(1)} – ${x.f_max.toFixed(1)} MHz`;
    $('start').min = $('end').min = x.n_min; $('start').max = $('end').max = x.n_max;
    const f = v => isNaN(parseInt(v)) ? '' : earfcnToMHz(c.band, parseInt(v)).toFixed(1) + ' MHz';
    $('fstart').textContent = f(c.start); $('fend').textContent = f(c.end);
  }
  $('cmdprev').textContent = buildArgs(c).map(q).join(' ');
  const err = validate(c);
  $('err').textContent = err;
  const s = parseInt(c.start), e = parseInt(c.end);
  if(!err){
    const off = parseFloat(c.off) || 0, step = parseFloat(c.step) || 0;
    const noff = (off > 0 && step > 0) ? Math.floor(2 * off / step) + 1 : 1;
    const pts = (e - s + 1) * noff, secs = pts * (parseInt(c.n) || 100) * 0.005;
    $('est').textContent = `${e - s + 1} EARFCN × ${noff} offset(s) = ${pts} puntos · hasta ~${fmtDur(secs)} solo escaneando PSS (más el tiempo de decodificar).`;
  } else $('est').textContent = '';
  const running = status.state === 'running' || status.state === 'stopping';
  $('go').disabled = !!err || running;
  $('stop').disabled = status.state !== 'running';
  saveCfg();
}
function fmtDur(s){ s = Math.round(s); if(s < 90) return s + ' s'; const m = Math.round(s / 60); return m < 90 ? m + ' min' : (m / 60).toFixed(1) + ' h'; }

function onBandChange(){
  const x = bandOf($('band').value);
  if(x){ $('start').value = x.n_min; $('end').value = x.n_max; }
  revisarRangos(false); refresh();
}

// ------------------------------------------------------------- log
function classify(l){
  if(/^\$ /.test(l)) return 'cmd';
  if(/^\[\s*\d+\/\d+\]: EARFCN/.test(l)) return 'prog';
  if(/^\[DETECCION\]/.test(l)) return 'det';
  if(/error|fall[oó]|No se pudo|no encontrado|Error/.test(l) && !/OK/.test(l)) return 'err';
  if(/\.\.\. OK|DECODIFICADO|CELDA REGISTRADA|MIB OK/.test(l)) return 'ok';
  if(/\.\.\. no\b|no decodificado|no se pudo/i.test(l)) return 'no';
  if(/^={3,}|FIN DEL ESCANEO|RESUMEN|Barrido|Interrupcion/.test(l)) return 'head';
  return '';
}
function addLog(lines){
  const box = $('log'), fl = $('filter').value.toLowerCase(), imp = $('onlyimp').checked;
  const frag = document.createDocumentFragment();
  for(const l of lines){
    const d = document.createElement('div'); const cls = classify(l);
    d.textContent = l; if(cls) d.className = cls;
    if((fl && !l.toLowerCase().includes(fl)) || (imp && cls === 'prog')) d.classList.add('hide');
    frag.appendChild(d);
  }
  box.appendChild(frag);
  while(box.childElementCount > 6000) box.removeChild(box.firstChild);
  if($('autoscroll').checked) box.scrollTop = box.scrollHeight;
}
function reFilter(){
  const fl = $('filter').value.toLowerCase(), imp = $('onlyimp').checked;
  for(const d of $('log').children){
    const hide = (fl && !d.textContent.toLowerCase().includes(fl)) || (imp && d.classList.contains('prog'));
    d.classList.toggle('hide', !!hide);
  }
}

// ------------------------------------------------------------- tabla
function renderTable(){
  const t = $('tbl');
  if(!rows.length){ t.innerHTML = '<div class="empty">Aún no hay celdas. Aparecerán aquí apenas se decodifiquen.</div>'; return; }
  const cols = []; rows.forEach(r => Object.keys(r).forEach(k => { if(!cols.includes(k)) cols.push(k); }));
  const tbl = document.createElement('table'), thead = tbl.createTHead().insertRow();
  cols.forEach(c => { const th = document.createElement('th'); th.textContent = c; thead.appendChild(th); });
  const tb = tbl.createTBody();
  rows.forEach(r => {
    const tr = tb.insertRow(); const op = (r['Operador'] || '');
    if(/SATELITAL/.test(op)) tr.className = 'sat'; else if(/DESCONOCIDO/.test(op)) tr.className = 'unk';
    if(r['#'] === lastRowN) tr.classList.add('new');
    cols.forEach(c => {
      const td = tr.insertCell(); const v = r[c] ?? '';
      if(c === 'SIB1' || c === 'SIB2'){ const b = document.createElement('span'); b.className = 'badge ' + (v === 'OK' ? 'ok' : 'no'); b.textContent = v; td.appendChild(b); }
      else td.textContent = v;
    });
  });
  const keep = t.scrollTop; t.replaceChildren(tbl); t.scrollTop = keep;
}
function exportCsv(){
  if(!rows.length) return;
  const cols = []; rows.forEach(r => Object.keys(r).forEach(k => { if(!cols.includes(k)) cols.push(k); }));
  const esc = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
  const txt = '﻿' + [cols.map(esc).join(',')].concat(rows.map(r => cols.map(c => esc(r[c])).join(','))).join('\n');
  download(txt, 'celdas_' + stamp() + '.csv', 'text/csv');
}
function stamp(){ const d = new Date(), p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`; }
function download(txt, name, type){
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([txt], {type})); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

// ------------------------------------------------------------- estado
const LABEL = {idle:'Inactivo', running:'Buscando…', stopping:'Deteniendo…', finished:'Finalizado', error:'Terminó con error'};
function renderStatus(){
  const s = status, p = s.progress || {};
  const pill = $('pill'); pill.className = 'pill ' + (serverOk ? (s.state || 'idle') : 'error');
  let t = serverOk ? (LABEL[s.state] || 'Inactivo') : 'Sin conexión con el servidor';
  if(serverOk && (s.state === 'finished' || s.state === 'error') && s.exit_code !== null && s.exit_code !== undefined) t += ` (código ${s.exit_code})`;
  $('pilltxt').textContent = t;
  if(p.earfcn !== undefined && (s.state === 'running' || s.state === 'stopping')){
    $('now').innerHTML = `EARFCN <b>${p.earfcn}</b> · ${p.freq.toFixed(1)} MHz <small>· offset ${p.offset >= 0 ? '+' : ''}${p.offset.toFixed(1)} kHz</small>`;
  } else if(s.state === 'finished' || s.state === 'error') $('now').textContent = 'Búsqueda terminada';
  else if(!s.state || s.state === 'idle') $('now').textContent = 'Sin búsqueda en curso';
  const pct = p.total ? Math.min(100, 100 * p.cur / p.total) : (s.state === 'finished' ? 100 : 0);
  $('barfill').style.width = pct + '%';
  $('s_prog').textContent = p.total ? `EARFCN ${p.cur} de ${p.total}` : '';
  $('s_cells').textContent = s.n_cells || 0; $('s_sib1').textContent = s.n_sib1 || 0; $('s_sib2').textContent = s.n_sib2 || 0;
  $('saved').textContent = s.csv ? 'CSV en vivo: ' + s.csv : '';
  const running = s.state === 'running' || s.state === 'stopping';
  $('go').disabled = !!validate(getCfg()) || running; $('stop').disabled = s.state !== 'running';
}
setInterval(() => {
  const s = status; if(!s.started || !(s.state === 'running' || s.state === 'stopping')) return;
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - s.started));
  $('clock').textContent = String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
}, 500);

async function poll(){
  try{
    const d = await api(`/api/poll?since=${since}&run=${runId}`);
    serverOk = true;
    if(d.reset){ $('log').replaceChildren(); rows = []; lastRowN = null; renderTable(); runId = d.run; }
    const lines = []; let cellChanged = false;
    for(const ev of d.events){
      if(ev.t === 'log') lines.push(ev.line);
      else if(ev.t === 'cell'){ rows = rows.filter(r => r['#'] !== ev.row['#']).concat([ev.row]); lastRowN = ev.row['#']; cellChanged = true; }
    }
    if(lines.length) addLog(lines);
    if(cellChanged) renderTable();
    since = d.next; status = d.status; renderStatus();
  }catch(e){ serverOk = false; renderStatus(); }
}

async function start(){
  const c = getCfg(), err = validate(c); if(err){ $('err').textContent = err; return; }
  const r = await api('/api/start', c);
  if(!r.ok){ $('err').textContent = r.error || 'No se pudo iniciar.'; return; }
  $('log').replaceChildren(); rows = []; lastRowN = null; renderTable(); since = 0; runId = r.run; $('clock').textContent = '00:00';
  poll();
}

(async function init(){
  INFO = await api('/api/info');
  $('band').innerHTML = INFO.bands.map(b => `<option value="${b.band}">Banda ${b.band} (${b.f_min.toFixed(0)}–${b.f_max.toFixed(0)} MHz)</option>`).join('');
  loadCfg(); if(!bandOf($('band').value)) $('band').value = INFO.bands[0].band;
  if(!INFO.bin_exists) $('err').textContent = 'No encuentro el programa en la ruta por defecto. Revísala en Avanzado → Programa.';
  [...FIELDS.filter(f => f !== 'band'), ...CHECKS].forEach(f => $(f).addEventListener('input', refresh));
  $('band').addEventListener('change', onBandChange);
  activarRangos(); ['start','end','gain'].forEach(id => $(id).addEventListener('rangochange', refresh));
  revisarRangos(false);
  $('preset').onclick = () => { $('band').value = '2'; $('start').value = 1175; $('end').value = 1176; revisarRangos(false); refresh(); };
  $('go').onclick = start;
  $('stop').onclick = () => api('/api/stop', {});
  $('csv').onclick = exportCsv;
  $('clrtbl').onclick = () => { rows = []; lastRowN = null; renderTable(); };
  $('clrlog').onclick = () => $('log').replaceChildren();
  $('dllog').onclick = () => download([...$('log').children].map(d => d.textContent).join('\n'), 'log_' + stamp() + '.txt', 'text/plain');
  $('filter').addEventListener('input', reFilter); $('onlyimp').addEventListener('change', reFilter);
  $('usage').onclick = async () => { const r = await api('/api/usage?bin=' + encodeURIComponent($('bin').value)); $('dlgtxt').textContent = r.text; $('dlg').showModal(); };
  refresh(); renderStatus(); poll(); setInterval(poll, 400);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
