// Canlı koç: aktif oturumun turn akışına bakıp *o an* eyleme dönüşebilir
// öneriler üretir.
//
// Tasarım ilkesi — dürüstlük:
//   Geriye dönük kurallar (rules/*.py) "şunu yapsaydın şu kadar kazanırdın"
//   diyen karşı-olgusal tahminler üretir; bunlar doğaları gereği abartmaya
//   açıktır. Buradaki kurallar bilerek öyle çalışmaz: yalnızca *doğrudan
//   ölçülen* büyüklükleri raporlar (mevcut bağlamın turn başına okuma
//   maliyeti, gerçekleşen harcama hızı, üst üste hata sayısı). Kullanıcıya
//   "şu kadar israf ettin" değil, "şu an durum bu" denir.
//
// Saf JS, bağımlılıksız: Python'a gitmez, her turn'de anında çalışır.

'use strict';

const { canonical, PRICING } = require('./pricing');

const CACHE_READ_FACTOR = 0.1;

// clear_now: bağlam eşiği aşmışken, son STALE_WINDOW turn boyunca bağlamın
// %JUMP_RATIO'sundan büyük yeni içerik gelmediyse "taşıma modundayız" demektir.
const STALE_WINDOW = 8;
const JUMP_RATIO = 0.1;

// error_loop: bu kadar ardışık API hatası bir döngü sayılır.
const ERROR_STREAK = 3;

// cost_velocity: son bu kadar dakikaya bakılır; anlamlı olması için en az
// MIN_VELOCITY_TURNS turn ve MIN_VELOCITY_HOURS süre gerekir.
const VELOCITY_WINDOW_MS = 15 * 60 * 1000;
const MIN_VELOCITY_TURNS = 5;
const MIN_VELOCITY_HOURS = 0.05; // ~3 dk

/**
 * Mevcut bağlamı bir kez daha okumanın turn başına maliyeti ($).
 * Sürmekte olan bir konuşmada geçmiş, cache *read* olarak faturalanır
 * (input fiyatının 0.1 katı). Model bilinmiyorsa null.
 */
function carryCostPerTurn(model, contextTokens) {
  const key = canonical(model);
  if (!key || !contextTokens) return null;
  return (contextTokens * PRICING[key].input * CACHE_READ_FACTOR) / 1_000_000;
}

function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + 'K';
  return String(n || 0);
}

/** Sondan başlayarak ardışık hata turn'lerini sayar. */
function trailingErrors(history) {
  let n = 0;
  for (let i = history.length - 1; i >= 0 && history[i].isError; i--) n++;
  return n;
}

/**
 * Bağlam büyük ama son turn'lerde kayda değer yeni içerik gelmiyorsa true.
 * "Yeni içerik" = bir önceki turn'e göre bağlamın %10'undan büyük sıçrama.
 */
function isStale(history, contextTokens) {
  const w = history.slice(-(STALE_WINDOW + 1));
  if (w.length < STALE_WINDOW + 1) return false;
  const threshold = contextTokens * JUMP_RATIO;
  for (let i = 1; i < w.length; i++) {
    if (w[i].contextTokens - w[i - 1].contextTokens >= threshold) return false;
  }
  return true;
}

/** Son VELOCITY_WINDOW_MS içindeki gerçekleşen harcama hızı ($/saat); yoksa null. */
function burnRateUsdPerHour(history) {
  if (history.length < MIN_VELOCITY_TURNS) return null;
  const last = history[history.length - 1];
  const lastMs = Date.parse(last.ts);
  if (!lastMs) return null;

  const recent = history.filter((t) => {
    const ms = Date.parse(t.ts);
    return ms && lastMs - ms <= VELOCITY_WINDOW_MS;
  });
  if (recent.length < MIN_VELOCITY_TURNS) return null;

  const firstMs = Date.parse(recent[0].ts);
  const hours = (lastMs - firstMs) / 3_600_000;
  if (hours < MIN_VELOCITY_HOURS) return null;

  // İlk turn penceresinin başlangıç noktası: maliyeti ona ait değil, aradaki
  // farkı sayarız.
  const spent = recent.slice(1).reduce((a, t) => a + (t.costUsd || 0), 0);
  return spent / hours;
}

/**
 * Aktif oturum istatistiklerinden canlı önerileri üretir.
 *
 * @param {{contextTokens: number, model: string|null, turns: number,
 *          costUsd: number, history: Array<{ts: string, contextTokens: number,
 *          costUsd: number, isError: boolean}>}} stats
 * @param {{warn: number, danger: number, burnRateUsdPerHour: number}} opts
 * @returns {{suggestions: Array<{id: string, severity: 'info'|'warn'|'danger',
 *            title: string, detail: string}>, carryPerTurn: number|null,
 *            burnRate: number|null}}
 */
function analyze(stats, opts) {
  const suggestions = [];
  const history = (stats && stats.history) || [];
  const ctx = (stats && stats.contextTokens) || 0;
  const carryPerTurn = carryCostPerTurn(stats && stats.model, ctx);
  const burnRate = burnRateUsdPerHour(history);

  if (!history.length) return { suggestions, carryPerTurn, burnRate };

  // 1) clear_now — bağlam büyük ve artık yeni bilgi gelmiyor.
  if (ctx >= opts.warn && isStale(history, ctx)) {
    const danger = ctx >= opts.danger;
    const cost = carryPerTurn
      ? ` Her yeni turn, sırf bu geçmişi yeniden okumak için ~$${carryPerTurn.toFixed(3)}.`
      : '';
    suggestions.push({
      id: 'clear_now',
      severity: danger ? 'danger' : 'warn',
      title: '/clear için iyi an',
      detail:
        `Bağlam ${fmtTokens(ctx)} ve son ${STALE_WINDOW} turn'de kayda değer yeni ` +
        `içerik gelmedi — taşıma modundasın.${cost} Konu değiştiyse /clear.`,
    });
  }

  // 2) error_loop — aynı duvara toslamak turn yakar.
  const errs = trailingErrors(history);
  if (errs >= ERROR_STREAK) {
    suggestions.push({
      id: 'error_loop',
      severity: 'warn',
      title: 'Hata döngüsü',
      detail:
        `Son ${errs} turn üst üste hata verdi. Aynı yaklaşımı tekrarlamak ` +
        `token yakar — sorunu daraltmayı ya da yöntemi değiştirmeyi dene.`,
    });
  }

  // 3) cost_velocity — gerçekleşen harcama hızı.
  if (burnRate !== null && burnRate >= opts.burnRateUsdPerHour) {
    suggestions.push({
      id: 'cost_velocity',
      severity: 'info',
      title: 'Harcama hızı yüksek',
      detail:
        `Son 15 dakikada saatte ~$${burnRate.toFixed(2)} hızındasın. ` +
        `İş basitleştiyse /model ile daha ucuz bir modele geçmek mantıklı olabilir.`,
    });
  }

  return { suggestions, carryPerTurn, burnRate };
}

module.exports = {
  analyze,
  carryCostPerTurn,
  burnRateUsdPerHour,
  isStale,
  trailingErrors,
  STALE_WINDOW,
  ERROR_STREAK,
};
