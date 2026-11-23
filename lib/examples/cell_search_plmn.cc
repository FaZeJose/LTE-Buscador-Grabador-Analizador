// SPDX-FileCopyrightText: Copyright (C) 2013-2026 Software Radio Systems Limited
// SPDX-License-Identifier: AGPL-3.0-or-later

#include <math.h>
#include <stdarg.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/time.h>
#include <unistd.h>

#include <unistd.h>

#include "srsran/srsran.h"
#include "srsran/asn1/rrc/bcch_msg.h"
#include "srsran/phy/fec/softbuffer.h"
#include "srsran/phy/ue/ue_sync.h"
#include "srsran/phy/ue/ue_dl.h"
#include "srsran/phy/ue/ue_mib.h"

#include "srsran/common/crash_handler.h"
extern "C" {
#include "srsran/phy/rf/rf_utils.h"
}

#ifndef DISABLE_RF
#include "srsran/phy/rf/rf.h"
#endif

#define MHZ 1000000
#define SAMP_FREQ 1920000
#define FLEN 9600
#define FLEN_PERIOD 0.005

#define MAX_EARFCN 1000

int band         = -1;
int earfcn_start = -1, earfcn_end = -1;
double off_range_khz = 0.0, off_step_khz = 10.0;
double psr_min = 2.0;                    // umbral minimo de PSR para aceptar una deteccion
uint32_t g_mib_max = 15, g_sib1_max = 30; // intentos maximos MIB / SIB1
bool dump_json = false;                  // imprimir SIB2 completo en JSON

cell_search_cfg_t cell_detect_config = {.max_frames_pbch      = SRSRAN_DEFAULT_MAX_FRAMES_PBCH,
                                        .max_frames_pss       = SRSRAN_DEFAULT_MAX_FRAMES_PSS,
                                        .nof_valid_pss_frames = SRSRAN_DEFAULT_NOF_VALID_PSS_FRAMES,
                                        .init_agc             = 0,
                                        .force_tdd            = false};


struct decoded_cell_info {
  // capa fisica / MIB
  int      pci       = -1;
  int      dl_earfcn = -1;
  double   freq      = 0.0;
  uint32_t prb       = 0;
  uint32_t ports     = 0;
  float    power_dbm = 0.0f;
  float    cfo_hz    = 0.0f;
  uint32_t sfn       = 0;
  uint32_t mib_tries  = 0;
  uint32_t sib1_tries = 0;
  bool     sib1_ok   = false;
  bool     sib2_ok   = false;
  // SIB1
  uint32_t    eci = 0;
  uint32_t    tac = 0;
  char        mcc[4]       = "N/A";
  char        mnc[4]       = "N/A";
  char        plmn_all[96] = "N/A";
  const char* operador     = "N/A";
  char        cell_barred[16] = "N/A";
  char        intra_freq[16]  = "N/A";
  bool        csg_ind          = false;
  uint32_t    csg_id           = 0;
  int         q_rx_lev_min_dbm = 0;
  bool        p_max_present    = false;
  int         p_max            = 0;
  uint32_t    freq_band        = 0;
  uint32_t    si_win_ms        = 0;
  uint32_t    si_tag           = 0;
  char        sched[200]       = "";
  // SIB2
  bool     ac_barring        = false;
  bool     mbsfn             = false;
  bool     ul_freq_present   = false;
  uint32_t ul_carrier_freq   = 0;
  uint32_t ul_bw_prb         = 0;
  uint32_t add_spec_emission = 0;
  char     time_align[16]    = "N/A";
};

// ---- Tabla de operadores por PLMN. Editala libremente. ----
struct plmn_entry {
  const char* mcc;
  const char* mnc;
  const char* name;
};

static const plmn_entry plmn_table[] = {
    {"716", "06", "Movistar Peru (terrestre)"},
    {"716", "10", "Claro Peru (terrestre)"},
    {"716", "20", "Claro Peru (terrestre)"},
    {"716", "15", "Bitel Peru (terrestre)"},
    {"716", "07", "Entel Peru (terrestre o posible satelital)"},
    {"716", "17", "Entel Peru (terrestre o posible satelital)"},
    {"730", "29", "Entel SpaceX Chile (SATELITAL)"},
};

static const char* const OP_UNKNOWN = "DESCONOCIDO - posible satelital";

static const char* operator_name(const char* mcc, const char* mnc)
{
  for (size_t i = 0; i < sizeof(plmn_table) / sizeof(plmn_table[0]); ++i) {
    if (strcmp(plmn_table[i].mcc, mcc) == 0 && strcmp(plmn_table[i].mnc, mnc) == 0) {
      return plmn_table[i].name;
    }
  }
  return OP_UNKNOWN;
}

static void sappend(char* dst, size_t cap, const char* fmt, ...)
{
  size_t len = strlen(dst);
  if (len + 1 >= cap) {
    return;
  }
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(dst + len, cap - len, fmt, ap);
  va_end(ap);
}

// Guarda en 'info' todo lo relevante del SIB1.
static void store_sib1_info(const asn1::rrc::sib_type1_s& sib1, decoded_cell_info* info)
{
  const auto& access = sib1.cell_access_related_info;

  const char* best = NULL;
  char        prev_mcc[4] = "N/A";
  info->plmn_all[0] = '\0';

  for (uint32_t i = 0; i < access.plmn_id_list.size(); ++i) {
    const auto& plmn = access.plmn_id_list[i].plmn_id;
    char        mcc[4] = "N/A";
    char        mnc[4] = "";

    if (plmn.mcc_present) {
      for (int k = 0; k < 3; ++k) {
        mcc[k] = (char)('0' + plmn.mcc[k]);
      }
      mcc[3] = '\0';
      memcpy(prev_mcc, mcc, 4);
    } else {
      memcpy(mcc, prev_mcc, 4);
    }

    size_t nm = 0;
    for (uint32_t j = 0; j < plmn.mnc.size() && nm < 3; ++j) {
      mnc[nm++] = (char)('0' + plmn.mnc[j]);
    }
    mnc[nm] = '\0';

    sappend(info->plmn_all, sizeof(info->plmn_all), "%s%s-%s", i ? ", " : "", mcc, mnc);

    const char* name = operator_name(mcc, mnc);
    if (i == 0) {
      memcpy(info->mcc, mcc, 4);
      snprintf(info->mnc, sizeof(info->mnc), "%s", mnc);
    }
    if (!best || best == OP_UNKNOWN) {
      best = name;
    } else if (strstr(name, "SATELITAL")) {
      best = name;
    }
  }
  info->operador = best ? best : OP_UNKNOWN;

  info->tac = (uint32_t)access.tac.to_number();
  info->eci = (uint32_t)access.cell_id.to_number();
  snprintf(info->cell_barred, sizeof(info->cell_barred), "%s", access.cell_barred.to_string());
  snprintf(info->intra_freq, sizeof(info->intra_freq), "%s", access.intra_freq_resel.to_string());
  info->csg_ind = access.csg_ind;
  info->csg_id  = access.csg_id_present ? (uint32_t)access.csg_id.to_number() : 0;

  info->q_rx_lev_min_dbm = (int)sib1.cell_sel_info.q_rx_lev_min * 2;
  info->p_max_present    = sib1.p_max_present;
  info->p_max            = (int)sib1.p_max;
  info->freq_band        = (uint32_t)sib1.freq_band_ind;
  info->si_win_ms        = (uint32_t)sib1.si_win_len.to_number();
  info->si_tag           = (uint32_t)sib1.sys_info_value_tag;

  info->sched[0] = '\0';
  for (uint32_t i = 0; i < sib1.sched_info_list.size(); ++i) {
    sappend(info->sched, sizeof(info->sched), "%sSI-%u:%s[", i ? " " : "", i + 1,
            sib1.sched_info_list[i].si_periodicity.to_string());
    for (uint32_t j = 0; j < sib1.sched_info_list[i].sib_map_info.size(); ++j) {
      sappend(info->sched, sizeof(info->sched), "%s%s", j ? "," : "",
              sib1.sched_info_list[i].sib_map_info[j].to_string());
    }
    sappend(info->sched, sizeof(info->sched), "]");
  }
}

// Guarda en 'info' los campos principales del SIB2.
static void store_sib2_info(const asn1::rrc::sib_type2_s& s2, decoded_cell_info* info)
{
  info->sib2_ok           = true;
  info->ac_barring        = s2.ac_barr_info_present;
  info->mbsfn             = s2.mbsfn_sf_cfg_list_present;
  info->ul_freq_present   = s2.freq_info.ul_carrier_freq_present;
  info->ul_carrier_freq   = (uint32_t)s2.freq_info.ul_carrier_freq;
  info->ul_bw_prb         = s2.freq_info.ul_bw_present ? (uint32_t)s2.freq_info.ul_bw.to_number() : 0;
  info->add_spec_emission = (uint32_t)s2.freq_info.add_spec_emission;
  snprintf(info->time_align, sizeof(info->time_align), "%s", s2.time_align_timer_common.to_string());
}

struct cells {
  srsran_cell_t cell;
  double        freq;
  int           dl_earfcn;
  float         power;
  float         cfo;
};

struct cells results[1024];

float rf_gain = 70.0;
char empty_rf_args[] = "";
char* rf_args = empty_rf_args;
char empty_rf_dev[] = "";
char* rf_dev = empty_rf_dev;

void usage(char* prog)
{
  printf("Usage: %s [agsendtvb] -b band\n", prog);
  printf("\t-a RF args [Default %s]\n", rf_args);
  printf("\t-d RF devicename [Default %s]\n", rf_dev);
  printf("\t-g RF gain [Default %.2f dB]\n", rf_gain);
  printf("\t-s earfcn_start [Default All]\n");
  printf("\t-e earfcn_end [Default All]\n");
  printf("\t-n nof_frames_total [Default 100]\n");
  printf("\t-o offset range +-kHz for Doppler sweep [Default 0=off]\n");
  printf("\t-k offset step kHz [Default 10]\n");
  printf("\t-m max MIB attempts [Default 15]\n");
  printf("\t-r max SIB1 attempts [Default 30]\n");
  printf("\t-p min PSR to accept a detection [Default 2.0]\n");
  printf("\t-j dump SIB2 as JSON\n");
  printf("\t-v [set srsran_verbose to debug, default none]\n");
}

void parse_args(int argc, char** argv)
{
  int opt;
  while ((opt = getopt(argc, argv, "a:b:d:s:e:n:g:o:k:m:r:p:jv")) != -1) {
    switch (opt) {
      case 'a':
        rf_args = optarg;
        break;
      case 'b':
        band = (int)strtol(optarg, NULL, 10);
        break;
      case 'd':
        rf_dev = optarg;
        break;
      case 's':
        earfcn_start = (int)strtol(optarg, NULL, 10);
        break;
      case 'e':
        earfcn_end = (int)strtol(optarg, NULL, 10);
        break;
      case 'n':
        cell_detect_config.max_frames_pss = (uint32_t)strtol(optarg, NULL, 10);
        break;
      case 'g':
        rf_gain = strtof(optarg, NULL);
        break;
      case 'o':
        off_range_khz = strtod(optarg, NULL);
        break;
      case 'k':
        off_step_khz = strtod(optarg, NULL);
        break;
      case 'm':
        g_mib_max = (uint32_t)strtol(optarg, NULL, 10);
        break;
      case 'r':
        g_sib1_max = (uint32_t)strtol(optarg, NULL, 10);
        break;
      case 'p':
        psr_min = strtod(optarg, NULL);
        break;
      case 'j':
        dump_json = true;
        break;
      case 'v':
        increase_srsran_verbose_level();
        break;
      default:
        usage(argv[0]);
        exit(-1);
    }
  }
  if (band == -1) {
    usage(argv[0]);
    exit(-1);
  }
}

int srsran_rf_recv_wrapper(void* h, void* data, uint32_t nsamples, srsran_timestamp_t* t)
{
  DEBUG(" ----  Receive %d samples  ---- ", nsamples);
  return srsran_rf_recv_with_time((srsran_rf_t*)h, data, nsamples, 1, NULL, NULL);
}


#ifndef DISABLE_RF
int srsran_rf_recv_wrapper_multi(void* h,
                                 cf_t* data_[SRSRAN_MAX_PORTS],
                                 uint32_t nsamples,
                                 srsran_timestamp_t* t)
{
  void* ptr[SRSRAN_MAX_PORTS] = {};
  for (uint32_t i = 0; i < SRSRAN_MAX_PORTS; ++i) {
    ptr[i] = data_[i];
  }

  return srsran_rf_recv_with_time_multi(
      (srsran_rf_t*)h, ptr, nsamples, true, NULL, NULL);
}
#endif

volatile sig_atomic_t go_exit = 0;

void sig_int_handler(int signo)
{
  if (signo == SIGINT) {
    go_exit = true;
  }
}

static SRSRAN_AGC_CALLBACK(srsran_rf_set_rx_gain_wrapper)
{
  srsran_rf_set_rx_gain((srsran_rf_t*)h, gain_db);
}


#ifndef DISABLE_RF

static bool decode_sib1_for_cell(srsran_rf_t* rf, const cells& result, decoded_cell_info* decoded)
{
  const srsran_cell_t cell = result.cell;

  if (decoded) {
    decoded->pci = cell.id;
    decoded->dl_earfcn = result.dl_earfcn;
    decoded->freq = result.freq;
    decoded->prb = cell.nof_prb;
    decoded->ports = cell.nof_ports;
    decoded->power_dbm = srsran_convert_power_to_dB(result.power);
    decoded->cfo_hz = result.cfo;
  }

  if (cell.frame_type == SRSRAN_TDD) {
    printf("PCI %d: TDD todavía no implementado para SIB1.\n", cell.id);
    return false;
  }

  printf("\n[ DECODIFICANDO ] PCI=%d EARFCN=%d Freq=%.3f MHz\n",
         cell.id, result.dl_earfcn, result.freq);

  srsran_ue_sync_t ue_sync = {};
  srsran_ue_dl_t ue_dl = {};
  srsran_ue_mib_t ue_mib = {};
  srsran_softbuffer_rx_t softbuffers[SRSRAN_MAX_CODEWORDS] = {};

  cf_t* sf_buffer[SRSRAN_MAX_PORTS] = {};
  uint8_t* data[SRSRAN_MAX_CODEWORDS] = {};

  bool soft_initialized[SRSRAN_MAX_CODEWORDS] = {};
  bool sync_initialized = false;
  bool dl_initialized = false;
  bool mib_initialized = false;
  bool stream_started = false;

  auto cleanup = [&]() {
    if (stream_started) {
      srsran_rf_stop_rx_stream(rf);
    }

    if (mib_initialized) {
      srsran_ue_mib_free(&ue_mib);
    }

    if (dl_initialized) {
      srsran_ue_dl_free(&ue_dl);
    }

    if (sync_initialized) {
      srsran_ue_sync_free(&ue_sync);
    }

    for (uint32_t i = 0; i < SRSRAN_MAX_CODEWORDS; ++i) {
      if (soft_initialized[i]) {
        srsran_softbuffer_rx_free(&softbuffers[i]);
      }
      free(data[i]);
    }

    for (uint32_t i = 0; i < SRSRAN_MAX_PORTS; ++i) {
      free(sf_buffer[i]);
    }
  };

  // Configurar frecuencia y tasa de muestreo LTE según el ancho de banda.
  const double sample_rate = srsran_sampling_freq_hz(cell.nof_prb);

  srsran_rf_set_rx_freq(rf, 0, (double)result.freq * MHZ);

  double actual_rate = srsran_rf_set_rx_srate(rf, sample_rate);

  printf("  PRB=%u, Fs solicitada=%.2f MHz, Fs configurada=%.2f MHz\n",
         cell.nof_prb, sample_rate / 1e6, actual_rate / 1e6);

  if (actual_rate <= 0) {
    printf("  Error configurando tasa de muestreo.\n");
    cleanup();
    return false;
  }

  // Inicializar sincronizador LTE.
  if (srsran_ue_sync_init_multi_decim(
          &ue_sync,
          cell.nof_prb,
          cell.id == 1000,
          srsran_rf_recv_wrapper_multi,
          1,
          (void*)rf,
          0)) {
    printf("  Error inicializando UE Sync.\n");
    cleanup();
    return false;
  }
  sync_initialized = true;

  if (srsran_ue_sync_set_cell(&ue_sync, cell)) {
    printf("  Error configurando celda en UE Sync.\n");
    cleanup();
    return false;
  }

  // Reutilizar la corrección CFO calculada durante la decodificación MIB.
  ue_sync.cfo_current_value = result.cfo / 15000.0f;
  ue_sync.cfo_is_copied = true;
  ue_sync.cfo_correct_enable_find = true;
  ue_sync.cfo_correct_enable_track = true;

  srsran_sync_set_cfo_cp_enable(&ue_sync.sfind, false, 0);

  // Buffers de recepción.
  const uint32_t max_samples = 3 * SRSRAN_SF_LEN_PRB(cell.nof_prb);

  sf_buffer[0] = srsran_vec_cf_malloc(max_samples);
  if (!sf_buffer[0]) {
    printf("  Error reservando buffer de muestras.\n");
    cleanup();
    return false;
  }

  if (srsran_ue_mib_init(&ue_mib, sf_buffer[0], cell.nof_prb)) {
    printf("  Error inicializando decodificador MIB.\n");
    cleanup();
    return false;
  }
  mib_initialized = true;

  if (srsran_ue_mib_set_cell(&ue_mib, cell)) {
    printf("  Error configurando celda MIB.\n");
    cleanup();
    return false;
  }

  printf("  Decodificador MIB/PBCH inicializado.\n");

  if (srsran_ue_dl_init(&ue_dl, sf_buffer, cell.nof_prb, 1)) {
    printf("  Error inicializando UE DL.\n");
    cleanup();
    return false;
  }
  dl_initialized = true;

  if (srsran_ue_dl_set_cell(&ue_dl, cell)) {
    printf("  Error configurando UE DL.\n");
    cleanup();
    return false;
  }

  // Configuración de decodificación PDSCH.
  srsran_ue_dl_cfg_t ue_dl_cfg = {};
  srsran_dl_sf_cfg_t dl_sf = {};
  srsran_pdsch_cfg_t pdsch_cfg = {};

  pdsch_cfg.meas_evm_en = true;
  pdsch_cfg.rnti = SRSRAN_SIRNTI;

  ue_dl_cfg.chest_cfg.estimator_alg = SRSRAN_ESTIMATOR_ALG_INTERPOLATE;
  ue_dl_cfg.chest_cfg.sync_error_enable = true;
  ue_dl_cfg.chest_cfg.cfo_estimate_enable = false;
  ue_dl_cfg.chest_cfg.cfo_estimate_sf_mask = 1023;

  ue_dl_cfg.cfg.dci_common_ss = true;
  ue_dl_cfg.cfg.pdsch.use_tbs_index_alt = false;

  // Softbuffers y buffers de datos.
  for (uint32_t i = 0; i < SRSRAN_MAX_CODEWORDS; ++i) {
    pdsch_cfg.softbuffers.rx[i] = &softbuffers[i];

    if (srsran_softbuffer_rx_init(&softbuffers[i], cell.nof_prb)) {
      printf("  Error inicializando softbuffer.\n");
      cleanup();
      return false;
    }
    soft_initialized[i] = true;

    data[i] = (uint8_t*)calloc(2000 * 8, sizeof(uint8_t));
    if (!data[i]) {
      printf("  Error reservando buffer PDSCH.\n");
      cleanup();
      return false;
    }
  }

  // Iniciar recepción.
  if (srsran_rf_start_rx_stream(rf, false)) {
    printf("  Error iniciando stream RF.\n");
    cleanup();
    return false;
  }
  stream_started = true;

  printf("  Flujo continuo: MIB (max %u intentos), SIB1 (max %u), luego SIB2.\n", g_mib_max, g_sib1_max);

  bool     found_sib1    = false;
  bool     found_sib2    = false;
  bool     mib_decoded   = false;
  uint32_t mib_try       = 0;
  uint32_t sib1_try      = 0;
  uint32_t sfn           = 0;
  uint32_t sf_after_sib1 = 0;
  const uint32_t sib2_budget_sf = 3000; // ~3 s buscando SIB2 despues de SIB1

  for (uint32_t count = 0; count < 30000 && !go_exit && !found_sib2; ++count) {
    cf_t* buffers[SRSRAN_MAX_CHANNELS] = {};
    buffers[0] = sf_buffer[0];

    int sync_ret = srsran_ue_sync_zerocopy(&ue_sync, buffers, max_samples);

    if (sync_ret < 0) {
      printf("  Error durante sincronizacion LTE.\n");
      break;
    }

    if (sync_ret != 1) {
      continue;
    }

    uint32_t sf_idx = srsran_ue_sync_get_sfidx(&ue_sync);

    // MIB/PBCH: un intento por trama (subtrama 0), siempre con muestras nuevas.
    // srsran_ue_mib_decode combina el PBCH de varias tramas.
    if (sf_idx == 0 && !mib_decoded) {
      uint8_t bch_payload[SRSRAN_BCH_PAYLOAD_LEN] = {};
      int     sfn_offset = 0;
      mib_try++;

      int mib_ret = srsran_ue_mib_decode(&ue_mib, bch_payload, NULL, &sfn_offset);

      if (mib_ret == SRSRAN_UE_MIB_FOUND) {
        srsran_cell_t mib_cell = cell;
        srsran_pbch_mib_unpack(bch_payload, &mib_cell, &sfn);
        sfn         = (sfn + sfn_offset + 1024) % 1024;
        mib_decoded = true;
        printf("  MIB   intento %2u/%u ... OK (SFN %u)\n", mib_try, g_mib_max, sfn);
        if (decoded) {
          decoded->sfn       = sfn;
          decoded->mib_tries = mib_try;
        }
      } else {
        printf("  MIB   intento %2u/%u ... no%s\n", mib_try, g_mib_max, mib_ret < 0 ? " (error PBCH)" : "");
        if (mib_try >= g_mib_max) {
          break;
        }
      }
    }

    // SIB1: subtrama 5 de tramas pares. Despues de SIB1 se prueba SI-RNTI
    // en todas las subtramas hasta encontrar SIB2.
    bool sib1_slot = (sf_idx == 5 && (sfn % 2) == 0);
    if (mib_decoded && (found_sib1 || sib1_slot)) {
      if (!found_sib1) {
        sib1_try++;
      } else {
        sf_after_sib1++;
        if (sf_after_sib1 > sib2_budget_sf) {
          break;
        }
      }

      bool acks[SRSRAN_MAX_CODEWORDS] = {};

      dl_sf.tti     = sfn * 10 + sf_idx;
      dl_sf.sf_type = SRSRAN_SF_NORM;

      int n = 0;

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

            if (c1.type() == asn1::rrc::bcch_dl_sch_msg_type_c::c1_c_::types::sib_type1) {
              if (!found_sib1) {
                found_sib1 = true;
                if (decoded) {
                  store_sib1_info(c1.sib_type1(), decoded);
                  decoded->sib1_ok    = true;
                  decoded->sib1_tries = sib1_try;
                }
                printf("  SIB1  intento %2u/%u ... OK (CRC OK)\n", sib1_try, g_sib1_max);
                printf("  SIB2  buscando SystemInformation (SI-RNTI)...\n");
              }

            } else if (c1.type() == asn1::rrc::bcch_dl_sch_msg_type_c::c1_c_::types::sys_info) {
              const auto& sys_info = c1.sys_info();

              if (sys_info.crit_exts.type() == asn1::rrc::sys_info_s::crit_exts_c_::types_opts::sys_info_r8) {
                const auto& sib_list = sys_info.crit_exts.sys_info_r8().sib_type_and_info;

                for (const auto& sib_item : sib_list) {
                  if (sib_item.type() == asn1::rrc::sib_info_item_c::types_opts::sib2) {
                    if (decoded) {
                      store_sib2_info(sib_item.sib2(), decoded);
                    }
                    printf("  SIB2  OK (%u subtramas despues de SIB1)\n", sf_after_sib1);
                    if (dump_json) {
                      asn1::json_writer json;
                      sib_item.sib2().to_json(json);
                      printf("%s\n", json.to_string().c_str());
                    }
                    found_sib2 = true;
                    break;
                  }
                }
              }
            }
          }
        }
      }

      if (!found_sib1) {
        printf("  SIB1  intento %2u/%u ... no\n", sib1_try, g_sib1_max);
        if (sib1_try >= g_sib1_max) {
          break;
        }
      }
    }

    // Incrementar SFN al finalizar la trama.
    if (sf_idx == 9 && mib_decoded) {
      sfn = (sfn + 1) % 1024;
    }
  }

  if (!mib_decoded) {
    printf("  MIB   no decodificado tras %u intentos.\n", mib_try);
  } else if (!found_sib1) {
    printf("  SIB1  no decodificado tras %u intentos.\n", sib1_try);
  } else if (!found_sib2 && !go_exit) {
    printf("  SIB2  no encontrado en %u subtramas.\n", sf_after_sib1);
  }

  if (decoded) {
    decoded->mib_tries  = mib_try;
    decoded->sib1_tries = sib1_try;
  }

  cleanup();
  return found_sib1;
}

#endif

static const char* lte_bandwidth_from_prb(uint32_t prb)
{
  switch (prb) {
    case 6:   return "1.4 MHz";
    case 15:  return "3 MHz";
    case 25:  return "5 MHz";
    case 50:  return "10 MHz";
    case 75:  return "15 MHz";
    case 100: return "20 MHz";
    default:  return "desconocido";
  }
}

#define BOXW 58

static void box_hr(const char* l, const char* r, const char* title)
{
  int used = 0;
  printf("%s", l);
  if (title && title[0]) {
    printf("─ %s ", title);
    used = 3 + (int)strlen(title);
  }
  for (int i = used; i < BOXW + 2; ++i) {
    printf("─");
  }
  printf("%s\n", r);
}

static void box_line(const char* fmt, ...)
{
  char    buf[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  printf("│ %-*.*s │\n", BOXW, BOXW, buf);
}

static void print_cell_box(const decoded_cell_info* d, uint32_t idx)
{
  char title[32];
  snprintf(title, sizeof(title), "CELDA #%u", idx);

  printf("\n");
  box_hr("┌", "┐", title);
  box_line("EARFCN: %d  (%.3f MHz)    PCI: %d", d->dl_earfcn, d->freq, d->pci);
  box_line("BW: %s (%u PRB)    Puertos antena: %u", lte_bandwidth_from_prb(d->prb), d->prb, d->ports);
  box_line("CFO: %+.1f Hz    Potencia PSS: %.1f dBm", d->cfo_hz, d->power_dbm);
  box_line("SFN: %u    Intentos: MIB %u, SIB1 %u", d->sfn, d->mib_tries, d->sib1_tries);

  box_hr("├", "┤", "SIB1");
  if (!d->sib1_ok) {
    box_line("No decodificado");
  } else {
    box_line("MCC: %s    MNC: %s", d->mcc, d->mnc);
    box_line("PLMN(s): %s", d->plmn_all);
    box_line("Operador: %s", d->operador);
    box_line("TAC: %u", d->tac);
    box_line("ECI: %u  (eNB ID %u / Cell ID %u)", d->eci, d->eci >> 8, d->eci & 0xFF);
    box_line("Cell barred: %s    Intra-freq resel: %s", d->cell_barred, d->intra_freq);
    box_line("CSG: %s", d->csg_ind ? "SI" : "NO");
    box_line("Qrxlevmin: %d dBm    Banda: %u", d->q_rx_lev_min_dbm, d->freq_band);
    if (d->p_max_present) {
      box_line("P-Max: %d dBm", d->p_max);
    } else {
      box_line("P-Max: no indicado");
    }
    box_line("SI window: %u ms    SI value tag: %u", d->si_win_ms, d->si_tag);
    box_line("SIB2 va en SI-1 (implicito)");
    size_t len = strlen(d->sched);
    for (size_t off = 0; off < len; off += BOXW - 7) {
      box_line("%s%.*s", off ? "       " : "Sched: ", (int)(BOXW - 7), d->sched + off);
    }
  }

  box_hr("├", "┤", "SIB2");
  if (!d->sib2_ok) {
    box_line("No decodificado");
  } else {
    box_line("AC barring: %s    MBSFN: %s", d->ac_barring ? "SI" : "NO", d->mbsfn ? "SI" : "NO");
    if (d->ul_freq_present) {
      box_line("UL EARFCN: %u", d->ul_carrier_freq);
    } else {
      box_line("UL EARFCN: por defecto (duplex estandar)");
    }
    if (d->ul_bw_prb) {
      box_line("UL BW: %u PRB", d->ul_bw_prb);
    } else {
      box_line("UL BW: igual que DL");
    }
    box_line("Add. spectrum emission: %u    Time align timer: %s", d->add_spec_emission, d->time_align);
  }
  box_hr("└", "┘", NULL);
}

static void print_decoded_cell_summary(const decoded_cell_info* d, uint32_t count)
{
  printf("\n========== RESUMEN DE CELDAS ==========\n");

  if (count == 0) {
    printf("No se detecto ninguna celda con MIB.\n");
    return;
  }

  printf("%-3s %-7s %-4s %-7s %-5s %-5s %-9s %-7s %s\n", "#", "EARFCN", "PCI", "BW", "SIB1", "SIB2", "PLMN", "TAC",
         "Operador");
  for (uint32_t i = 0; i < count; ++i) {
    char plmn[12] = "N/A";
    char tac[12]  = "N/A";
    if (d[i].sib1_ok) {
      snprintf(plmn, sizeof(plmn), "%s-%s", d[i].mcc, d[i].mnc);
      snprintf(tac, sizeof(tac), "%u", d[i].tac);
    }
    printf("%-3u %-7d %-4d %-7s %-5s %-5s %-9s %-7s %s\n",
           i + 1,
           d[i].dl_earfcn,
           d[i].pci,
           lte_bandwidth_from_prb(d[i].prb),
           d[i].sib1_ok ? "OK" : "no",
           d[i].sib2_ok ? "OK" : "no",
           plmn,
           tac,
           d[i].sib1_ok ? d[i].operador : "-");
  }
}

int main(int argc, char** argv)
{
  int                           n;
  srsran_rf_t                   rf;
  srsran_ue_cellsearch_t        cs;
  srsran_ue_cellsearch_result_t found_cells[3];
  int                           nof_freqs;
  srsran_earfcn_t               channels[MAX_EARFCN];
  int                           freq;
  uint32_t                      n_found_cells = 0;
  decoded_cell_info              decoded_cells[1024] = {};
  uint32_t                      n_decoded_cells = 0;
  static bool                   sib_ok_de[1024] = {};  // por indice de results: SIB1 decodificado
  static int                    dec_de[1024];          // por indice de results: indice en decoded_cells
  for (int k = 0; k < 1024; ++k) {
    dec_de[k] = -1;
  }

  srsran_debug_handle_crash(argc, argv);

  parse_args(argc, argv);

  printf("Opening RF device...\n");

  if (srsran_rf_open_devname(&rf, rf_dev, rf_args, 1)) {
    ERROR("Error opening rf");
    exit(-1);
  }
  if (!cell_detect_config.init_agc) {
    srsran_rf_set_rx_gain(&rf, rf_gain);
  } else {
    printf("Starting AGC thread...\n");
    if (srsran_rf_start_gain_thread(&rf, false)) {
      ERROR("Error opening rf");
      exit(-1);
    }
    srsran_rf_set_rx_gain(&rf, 50);
  }

  // Supress RF messages
  srsran_rf_suppress_stdout(&rf);

  nof_freqs = srsran_band_get_fd_band(band, channels, earfcn_start, earfcn_end, MAX_EARFCN);
  if (nof_freqs < 0) {
    ERROR("Error getting EARFCN list");
    exit(-1);
  }

  sigset_t sigset;
  sigemptyset(&sigset);
  sigaddset(&sigset, SIGINT);
  sigprocmask(SIG_UNBLOCK, &sigset, NULL);
  signal(SIGINT, sig_int_handler);

  if (srsran_ue_cellsearch_init(&cs, cell_detect_config.max_frames_pss, srsran_rf_recv_wrapper, (void*)&rf)) {
    ERROR("Error initiating UE cell detect");
    exit(-1);
  }

  if (cell_detect_config.max_frames_pss) {
    srsran_ue_cellsearch_set_nof_valid_frames(&cs, cell_detect_config.nof_valid_pss_frames);
  }
  if (cell_detect_config.init_agc) {
    srsran_rf_info_t* rf_info = srsran_rf_get_info(&rf);
    srsran_ue_sync_start_agc(&cs.ue_sync,
                             srsran_rf_set_rx_gain_wrapper,
                             rf_info->min_rx_gain,
                             rf_info->max_rx_gain,
                             cell_detect_config.init_agc);
  }

  int n_off = (off_range_khz > 0 && off_step_khz > 0) ? (int)(2 * off_range_khz / off_step_khz) + 1 : 1;
  for (int step_idx = 0; step_idx < nof_freqs * n_off && !go_exit; step_idx++) {
    freq = step_idx / n_off;
    // Orden del barrido: primero la frecuencia exacta (0) y luego +-k, +-2k...: una celda
    // terrestre se prueba primero sin desplazamiento, que es donde mejor decodifica.
    int    i_off  = step_idx % n_off;
    int    m_off  = (i_off + 1) / 2;
    double off_hz = (i_off == 0) ? 0.0 : ((i_off % 2) ? -m_off : m_off) * off_step_khz * 1e3;
    double rf_freq_hz = (double)channels[freq].fd * MHZ + off_hz;
    srsran_rf_set_rx_freq(&rf, 0, rf_freq_hz);
    INFO("Set rf_freq to %.3f MHz", (double)channels[freq].fd * MHZ / 1000000);

    printf("[%3d/%d]: EARFCN %d Freq. %.2f MHz looking for PSS (offset %+.1f kHz).\n",
           freq + 1, nof_freqs, channels[freq].id, channels[freq].fd, off_hz / 1e3);
    fflush(stdout);

    if (SRSRAN_VERBOSE_ISINFO()) {
      printf("\n");
    }

    bzero(found_cells, 3 * sizeof(srsran_ue_cellsearch_result_t));

    INFO("Setting sampling frequency %.2f MHz for PSS search", SRSRAN_CS_SAMP_FREQ / 1000000);
    srsran_rf_set_rx_srate(&rf, SRSRAN_CS_SAMP_FREQ);
    INFO("Starting receiver...");
    srsran_rf_start_rx_stream(&rf, false);

    n = srsran_ue_cellsearch_scan(&cs, found_cells, NULL);
    srsran_rf_stop_rx_stream(&rf);

    if (n < 0) {
      ERROR("Error searching cell");
      continue;
    }
    if (n == 0) {
      continue;
    }

    for (int i = 0; i < 3 && !go_exit; ++i) {
      if (found_cells[i].psr <= psr_min) {
        continue;
      }

      srsran_cell_t cell = {};
      cell.id = found_cells[i].cell_id;
      cell.cp = found_cells[i].cp;
      float search_cell_cfo = found_cells[i].cfo;

      printf("\n[DETECCION] EARFCN=%d PCI=%d. Decodificando MIB...\n",
             channels[freq].id, cell.id);

      int ret = rf_mib_decoder(&rf, 1, &cell_detect_config, &cell, &search_cell_cfo, NULL, NULL);
      if (ret < 0) {
        printf("  Error al ejecutar el decodificador MIB. Se continua con la siguiente celda.\n");
        continue;
      }
      if (ret != SRSRAN_UE_MIB_FOUND || cell.nof_ports == 0) {
        printf("  MIB no decodificado para PCI=%d.\n", cell.id);
        continue;
      }

      // Si la celda ya se vio en este EARFCN: se salta solo si su SIB1 ya salio bien.
      // Si no, se reintenta aqui (otro desplazamiento del barrido puede ser el bueno).
      int previa = -1;
      for (uint32_t k = 0; k < n_found_cells; ++k) {
        if (results[k].dl_earfcn == channels[freq].id &&
            results[k].cell.id == cell.id) {
          previa = (int)k;
          break;
        }
      }
      if (previa >= 0 && sib_ok_de[previa]) {
        printf("  Celda PCI=%d ya decodificada en EARFCN=%d.\n", cell.id, channels[freq].id);
        continue;
      }
      if (previa >= 0) {
        printf("  Celda PCI=%d sin SIB1 en el intento anterior: se reintenta con este desplazamiento.\n",
               cell.id);
      }

      if (previa < 0 && n_found_cells >= 1024) {
        printf("Se alcanzo el limite de 1024 celdas.\n");
        go_exit = true;
        break;
      }

      cells current = {};
      current.cell = cell;
      current.freq = rf_freq_hz / MHZ;
      current.dl_earfcn = channels[freq].id;
      current.power = found_cells[i].peak;
      current.cfo = search_cell_cfo;
      int idx_res = (previa >= 0) ? previa : (int)n_found_cells++;
      results[idx_res] = current;

      printf("  MIB OK: PCI=%d, %u PRB, %u puertos, CFO=%+.1f Hz\n",
             cell.id, cell.nof_prb, cell.nof_ports, search_cell_cfo);

      decoded_cell_info candidate = {};
      bool              sib_ok    = false;
      for (int pass = 1; pass <= 2 && !go_exit && !sib_ok && cell.frame_type != SRSRAN_TDD; ++pass) {
        if (pass > 1) {
          printf("  Reintento completo (resincronizando)...\n");
        }
        candidate = decoded_cell_info();
        sib_ok    = decode_sib1_for_cell(&rf, current, &candidate);
      }

      sib_ok_de[idx_res] = sib_ok;
      if (dec_de[idx_res] >= 0) {
        decoded_cells[dec_de[idx_res]] = candidate;  // reemplaza el intento fallido
        print_cell_box(&candidate, dec_de[idx_res] + 1);
      } else if (n_decoded_cells < 1024) {
        dec_de[idx_res]                 = (int)n_decoded_cells;
        decoded_cells[n_decoded_cells++] = candidate;
        print_cell_box(&candidate, n_decoded_cells);
      }

      if (!sib_ok && !go_exit) {
        printf("  PCI=%d: SIB1 no decodificado. Continuando.\n", cell.id);
      }
    }
  }

  srsran_ue_cellsearch_free(&cs);

  printf("\n========== FIN DEL ESCANEO LTE ==========");
  if (go_exit) {
    printf("\nInterrupcion solicitada por Ctrl+C. Se muestran los resultados acumulados.\n");
  } else {
    printf("\nBarrido de frecuencias completado.\n");
  }
  printf("Celdas con MIB decodificado: %u\n", n_found_cells);
  print_decoded_cell_summary(decoded_cells, n_decoded_cells);
  printf("\nBye\n");

  srsran_rf_close(&rf);
  exit(0);
}
