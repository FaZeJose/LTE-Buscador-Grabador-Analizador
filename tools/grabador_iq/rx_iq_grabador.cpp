// rx_iq_grabador.cpp
// Grabador IQ crudo SOLO RECEPCION para USRP (X300), usando la API C++ de UHD.
// No se crea nunca un tx_streamer: el programa no puede transmitir.
//
// Escribe muestras complejas intercaladas I,Q en el formato pedido:
//   sc16 -> int16 I, int16 Q (4 bytes/muestra)
//   sc8  -> int8  I, int8  Q (2 bytes/muestra), con sc8 tambien por el cable
//
// Al terminar imprime en stdout una linea "RESUMEN_JSON {...}" con
// estadisticas (overflows, muestras, hora de inicio) que lee el script Python.

#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/utils/safe_main.hpp>
#include <uhd/utils/thread.hpp>
#include <boost/program_options.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdio>
#include <cstdint>
#include <ctime>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <thread>
#include <vector>

namespace po = boost::program_options;

static std::atomic<bool> g_parar{false};
static void manejador_senal(int) { g_parar = true; }

static std::string hora_utc_iso(std::chrono::system_clock::time_point tp)
{
    auto t  = std::chrono::system_clock::to_time_t(tp);
    auto us = std::chrono::duration_cast<std::chrono::microseconds>(tp.time_since_epoch()).count()
              % 1000000;
    std::tm tm{};
    gmtime_r(&t, &tm);
    std::ostringstream os;
    os << std::put_time(&tm, "%Y-%m-%dT%H:%M:%S") << "." << std::setw(6) << std::setfill('0')
       << us << "Z";
    return os.str();
}

// Cola de bloques para separar la recepcion (tiempo real) de la escritura a disco.
struct Cola
{
    std::mutex m;
    std::condition_variable cv;
    std::deque<std::vector<char>> q;
    std::vector<std::vector<char>> libres;
    bool fin           = false;
    size_t max_en_cola = 0;
};

int UHD_SAFE_MAIN(int argc, char* argv[])
{
    std::string args, archivo, formato, antena, subdev, ref, snapshot;
    size_t snapshot_n;
    double freq, tasa, ganancia, duracion, bw;
    size_t canal;

    po::options_description desc("rx_iq_grabador (solo RX)");
    // clang-format off
    desc.add_options()
        ("help", "ayuda")
        ("args", po::value<std::string>(&args)->default_value("type=x300,addr=192.168.10.2"), "argumentos del dispositivo UHD")
        ("file", po::value<std::string>(&archivo)->required(), "archivo de salida .iq")
        ("freq", po::value<double>(&freq)->required(), "frecuencia central (Hz)")
        ("rate", po::value<double>(&tasa)->default_value(20e6), "tasa de muestreo (S/s)")
        ("gain", po::value<double>(&ganancia)->default_value(30), "ganancia RX (dB)")
        ("duration", po::value<double>(&duracion)->required(), "duracion (s)")
        ("format", po::value<std::string>(&formato)->default_value("sc16"), "sc16 | sc8")
        ("ant", po::value<std::string>(&antena)->default_value("RX2"), "antena (RX2 | TX/RX)")
        ("subdev", po::value<std::string>(&subdev)->default_value(""), "subdispositivo, p.ej. A:0 o B:0")
        ("channel", po::value<size_t>(&canal)->default_value(0), "canal RX")
        ("bw", po::value<double>(&bw)->default_value(0), "ancho de banda analogico (Hz), 0 = no tocar")
        ("ref", po::value<std::string>(&ref)->default_value("internal"), "referencia de reloj: internal|external|gpsdo")
        ("snapshot", po::value<std::string>(&snapshot)->default_value(""), "archivo donde dejar cada ~200 ms las ultimas muestras escritas (para ver el espectro)")
        ("snapshot-n", po::value<size_t>(&snapshot_n)->default_value(16384), "muestras por snapshot")
    ;
    // clang-format on
    po::variables_map vm;
    po::store(po::parse_command_line(argc, argv, desc), vm);
    if (vm.count("help")) {
        std::cout << desc << std::endl;
        return EXIT_SUCCESS;
    }
    po::notify(vm);

    if (formato != "sc16" && formato != "sc8") {
        std::cerr << "Formato no valido: " << formato << " (use sc16 o sc8)" << std::endl;
        return EXIT_FAILURE;
    }
    const size_t bytes_por_muestra = (formato == "sc16") ? 4 : 2;

    std::signal(SIGINT, manejador_senal);
    std::signal(SIGTERM, manejador_senal);
    uhd::set_thread_priority_safe();

    std::cerr << "Abriendo USRP: " << args << std::endl;
    auto usrp = uhd::usrp::multi_usrp::make(args);
    usrp->set_clock_source(ref);
    if (!subdev.empty())
        usrp->set_rx_subdev_spec(uhd::usrp::subdev_spec_t(subdev));

    usrp->set_rx_rate(tasa, canal);
    const double tasa_real = usrp->get_rx_rate(canal);
    usrp->set_rx_freq(uhd::tune_request_t(freq), canal);
    const double freq_real = usrp->get_rx_freq(canal);
    usrp->set_rx_gain(ganancia, canal);
    const double ganancia_real = usrp->get_rx_gain(canal);
    usrp->set_rx_antenna(antena, canal);
    if (bw > 0)
        usrp->set_rx_bandwidth(bw, canal);

    std::cerr << "Tasa real: " << tasa_real / 1e6 << " Msps, frecuencia real: " << freq_real / 1e6
              << " MHz, ganancia real: " << ganancia_real << " dB, antena: "
              << usrp->get_rx_antenna(canal) << std::endl;

    // Esperar enganche del LO
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    auto sensores = usrp->get_rx_sensor_names(canal);
    bool lo_ok    = true;
    if (std::find(sensores.begin(), sensores.end(), "lo_locked") != sensores.end()) {
        for (int i = 0; i < 20 && !usrp->get_rx_sensor("lo_locked", canal).to_bool(); i++)
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        lo_ok = usrp->get_rx_sensor("lo_locked", canal).to_bool();
        std::cerr << "LO enganchado: " << (lo_ok ? "si" : "NO") << std::endl;
    }

    uhd::stream_args_t sargs(formato, formato); // cpu = otw
    sargs.channels = {canal};
    auto rx        = usrp->get_rx_stream(sargs);
    const size_t spp = rx->get_max_num_samps();

    std::ofstream fout(archivo, std::ios::binary);
    if (!fout) {
        std::cerr << "No se puede abrir " << archivo << std::endl;
        return EXIT_FAILURE;
    }

    // Bloques de ~0.1 s
    const size_t muestras_bloque = static_cast<size_t>(tasa_real / 10);
    const size_t bytes_bloque    = muestras_bloque * bytes_por_muestra;
    const uint64_t total_muestras = static_cast<uint64_t>(duracion * tasa_real + 0.5);

    Cola cola;
    std::atomic<bool> error_disco{false};
    auto ultimo_snapshot = std::chrono::steady_clock::now() - std::chrono::seconds(1);
    std::thread escritor([&] {
        while (true) {
            std::vector<char> b;
            {
                std::unique_lock<std::mutex> lk(cola.m);
                cola.cv.wait(lk, [&] { return !cola.q.empty() || cola.fin; });
                if (cola.q.empty() && cola.fin)
                    break;
                b = std::move(cola.q.front());
                cola.q.pop_front();
            }
            fout.write(b.data(), b.size());
            if (!fout)
                error_disco = true;
            // Snapshot de las ultimas muestras escritas (escritura atomica con rename)
            auto ahora_s = std::chrono::steady_clock::now();
            if (!snapshot.empty() && ahora_s - ultimo_snapshot > std::chrono::milliseconds(200)) {
                ultimo_snapshot = ahora_s;
                size_t nb  = std::min(b.size(), snapshot_n * bytes_por_muestra);
                std::string tmp = snapshot + ".tmp";
                if (FILE* fs = std::fopen(tmp.c_str(), "wb")) {
                    std::fwrite(b.data() + b.size() - nb, 1, nb, fs);
                    std::fclose(fs);
                    std::rename(tmp.c_str(), snapshot.c_str());
                }
            }
            std::lock_guard<std::mutex> lk(cola.m);
            b.resize(bytes_bloque);
            cola.libres.push_back(std::move(b));
        }
    });

    auto obtener_buffer = [&]() {
        std::lock_guard<std::mutex> lk(cola.m);
        if (!cola.libres.empty()) {
            auto b = std::move(cola.libres.back());
            cola.libres.pop_back();
            return b;
        }
        return std::vector<char>(bytes_bloque);
    };

    uint64_t n_overflow = 0, n_fuera_secuencia = 0, n_timeout = 0, n_otros = 0;
    uint64_t recibidas = 0;
    bool primera = true;
    std::chrono::system_clock::time_point t_inicio;
    double t_usrp_inicio = 0;

    uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
    cmd.stream_now = false;
    usrp->set_time_now(uhd::time_spec_t(0.0));
    cmd.time_spec = uhd::time_spec_t(0.5);
    rx->issue_stream_cmd(cmd);

    uhd::rx_metadata_t md;
    std::vector<char> buf = obtener_buffer();
    size_t llenado        = 0;
    double timeout        = 1.5;
    auto ultimo_progreso  = std::chrono::steady_clock::now();
    auto ultimo_estado    = ultimo_progreso;

    while (recibidas < total_muestras && !g_parar && !error_disco) {
        size_t cabe = std::min<uint64_t>(muestras_bloque - llenado, total_muestras - recibidas);
        cabe        = std::min(cabe, spp * 16);
        size_t n    = rx->recv(buf.data() + llenado * bytes_por_muestra, cabe, md, timeout, false);
        timeout     = 0.2;

        switch (md.error_code) {
            case uhd::rx_metadata_t::ERROR_CODE_NONE:
                break;
            case uhd::rx_metadata_t::ERROR_CODE_OVERFLOW:
                if (md.out_of_sequence)
                    n_fuera_secuencia++; // 'D' (paquetes perdidos en la red)
                else
                    n_overflow++; // 'O'
                break;
            case uhd::rx_metadata_t::ERROR_CODE_TIMEOUT:
                n_timeout++;
                if (n_timeout > 20) {
                    std::cerr << "\nDemasiados timeouts, abortando." << std::endl;
                    g_parar = true;
                }
                break;
            default:
                n_otros++;
                std::cerr << "\nError de recepcion: " << md.strerror() << std::endl;
                break;
        }
        if (n == 0)
            continue;
        if (primera) {
            t_inicio      = std::chrono::system_clock::now();
            t_usrp_inicio = md.time_spec.get_real_secs();
            primera       = false;
        }
        llenado += n;
        recibidas += n;
        if (llenado == muestras_bloque || recibidas >= total_muestras) {
            buf.resize(llenado * bytes_por_muestra);
            {
                std::lock_guard<std::mutex> lk(cola.m);
                cola.q.push_back(std::move(buf));
                cola.max_en_cola = std::max(cola.max_en_cola, cola.q.size());
            }
            cola.cv.notify_one();
            buf     = obtener_buffer();
            llenado = 0;
        }
        auto ahora = std::chrono::steady_clock::now();
        if (ahora - ultimo_estado > std::chrono::milliseconds(500)) {
            ultimo_estado = ahora;
            // linea para la interfaz: recibidas total O D
            std::cout << "PROGRESO " << recibidas << " " << total_muestras << " " << n_overflow
                      << " " << n_fuera_secuencia << std::endl;
        }
        if (ahora - ultimo_progreso > std::chrono::seconds(5)) {
            ultimo_progreso = ahora;
            std::cerr << "\r  " << std::fixed << std::setprecision(1)
                      << 100.0 * recibidas / total_muestras << " %  overflows(O)=" << n_overflow
                      << " perdidos(D)=" << n_fuera_secuencia << "      " << std::flush;
        }
    }

    rx->issue_stream_cmd(uhd::stream_cmd_t(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS));
    if (llenado > 0) {
        buf.resize(llenado * bytes_por_muestra);
        std::lock_guard<std::mutex> lk(cola.m);
        cola.q.push_back(std::move(buf));
    }
    {
        std::lock_guard<std::mutex> lk(cola.m);
        cola.fin = true;
    }
    cola.cv.notify_one();
    escritor.join();
    fout.close();
    std::cerr << std::endl;

    std::ostringstream js;
    js << std::setprecision(15) << "{"
       << "\"frecuencia_real_hz\":" << freq_real << ","
       << "\"tasa_real_sps\":" << tasa_real << ","
       << "\"ganancia_real_db\":" << ganancia_real << ","
       << "\"antena\":\"" << usrp->get_rx_antenna(canal) << "\","
       << "\"lo_enganchado\":" << (lo_ok ? "true" : "false") << ","
       << "\"hora_inicio_utc\":\"" << (primera ? "" : hora_utc_iso(t_inicio)) << "\","
       << "\"tiempo_usrp_inicio_s\":" << t_usrp_inicio << ","
       << "\"muestras_pedidas\":" << total_muestras << ","
       << "\"muestras_grabadas\":" << recibidas << ","
       << "\"overflows_O\":" << n_overflow << ","
       << "\"paquetes_perdidos_D\":" << n_fuera_secuencia << ","
       << "\"timeouts\":" << n_timeout << ","
       << "\"otros_errores\":" << n_otros << ","
       << "\"max_bloques_en_cola\":" << cola.max_en_cola << ","
       << "\"interrumpido\":" << (g_parar ? "true" : "false") << ","
       << "\"error_disco\":" << (error_disco ? "true" : "false") << "}";
    std::cout << "RESUMEN_JSON " << js.str() << std::endl;

    return (error_disco || recibidas == 0) ? EXIT_FAILURE : EXIT_SUCCESS;
}
