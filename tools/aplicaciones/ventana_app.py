#!/usr/bin/env python3
"""Ventana nativa (GTK 3 + WebKitGTK) para las herramientas LTE.

Arranca el servidor local de la herramienta (si no está ya corriendo), lo
muestra en una ventana propia con su nombre y su logo, sin depender de ningún
navegador, y al cerrar la ventana apaga el servidor.

Uso: ventana_app.py buscador | grabador | analizador
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

TOOLS = Path(__file__).resolve().parent.parent
ICONOS = Path(__file__).resolve().parent / "iconos"

APPS = {
    "buscador": ("Buscador LTE", TOOLS / "cell_search_ui" / "cell_search_ui.py", 8765),
    "grabador": ("Grabador IQ", TOOLS / "grabador_iq" / "grabador_ui.py", 8770),
    "analizador": ("Analizador IQ", TOOLS / "analizador_iq" / "analizador_ui.py", 8780),
}

DATOS = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "herramientas-lte"


def responde(url):
    try:
        with urllib.request.urlopen(url, timeout=1):
            return True
    except Exception:
        return False


def carpeta_descargas():
    try:
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    except Exception:
        d = None
    return d or str(Path.home())


class App(Gtk.Application):
    def __init__(self, clave):
        self.clave = clave
        self.nombre, self.script, self.puerto = APPS[clave]
        # El id de la aplicación coincide con el archivo lte-<clave>.desktop:
        # así GNOME muestra en la barra el logo y el nombre de esta app.
        super().__init__(application_id=f"lte.{clave}")
        GLib.set_prgname(f"lte-{clave}")
        GLib.set_application_name(self.nombre)
        self.servidor = None
        self.ventana = None
        self.url = f"http://127.0.0.1:{self.puerto}/"

    # ---------- servidor ----------
    def arrancar_servidor(self):
        if responde(self.url):
            return True
        DATOS.mkdir(parents=True, exist_ok=True)
        log = open(DATOS / f"{self.clave}.log", "w")
        env = dict(os.environ)
        env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "")
        self.servidor = subprocess.Popen(
            [sys.executable, str(self.script), "--port", str(self.puerto), "--no-browser"],
            cwd=self.script.parent, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env=env)
        for _ in range(60):
            if responde(self.url):
                return True
            if self.servidor.poll() is not None:
                break
            time.sleep(0.2)
        # quizá tomó otro puerto porque el normal estaba ocupado
        try:
            import re
            m = re.search(r"http://127\.0\.0\.1:\d+/", (DATOS / f"{self.clave}.log").read_text())
            if m and responde(m.group(0)):
                self.url = m.group(0)
                return True
        except Exception:
            pass
        return False

    def apagar_servidor(self):
        p = self.servidor
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # ---------- ventana ----------
    def do_activate(self):
        if self.ventana:
            self.ventana.present()
            return
        self.ventana = Gtk.ApplicationWindow(application=self, title=self.nombre)
        self.ventana.set_default_size(1400, 900)
        self.ventana.set_icon_from_file(str(ICONOS / f"{self.clave}.svg"))
        self.ventana.maximize()

        if not self.arrancar_servidor():
            self.error(f"No se pudo iniciar {self.nombre}.\n\nRevisa el registro:\n{DATOS / (self.clave + '.log')}")
            return

        perfil = DATOS / f"ventana-{self.clave}"
        gestor = WebKit2.WebsiteDataManager(
            base_data_directory=str(perfil / "datos"),
            base_cache_directory=str(perfil / "cache"))
        contexto = WebKit2.WebContext.new_with_website_data_manager(gestor)
        contexto.connect("download-started", self.descarga)

        vista = WebKit2.WebView.new_with_context(contexto)
        ajustes = vista.get_settings()
        ajustes.set_enable_developer_extras(False)
        ajustes.set_javascript_can_open_windows_automatically(False)
        vista.connect("context-menu", lambda *a: True)  # sin menú "Recargar/Inspeccionar"
        vista.connect("decide-policy", self.politica)
        vista.connect("load-failed", self.fallo_carga)
        vista.load_uri(self.url)

        self.ventana.add(vista)
        self.ventana.show_all()

    def politica(self, vista, decision, tipo):
        # Enlaces externos (si los hubiera) se abren en el navegador del sistema.
        if tipo in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(("http://", "https://")) and not uri.startswith(self.url):
                Gtk.show_uri_on_window(self.ventana, uri, Gtk.get_current_event_time())
                decision.ignore()
                return True
        return False

    def fallo_carga(self, vista, evento, uri, err):
        # el servidor puede tardar un poco más: reintentar
        GLib.timeout_add(800, lambda: (vista.load_uri(self.url), False)[1])
        return True

    def descarga(self, contexto, descarga):
        def decidir(d, nombre_sugerido):
            dlg = Gtk.FileChooserNative.new("Guardar archivo", self.ventana,
                                            Gtk.FileChooserAction.SAVE, "_Guardar", "_Cancelar")
            dlg.set_do_overwrite_confirmation(True)
            dlg.set_current_folder(carpeta_descargas())
            dlg.set_current_name(nombre_sugerido or "datos.csv")
            if dlg.run() == Gtk.ResponseType.ACCEPT:
                d.set_destination(dlg.get_uri())
            else:
                d.cancel()
            return True
        descarga.connect("decide-destination", decidir)

    def error(self, texto):
        dlg = Gtk.MessageDialog(transient_for=self.ventana, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text=texto)
        dlg.run()
        dlg.destroy()
        self.quit()

    def do_shutdown(self):
        self.apagar_servidor()
        Gtk.Application.do_shutdown(self)


def main():
    clave = sys.argv[1] if len(sys.argv) > 1 else ""
    if clave not in APPS:
        sys.exit("Uso: ventana_app.py buscador|grabador|analizador")
    app = App(clave)
    signal.signal(signal.SIGTERM, lambda *a: GLib.idle_add(app.quit))
    sys.exit(app.run([sys.argv[0]]))


if __name__ == "__main__":
    main()
