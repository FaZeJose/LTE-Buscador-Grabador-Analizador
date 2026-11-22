/*
 * analizar_celda_iq.cc
 *
 * Decodificador LTE sobre un archivo IQ (cf32) en lugar de la radio.
 * Lo usa analizador_ui.py / analizar_grabacion.py. No modifica ni depende de
 * cell_search_plmn: reutiliza la misma cadena de srsRAN (PSS/SSS, MIB, SIB1, SIB2).
 *
 * Fases:
 *   -m mib  archivo a 1.92 Msps centrado en el EARFCN: busca celdas (PSS/SSS),
 *           decodifica el MIB y devuelve PCI, PRB, puertos, SFN, CFO.
 *   -m sib  archivo a la tasa de la celda (segun PRB): sincroniza con la celda
 *           indicada y decodifica MIB + SIB1 (+ SIB2).
 *
 * Cada resultado se imprime en una linea "RESULTADO {json}".
 */

#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <unistd.h>

// srsran.h primero: declara la API C con extern "C"
#include "srsran/srsran.h"
#include "srsran/asn1/rrc/bcch_msg.h"

// ------------------------------------------------------------------ opciones
static const char* g_file      = NULL;
static std::string g_modo      = "mib";
static double      g_psr_min   = 1.5;
static uint32_t    g_max_pss   = 200;  // tramas maximas buscando PSS (5 ms c/u)
static uint32_t    g_max_pbch  = 60;   // tramas maximas buscando MIB
static int         g_pci       = -1;
static uint32_t    g_prb       = 0;
static uint32_t    g_ports     = 1;
static bool        g_cp_ext    = false;
static float       g_cfo_hz    = 0.0f;
static uint32_t    g_sib1_max  = 60;
static uint32_t    g_sib2_sf   = 3000;

static volatile sig_atomic_t go_exit = 0;
static void                  sig_int_handler(int) { go_exit = 1; }

// ------------------------------------------------------------------ JSON
static std::string jesc(const char* s)
{
  std::string o;
  for (; s && *s; ++s) {
    if (*s == '"' || *s == '\\') {
      o += '\\';
      o += *s;
    } else if ((unsigned char)*s < 0x20) {
      o += ' ';
    } else {
      o += *s;
    }
  }
  return o;
}

// ------------------------------------------------------------------ lectura de archivo
struct fuente_t {
  FILE*    f;
  uint64_t leidas;
  bool     eof;
};

static int leer(fuente_t* src, cf_t* dst, uint32_t n)
{
  if (src->eof) {
    return -1;
  }
  size_t r = fread(dst, sizeof(cf_t), n, src->f);
  src->leidas += r;
  if (r < n) {
    src->eof = true;
    return -1; // fin del archivo: termina la busqueda
  }
  return (int)n;
}

static int recv_cb(void* h, void* data, uint32_t n, srsran_timestamp_t* t)
{
  return leer((fuente_t*)h, (cf_t*)data, n);
}

static int recv_cb_multi(void* h, cf_t* data[SRSRAN_MAX_CHANNELS], uint32_t n, srsran_timestamp_t* t)
{
  return leer((fuente_t*)h, data[0], n);
}

static bool abrir(fuente_t* src)
{
  src->f      = fopen(g_file, "rb");
  src->leidas = 0;
  src->eof    = false;
  if (!src->f) {
    fprintf(stderr, "No se puede abrir %s\n", g_file);
    return false;
  }
  return true;
}

// ------------------------------------------------------------------ fase MIB
static int fase_mib()
{
  fuente_t src;
  if (!abrir(&src)) {
    return -1;
  }
  srsran_ue_cellsearch_t        cs;
  srsran_ue_cellsearch_result_t found[3];
  bzero(found, sizeof(found));

  if (srsran_ue_cellsearch_init(&cs, g_max_pss, recv_cb, (void*)&src)) {
    fprintf(stderr, "Error iniciando cell search\n");
    return -1;
  }
  srsran_ue_cellsearch_set_nof_valid_frames(&cs, 4);
  int n = srsran_ue_cellsearch_scan(&cs, found, NULL);
  srsran_ue_cellsearch_free(&cs);
  fclose(src.f);

  int nres = 0;
  for (int i = 0; i < 3 && !go_exit; ++i) {
    if (found[i].psr <= 0 || found[i].psr <= g_psr_min) {
      continue;
    }
    // PCI repetido (misma celda vista con varios N_id_2): ya informado
    bool repetido = false;
    for (int j = 0; j < i; ++j) {
      if (found[j].cell_id == found[i].cell_id && found[j].psr > g_psr_min) {
        repetido = true;
      }
    }
    if (repetido) {
      continue;
    }

    srsran_cell_t cell = {};
    cell.id            = found[i].cell_id;
    cell.cp            = found[i].cp;
    cell.nof_prb       = SRSRAN_UE_MIB_NOF_PRB;
    cell.nof_ports     = 0;
    float    cfo       = found[i].cfo;
    uint32_t sfn       = 0;
    int      sfn_off   = 0;
    bool     mib_ok    = false;

    // Volver al inicio del archivo para decodificar el MIB de esta celda
    fuente_t s2;
    if (abrir(&s2)) {
      srsran_ue_mib_sync_t ue_mib;
      uint8_t              bch[SRSRAN_BCH_PAYLOAD_LEN] = {};
      if (srsran_ue_mib_sync_init_multi(&ue_mib, recv_cb_multi, 1, (void*)&s2) == 0 &&
          srsran_ue_mib_sync_set_cell(&ue_mib, cell) == 0) {
        ue_mib.ue_sync.cfo_current_value       = cfo / 15000;
        ue_mib.ue_sync.cfo_is_copied           = true;
        ue_mib.ue_sync.cfo_correct_enable_find = true;
        srsran_sync_set_cfo_cp_enable(&ue_mib.ue_sync.sfind, false, 0);
        int r = srsran_ue_mib_sync_decode(&ue_mib, g_max_pbch, bch, &cell.nof_ports, &sfn_off);
        if (r == 1) {
          srsran_pbch_mib_unpack(bch, &cell, &sfn);
          sfn    = (sfn + sfn_off + 1024) % 1024;
          mib_ok = true;
        }
        cfo = srsran_ue_sync_get_cfo(&ue_mib.ue_sync);
        srsran_ue_mib_sync_free(&ue_mib);
      }
      fclose(s2.f);
    }

    printf("RESULTADO {\"fase\":\"mib\",\"pci\":%u,\"psr\":%.3f,\"cfo_hz\":%.1f,\"cp\":\"%s\","
           "\"mib_ok\":%s,\"prb\":%u,\"ports\":%u,\"sfn\":%u,\"phich\":\"%s\"}\n",
           cell.id,
           found[i].psr,
           cfo,
           cell.cp == SRSRAN_CP_EXT ? "ext" : "norm",
           mib_ok ? "true" : "false",
           mib_ok ? cell.nof_prb : 0,
           mib_ok ? cell.nof_ports : 0,
           sfn,
           mib_ok ? (cell.phich_resources == SRSRAN_PHICH_R_1_6   ? "1/6"
                     : cell.phich_resources == SRSRAN_PHICH_R_1_2 ? "1/2"
                     : cell.phich_resources == SRSRAN_PHICH_R_1   ? "1"
                                                                  : "2")
                  : "");
    fflush(stdout);
    nres++;
  }
  if (nres == 0) {
    printf("RESULTADO {\"fase\":\"mib\",\"pci\":-1,\"psr\":%.3f,\"mib_ok\":false,\"busqueda\":%d}\n",
           found[0].psr,
           n);
  }
  return 0;
}

// ------------------------------------------------------------------ fase SIB
static int fase_sib()
{
  if (g_pci < 0 || g_prb == 0) {
    fprintf(stderr, "La fase sib necesita -c PCI y -r PRB\n");
    return -1;
  }
  fuente_t src;
  if (!abrir(&src)) {
    return -1;
  }

  srsran_cell_t cell   = {};
  cell.id              = (uint32_t)g_pci;
  cell.nof_prb         = g_prb;
  cell.nof_ports       = g_ports;
  cell.cp              = g_cp_ext ? SRSRAN_CP_EXT : SRSRAN_CP_NORM;
  cell.phich_length    = SRSRAN_PHICH_NORM;
  cell.phich_resources = SRSRAN_PHICH_R_1;
  cell.frame_type      = SRSRAN_FDD;

  srsran_ue_sync_t       ue_sync                         = {};
  srsran_ue_dl_t         ue_dl                           = {};
  srsran_ue_mib_t        ue_mib                          = {};
  srsran_softbuffer_rx_t softbuffers[SRSRAN_MAX_CODEWORDS] = {};
  cf_t*                  sf_buffer[SRSRAN_MAX_PORTS]     = {};
  uint8_t*               data[SRSRAN_MAX_CODEWORDS]      = {};

  const uint32_t max_samples = 3 * SRSRAN_SF_LEN_PRB(cell.nof_prb);
  sf_buffer[0]               = srsran_vec_cf_malloc(max_samples);

  if (srsran_ue_sync_init_multi_decim(&ue_sync, cell.nof_prb, false, recv_cb_multi, 1, (void*)&src, 0) ||
      srsran_ue_sync_set_cell(&ue_sync, cell) || srsran_ue_mib_init(&ue_mib, sf_buffer[0], cell.nof_prb) ||
      srsran_ue_mib_set_cell(&ue_mib, cell) || srsran_ue_dl_init(&ue_dl, sf_buffer, cell.nof_prb, 1) ||
      srsran_ue_dl_set_cell(&ue_dl, cell)) {
    fprintf(stderr, "Error inicializando la cadena de recepcion\n");
    return -1;
  }
  ue_sync.cfo_current_value       = g_cfo_hz / 15000.0f;
  ue_sync.cfo_is_copied           = true;
  ue_sync.cfo_correct_enable_find = true;
  ue_sync.cfo_correct_enable_track = true;
  srsran_sync_set_cfo_cp_enable(&ue_sync.sfind, false, 0);

  srsran_ue_dl_cfg_t ue_dl_cfg = {};
  srsran_dl_sf_cfg_t dl_sf     = {};
  srsran_pdsch_cfg_t pdsch_cfg = {};
  pdsch_cfg.meas_evm_en        = true;
  pdsch_cfg.rnti               = SRSRAN_SIRNTI;
  ue_dl_cfg.chest_cfg.estimator_alg        = SRSRAN_ESTIMATOR_ALG_INTERPOLATE;
  ue_dl_cfg.chest_cfg.sync_error_enable    = true;
  ue_dl_cfg.chest_cfg.cfo_estimate_enable  = false;
  ue_dl_cfg.chest_cfg.cfo_estimate_sf_mask = 1023;
  ue_dl_cfg.cfg.dci_common_ss              = true;
  ue_dl_cfg.cfg.pdsch.use_tbs_index_alt    = false;
  for (uint32_t i = 0; i < SRSRAN_MAX_CODEWORDS; ++i) {
    pdsch_cfg.softbuffers.rx[i] = &softbuffers[i];
    srsran_softbuffer_rx_init(&softbuffers[i], cell.nof_prb);
    data[i] = (uint8_t*)calloc(2000 * 8, sizeof(uint8_t));
  }

  bool        mib_ok = false, sib1_ok = false, sib2_ok = false;
  uint32_t    sfn = 0, mib_try = 0, sib1_try = 0, sf_after = 0;
  std::string j1, j2; // campos JSON de SIB1 y SIB2

  for (uint32_t count = 0; count < 60000 && !go_exit && !sib2_ok; ++count) {
    cf_t* buffers[SRSRAN_MAX_CHANNELS] = {};
    buffers[0]                         = sf_buffer[0];
    int r                              = srsran_ue_sync_zerocopy(&ue_sync, buffers, max_samples);
    if (r < 0) {
      break; // fin del archivo
    }
    if (r != 1) {
      continue;
    }
    uint32_t sf_idx = srsran_ue_sync_get_sfidx(&ue_sync);

    if (sf_idx == 0 && !mib_ok) {
      uint8_t bch[SRSRAN_BCH_PAYLOAD_LEN] = {};
      int     sfn_off                     = 0;
      mib_try++;
      if (srsran_ue_mib_decode(&ue_mib, bch, NULL, &sfn_off) == SRSRAN_UE_MIB_FOUND) {
        srsran_cell_t c = cell;
        srsran_pbch_mib_unpack(bch, &c, &sfn);
        sfn    = (sfn + sfn_off + 1024) % 1024;
        mib_ok = true;
      }
    }

    bool sib1_slot = (sf_idx == 5 && (sfn % 2) == 0);
    if (mib_ok && (sib1_ok || sib1_slot)) {
      if (!sib1_ok) {
        sib1_try++;
      } else if (++sf_after > g_sib2_sf) {
        break;
      }
      bool acks[SRSRAN_MAX_CODEWORDS] = {};
      dl_sf.tti                       = sfn * 10 + sf_idx;
      dl_sf.sf_type                   = SRSRAN_SF_NORM;
      int n                           = 0;
      for (uint32_t tm = 0; tm < 4 && !n; ++tm) {
        ue_dl_cfg.cfg.tm = (srsran_tm_t)tm;
        if ((ue_dl_cfg.cfg.tm == SRSRAN_TM1 && cell.nof_ports == 1) ||
            (ue_dl_cfg.cfg.tm > SRSRAN_TM1 && cell.nof_ports > 1)) {
          n = srsran_ue_dl_find_and_decode(&ue_dl, &dl_sf, &ue_dl_cfg, &pdsch_cfg, data, acks);
        }
      }
      if (n > 0 && pdsch_cfg.grant.tb[0].enabled && acks[0]) {
        uint32_t nbytes = (pdsch_cfg.grant.tb[0].tbs + 7) / 8;
        if (nbytes > 0 && nbytes <= 2000 * 8) {
          asn1::rrc::bcch_dl_sch_msg_s msg;
          asn1::cbit_ref               bref(data[0], nbytes * 8);
          if (msg.unpack(bref) == asn1::SRSASN_SUCCESS &&
              msg.msg.type() == asn1::rrc::bcch_dl_sch_msg_type_c::types_opts::c1) {
            auto& c1 = msg.msg.c1();
            if (c1.type() == asn1::rrc::bcch_dl_sch_msg_type_c::c1_c_::types::sib_type1 && !sib1_ok) {
              sib1_ok          = true;
              const auto& sib1 = c1.sib_type1();
              const auto& acc  = sib1.cell_access_related_info;
              char        buf[512];
              std::string plmns = "[";
              char        prev_mcc[4] = "";
              for (uint32_t i = 0; i < acc.plmn_id_list.size(); ++i) {
                const auto& p      = acc.plmn_id_list[i].plmn_id;
                char        mcc[4] = "", mnc[4] = "";
                if (p.mcc_present) {
                  for (int k = 0; k < 3; ++k)
                    mcc[k] = (char)('0' + p.mcc[k]);
                  mcc[3] = 0;
                  memcpy(prev_mcc, mcc, 4);
                } else {
                  memcpy(mcc, prev_mcc, 4);
                }
                size_t nm = 0;
                for (uint32_t k = 0; k < p.mnc.size() && nm < 3; ++k)
                  mnc[nm++] = (char)('0' + p.mnc[k]);
                mnc[nm] = 0;
                snprintf(buf, sizeof(buf), "%s{\"mcc\":\"%s\",\"mnc\":\"%s\"}", i ? "," : "", mcc, mnc);
                plmns += buf;
              }
              plmns += "]";
              std::string sched;
              for (uint32_t i = 0; i < sib1.sched_info_list.size(); ++i) {
                sched += (i ? " " : "") + std::string("SI-") + std::to_string(i + 1) + ":" +
                         sib1.sched_info_list[i].si_periodicity.to_string() + "[";
                for (uint32_t k = 0; k < sib1.sched_info_list[i].sib_map_info.size(); ++k)
                  sched += (k ? "," : "") + std::string(sib1.sched_info_list[i].sib_map_info[k].to_string());
                sched += "]";
              }
              snprintf(buf,
                       sizeof(buf),
                       ",\"plmn\":%s,\"tac\":%u,\"eci\":%u,\"cell_barred\":\"%s\",\"intra_freq_resel\":\"%s\","
                       "\"q_rx_lev_min_dbm\":%d,\"p_max\":%s,\"freq_band\":%u,\"si_win_ms\":%u,\"si_tag\":%u,",
                       plmns.c_str(),
                       (uint32_t)acc.tac.to_number(),
                       (uint32_t)acc.cell_id.to_number(),
                       acc.cell_barred.to_string(),
                       acc.intra_freq_resel.to_string(),
                       (int)sib1.cell_sel_info.q_rx_lev_min * 2,
                       sib1.p_max_present ? std::to_string((int)sib1.p_max).c_str() : "null",
                       (uint32_t)sib1.freq_band_ind,
                       (uint32_t)sib1.si_win_len.to_number(),
                       (uint32_t)sib1.sys_info_value_tag);
              j1 = std::string(buf) + "\"sched\":\"" + jesc(sched.c_str()) + "\",\"sib1_intentos\":" +
                   std::to_string(sib1_try);
            } else if (c1.type() == asn1::rrc::bcch_dl_sch_msg_type_c::c1_c_::types::sys_info) {
              const auto& si = c1.sys_info();
              if (si.crit_exts.type() == asn1::rrc::sys_info_s::crit_exts_c_::types_opts::sys_info_r8) {
                for (const auto& it : si.crit_exts.sys_info_r8().sib_type_and_info) {
                  if (it.type() == asn1::rrc::sib_info_item_c::types_opts::sib2) {
                    const auto& s2 = it.sib2();
                    char        buf[300];
                    snprintf(buf,
                             sizeof(buf),
                             ",\"ac_barring\":%s,\"mbsfn\":%s,\"ul_earfcn\":%s,\"ul_bw_prb\":%u,"
                             "\"add_spec_emission\":%u,\"time_align\":\"%s\"",
                             s2.ac_barr_info_present ? "true" : "false",
                             s2.mbsfn_sf_cfg_list_present ? "true" : "false",
                             s2.freq_info.ul_carrier_freq_present
                                 ? std::to_string((uint32_t)s2.freq_info.ul_carrier_freq).c_str()
                                 : "null",
                             s2.freq_info.ul_bw_present ? (uint32_t)s2.freq_info.ul_bw.to_number() : 0,
                             (uint32_t)s2.freq_info.add_spec_emission,
                             s2.time_align_timer_common.to_string());
                    j2      = buf;
                    sib2_ok = true;
                    break;
                  }
                }
              }
            }
          }
        }
      }
      if (!sib1_ok && sib1_try >= g_sib1_max) {
        break;
      }
    }
    if (sf_idx == 9 && mib_ok) {
      sfn = (sfn + 1) % 1024;
    }
  }

  float snr = ue_dl.chest_res.snr_db;
  printf("RESULTADO {\"fase\":\"sib\",\"pci\":%u,\"prb\":%u,\"mib_ok\":%s,\"sib1_ok\":%s,\"sib2_ok\":%s,"
         "\"sfn\":%u,\"mib_intentos\":%u,\"cfo_hz\":%.1f,\"snr_db\":%.1f%s%s}\n",
         cell.id,
         cell.nof_prb,
         mib_ok ? "true" : "false",
         sib1_ok ? "true" : "false",
         sib2_ok ? "true" : "false",
         sfn,
         mib_try,
         srsran_ue_sync_get_cfo(&ue_sync),
         isfinite(snr) ? snr : -99.0f,
         j1.empty() ? "" : j1.c_str(),
         j2.c_str());
  fflush(stdout);

  srsran_ue_mib_free(&ue_mib);
  srsran_ue_dl_free(&ue_dl);
  srsran_ue_sync_free(&ue_sync);
  for (uint32_t i = 0; i < SRSRAN_MAX_CODEWORDS; ++i) {
    srsran_softbuffer_rx_free(&softbuffers[i]);
    free(data[i]);
  }
  free(sf_buffer[0]);
  fclose(src.f);
  return 0;
}

static void uso(const char* p)
{
  printf("Uso: %s -f archivo.cf32 [-m mib|sib] [opciones]\n", p);
  printf("  -m mib : archivo a 1.92 Msps; busca PSS/SSS y decodifica MIB\n");
  printf("  -m sib : archivo a la tasa de la celda; decodifica MIB, SIB1 y SIB2\n");
  printf("  -p PSR minimo (mib) [%.1f]\n", g_psr_min);
  printf("  -n tramas maximas PSS (mib) [%u]\n", g_max_pss);
  printf("  -x tramas maximas PBCH (mib) [%u]\n", g_max_pbch);
  printf("  -c PCI  -r PRB  -t puertos  -e (CP extendido)  -o CFO Hz  (sib)\n");
  printf("  -s intentos maximos SIB1 (sib) [%u]\n", g_sib1_max);
  printf("  -v mas detalle de srsRAN\n");
}

int main(int argc, char** argv)
{
  int opt;
  while ((opt = getopt(argc, argv, "f:m:p:n:x:c:r:t:eo:s:vh")) != -1) {
    switch (opt) {
      case 'f': g_file = optarg; break;
      case 'm': g_modo = optarg; break;
      case 'p': g_psr_min = strtod(optarg, NULL); break;
      case 'n': g_max_pss = (uint32_t)strtoul(optarg, NULL, 10); break;
      case 'x': g_max_pbch = (uint32_t)strtoul(optarg, NULL, 10); break;
      case 'c': g_pci = (int)strtol(optarg, NULL, 10); break;
      case 'r': g_prb = (uint32_t)strtoul(optarg, NULL, 10); break;
      case 't': g_ports = (uint32_t)strtoul(optarg, NULL, 10); break;
      case 'e': g_cp_ext = true; break;
      case 'o': g_cfo_hz = strtof(optarg, NULL); break;
      case 's': g_sib1_max = (uint32_t)strtoul(optarg, NULL, 10); break;
      case 'v': increase_srsran_verbose_level(); break;
      default: uso(argv[0]); return 1;
    }
  }
  if (!g_file) {
    uso(argv[0]);
    return 1;
  }
  signal(SIGINT, sig_int_handler);
  signal(SIGTERM, sig_int_handler);
  if (g_modo == "mib") {
    return fase_mib() < 0 ? 1 : 0;
  }
  if (g_modo == "sib") {
    return fase_sib() < 0 ? 1 : 0;
  }
  uso(argv[0]);
  return 1;
}
