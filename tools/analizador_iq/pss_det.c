/*
 * pss_det.c - Nucleo rapido de deteccion PSS para analizar_grabacion.py
 *
 * Por cada trama (FFT de N muestras de la grabacion, ya calculada en Python) y
 * por cada canal (EARFCN + hipotesis Doppler) x N_id2:
 *   Z = X[bins del canal] * conj(PSS)  ->  IFFT de M puntos  ->  |.|^2
 * y acumula la potencia "doblada" con periodo de 5 ms (integracion no coherente).
 * Al completar cada ventana calcula el PSR (pico / maximo lateral) y su posicion.
 *
 * Paraleliza por canal con OpenMP; usa FFTW (la misma libreria FFT que srsRAN).
 * Compilar: gcc -O3 -march=native -fopenmp -shared -fPIC pss_det.c -o libpss_det.so -lfftw3f -lm
 */
#include <complex.h>
#include <fftw3.h>
#include <math.h>
#include <omp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
  int            N, M, C, nval, period, W, excl;
  int*           koff;  /* C: bin central de cada canal (0..N-1) */
  int*           mapa;  /* M: desplazamiento de bin para cada posicion de la IFFT (orden FFT) */
  float complex* P;     /* 3*M: conj(PSS) en orden FFT */
  float*         acc;   /* C*3*period */
  int64_t        g;     /* indice global de muestra de deteccion */
  fftwf_plan     plan;
  int            nth;
  float complex** Z;    /* buffers por hilo */
  float complex** Y;
} det_t;

void* det_crear(int N, int M, int C, const int* koff, const float complex* P, int nval, int period, int K,
                int excl, int nth)
{
  det_t* d = calloc(1, sizeof(det_t));
  d->N = N, d->M = M, d->C = C, d->nval = nval, d->period = period, d->W = period * K, d->excl = excl;
  d->koff = malloc(sizeof(int) * C);
  memcpy(d->koff, koff, sizeof(int) * C);
  d->mapa = malloc(sizeof(int) * M);
  for (int m = 0; m < M; m++)
    d->mapa[m] = (m < M / 2) ? m : m - M;
  d->P = fftwf_malloc(sizeof(float complex) * 3 * M);
  memcpy(d->P, P, sizeof(float complex) * 3 * M);
  d->acc = calloc((size_t)C * 3 * period, sizeof(float));
  d->nth = nth > 0 ? nth : omp_get_max_threads();
  d->Z   = malloc(sizeof(void*) * d->nth);
  d->Y   = malloc(sizeof(void*) * d->nth);
  for (int t = 0; t < d->nth; t++) {
    d->Z[t] = fftwf_malloc(sizeof(float complex) * M);
    d->Y[t] = fftwf_malloc(sizeof(float complex) * M);
  }
  d->plan = fftwf_plan_dft_1d(M, (fftwf_complex*)d->Z[0], (fftwf_complex*)d->Y[0], FFTW_BACKWARD, FFTW_MEASURE);
  d->g    = 0;
  return d;
}

static void cerrar_ventana(det_t* d, const float* a, float* psr, int* pico)
{
  int   p  = 0;
  float pk = a[0];
  for (int i = 1; i < d->period; i++)
    if (a[i] > pk)
      pk = a[i], p = i;
  float sl = 1e-30f;
  for (int i = 0; i < d->period; i++) {
    int dist = abs(i - p);
    if (dist > d->period / 2)
      dist = d->period - dist;
    if (dist > d->excl && a[i] > sl)
      sl = a[i];
  }
  *psr  = pk / sl;
  *pico = p;
}

/* Procesa una trama. X: N bins (FFT de la trama). Devuelve cuantas ventanas se cerraron
 * (0..maxv) y deja sus PSR/picos en out_psr/out_pico con forma [ventana][C*3]. */
int det_trama(void* h, const float complex* X, float* out_psr, int* out_pico, int maxv)
{
  det_t*  d  = h;
  int64_t g0 = d->g;
  int     nv = 0;
  for (int64_t s = g0 + 1; s <= g0 + d->nval; s++)
    if (s % d->W == 0)
      nv++;
  if (nv > maxv)
    nv = maxv;

#pragma omp parallel for schedule(dynamic, 1) num_threads(d->nth)
  for (int cn = 0; cn < d->C * 3; cn++) {
    int            t   = omp_get_thread_num();
    int            c   = cn / 3, n = cn % 3;
    float complex* Z   = d->Z[t];
    float complex* Y   = d->Y[t];
    const float complex* P = d->P + (size_t)n * d->M;
    int            k   = d->koff[c];
    for (int m = 0; m < d->M; m++) {
      int b = k + d->mapa[m];
      if (b < 0)
        b += d->N;
      else if (b >= d->N)
        b -= d->N;
      Z[m] = X[b] * P[m];
    }
    fftwf_execute_dft(d->plan, (fftwf_complex*)Z, (fftwf_complex*)Y);
    float* a   = d->acc + (size_t)cn * d->period;
    int    lag = (int)(g0 % d->period);
    int    w   = 0;
    int    falta = d->W - (int)(g0 % d->W); /* muestras hasta cerrar la ventana */
    int    j   = 0;
    const float* yf = (const float*)Y;
    while (j < d->nval) {
      int tramo = d->nval - j;
      if (tramo > d->period - lag) tramo = d->period - lag;
      if (tramo > falta) tramo = falta;
      float* ap = a + lag;
      const float* yp = yf + 2 * j;
      for (int i = 0; i < tramo; i++)
        ap[i] += yp[2 * i] * yp[2 * i] + yp[2 * i + 1] * yp[2 * i + 1];
      j += tramo; lag += tramo; falta -= tramo;
      if (lag == d->period) lag = 0;
      if (falta == 0) {
        if (w < nv)
          cerrar_ventana(d, a, &out_psr[(size_t)w * d->C * 3 + cn], &out_pico[(size_t)w * d->C * 3 + cn]);
        w++;
        memset(a, 0, sizeof(float) * d->period);
        falta = d->W;
      }
    }
  }
  d->g += d->nval;
  return nv;
}

void det_liberar(void* h)
{
  det_t* d = h;
  if (!d)
    return;
  fftwf_destroy_plan(d->plan);
  for (int t = 0; t < d->nth; t++) {
    fftwf_free(d->Z[t]);
    fftwf_free(d->Y[t]);
  }
  free(d->Z), free(d->Y), fftwf_free(d->P), free(d->acc), free(d->koff), free(d->mapa), free(d);
}
