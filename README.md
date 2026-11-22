# LTE Buscador · Grabador · Analizador

Tres aplicaciones de escritorio para Ubuntu que buscan, graban y decodifican
señales LTE con un **USRP X300**. Se hicieron para estudiar la señal satelital
**Starlink Direct-to-Cell** (LTE en banda 2), pero sirven para cualquier celda LTE.

| Aplicación | Qué hace |
|---|---|
| **Buscador LTE** | Busca celdas LTE en vivo por EARFCN y muestra las celdas encontradas con su PCI, PLMN y TAC (`cell_search_plmn`). |
| **Grabador IQ** | Graba muestras IQ por bloques de 20 MHz, con espectro y cascada en vivo. Solo recibe: nunca transmite. |
| **Analizador IQ** | Busca la PSS en toda la grabación, con barrido de Doppler, y decodifica MIB, SIB1 y SIB2. |

Cada una se abre desde el menú de aplicaciones, en su propia ventana y con su logo.

> Basado en [srsRAN_4G](https://github.com/srsran/srsRAN_4G) de Software Radio
> Systems (SRS). **No es la versión oficial de srsRAN.** El README original está en
> [README.srsRAN.md](README.srsRAN.md).

## Requisitos

- Ubuntu 26.04 (probado). Puede funcionar en otras versiones recientes.
- USRP X300/X310 conectado por 1 GbE. Para las grabaciones grandes conviene un disco SSD.
- Conexión a internet durante la instalación.

## Instalación

```bash
git clone https://github.com/FaZeJose/LTE-Buscador-Grabador-Analizador.git
cd LTE-Buscador-Grabador-Analizador
./install.sh
```

El instalador pide la contraseña de Ubuntu cuando la necesita y hace lo siguiente:

1. Instala las dependencias con `apt`, entre ellas UHD, FFTW, mbedTLS, Boost, GTK y WebKit.
2. Compila solo lo necesario de srsRAN: `cell_search_plmn` y `analizar_celda_iq`. Tarda entre 10 y 30 minutos.
3. Compila el grabador (`rx_iq_grabador`) y el detector de PSS (`libpss_det.so`).
4. Descarga las imágenes FPGA del X300.
5. Amplía los búferes de red del sistema para recibir sin pérdidas.
6. Agrega **Buscador LTE**, **Grabador IQ** y **Analizador IQ** al menú.

Si usas un UHD compilado a mano, indícalo así: `UHD_PREFIX=/ruta/a/uhd-install ./install.sh`.

Para desinstalar: `./uninstall.sh`. No borra tus grabaciones.

## Conectar el X300

El X300 usa la IP `192.168.10.2` en su puerto 1 GbE. La tarjeta de red de la PC
tiene que estar en la misma red:

```bash
ip link                                   # mira el nombre de tu puerto, por ejemplo eno1
nmcli con add type ethernet ifname eno1 con-name usrp \
      ipv4.method manual ipv4.addresses 192.168.10.1/24 ipv6.method ignore
nmcli con up usrp
uhd_find_devices --args addr=192.168.10.2  # debe aparecer el X300
```

Si UHD avisa que la imagen FPGA no es compatible, actualízala con:
`uhd_image_loader --args type=x300,addr=192.168.10.2,fpga=HG`

## Uso desde la terminal

Las interfaces también se pueden abrir sin el menú:

```bash
python3 tools/aplicaciones/ventana_app.py buscador     # o grabador / analizador
```

Cada herramienta tiene más detalles en su carpeta: [`tools/grabador_iq`](tools/grabador_iq/LEEME.md)
y [`tools/analizador_iq`](tools/analizador_iq/LEEME.md).

## Dónde se guardan los datos

| Qué | Dónde |
|---|---|
| Grabaciones IQ | `~/capturas_iq` |
| Registros y CSV del buscador | `~/cell_search_logs` |
| Preferencias y registros de las ventanas | `~/.local/share/herramientas-lte` |

## Cambios respecto a srsRAN_4G

- `lib/examples/cell_search_plmn.cc`: buscador de celdas con decodificación del PLMN.
- `lib/examples/analizar_celda_iq.cc`: decodificador de MIB, SIB1 y SIB2 sobre archivos IQ.
- `lib/src/phy/rf/rf_utils.c` y `rf_utils.h`, `lib/examples/cell_search.c`: `rf_mib_decoder` también devuelve el SFN decodificado del MIB.
- `CMakeLists.txt`: C++17 y los dos programas nuevos.
- `tools/`: las tres aplicaciones, sus logos, `install.sh` y `uninstall.sh`.

## Aviso legal

Estas herramientas solo **reciben**: analizan la información de sistema que las
celdas LTE difunden públicamente (MIB y SIB). No transmiten ni intentan acceder a
datos de usuarios. Cada usuario es responsable de cumplir las normas de
radiocomunicaciones de su país.

## Licencia

Igual que srsRAN_4G, este proyecto se distribuye bajo la licencia
**GNU AGPL v3** (ver [LICENSE](LICENSE) y [COPYRIGHTS.txt](COPYRIGHTS.txt)).
Las modificaciones y las herramientas de `tools/` son © 2026 Jose Fabricio Sarcco Yana,
bajo la misma licencia.
