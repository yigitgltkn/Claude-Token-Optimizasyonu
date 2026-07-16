// Canlı koç kuralları — node --test ile çalışır (bağımlılık yok).
//   cd vscode-extension && node --test

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const {
  analyze,
  carryCostPerTurn,
  burnRateUsdPerHour,
  isStale,
  trailingErrors,
  STALE_WINDOW,
} = require('../src/liveCoach');

const OPTS = { warn: 120000, danger: 180000, burnRateUsdPerHour: 10 };

/** ctx dizisinden turn geçmişi kurar; ts'ler 1'er dakika arayla. */
function history(contexts, { costUsd = 0.1, errors = [], startMs = Date.UTC(2026, 6, 16, 12, 0, 0) } = {}) {
  return contexts.map((contextTokens, i) => ({
    ts: new Date(startMs + i * 60_000).toISOString(),
    contextTokens,
    costUsd,
    isError: errors.includes(i),
  }));
}

/** Yavaş büyüyen (bayat) bir bağlam serisi: turn başına +1K. */
function stalePlateau(base, n) {
  return Array.from({ length: n }, (_, i) => base + i * 1000);
}

test('carryCostPerTurn: cache-read 0.1x uygular', () => {
  // opus-4-8 input $5/M → 100K bağlam = 100000 * 5 * 0.1 / 1e6 = $0.05
  assert.strictEqual(carryCostPerTurn('claude-opus-4-8', 100000), 0.05);
  // sonnet-5 input $3/M → 100K = $0.03
  assert.ok(Math.abs(carryCostPerTurn('claude-sonnet-5', 100000) - 0.03) < 1e-9);
});

test('carryCostPerTurn: bilinmeyen model ve sıfır bağlam null', () => {
  assert.strictEqual(carryCostPerTurn('gpt-yok', 100000), null);
  assert.strictEqual(carryCostPerTurn('claude-opus-4-8', 0), null);
});

test('carryCostPerTurn: tarih ekli model ID tanınır', () => {
  assert.strictEqual(carryCostPerTurn('claude-opus-4-8-20260101', 100000), 0.05);
});

test('trailingErrors: yalnızca sondan ardışık olanları sayar', () => {
  assert.strictEqual(trailingErrors(history([1, 2, 3, 4], { errors: [1, 2, 3] })), 3);
  // araya temiz bir turn girerse seri kırılır
  assert.strictEqual(trailingErrors(history([1, 2, 3, 4], { errors: [0, 1] })), 0);
  assert.strictEqual(trailingErrors([]), 0);
});

test('isStale: yavaş büyüyen uzun seri bayattır', () => {
  const h = history(stalePlateau(150000, STALE_WINDOW + 1));
  assert.strictEqual(isStale(h, 158000), true);
});

test('isStale: büyük sıçrama varsa bayat değil (yeni bilgi geldi)', () => {
  const ctxs = stalePlateau(150000, STALE_WINDOW + 1);
  ctxs[ctxs.length - 1] += 30000; // %10'dan büyük sıçrama
  assert.strictEqual(isStale(history(ctxs), 188000), false);
});

test('isStale: pencere dolmadan karar vermez', () => {
  assert.strictEqual(isStale(history(stalePlateau(150000, 3)), 152000), false);
});

test('analyze: bayat + eşik üstü → clear_now', () => {
  const stats = {
    contextTokens: 158000,
    model: 'claude-opus-4-8',
    history: history(stalePlateau(150000, STALE_WINDOW + 1)),
  };
  const { suggestions, carryPerTurn } = analyze(stats, OPTS);
  const s = suggestions.find((x) => x.id === 'clear_now');
  assert.ok(s, 'clear_now bekleniyordu');
  assert.strictEqual(s.severity, 'warn');
  assert.match(s.detail, /taşıma modundasın/);
  // dürüstlük: turn başına taşıma maliyeti gerçek hesaptan gelir
  assert.ok(Math.abs(carryPerTurn - 0.079) < 0.001);
  assert.match(s.detail, /\$0\.079/);
});

test('analyze: 180K üstünde clear_now danger olur', () => {
  const stats = {
    contextTokens: 190000,
    model: 'claude-opus-4-8',
    history: history(stalePlateau(182000, STALE_WINDOW + 1)),
  };
  const s = analyze(stats, OPTS).suggestions.find((x) => x.id === 'clear_now');
  assert.strictEqual(s.severity, 'danger');
});

test('analyze: eşik altında bayat olsa da clear_now yok', () => {
  const stats = {
    contextTokens: 50000,
    model: 'claude-opus-4-8',
    history: history(stalePlateau(42000, STALE_WINDOW + 1)),
  };
  assert.strictEqual(
    analyze(stats, OPTS).suggestions.find((x) => x.id === 'clear_now'),
    undefined
  );
});

test('analyze: eşik üstü ama yeni içerik geliyorsa clear_now yok (gürültü olmasın)', () => {
  const ctxs = stalePlateau(150000, STALE_WINDOW + 1);
  ctxs[ctxs.length - 2] += 40000;
  const stats = {
    contextTokens: 198000,
    model: 'claude-opus-4-8',
    history: history(ctxs),
  };
  assert.strictEqual(
    analyze(stats, OPTS).suggestions.find((x) => x.id === 'clear_now'),
    undefined
  );
});

test('analyze: 3 ardışık hata → error_loop', () => {
  const stats = {
    contextTokens: 10000,
    model: 'claude-opus-4-8',
    history: history([1000, 2000, 3000, 4000, 5000], { errors: [2, 3, 4] }),
  };
  const s = analyze(stats, OPTS).suggestions.find((x) => x.id === 'error_loop');
  assert.ok(s);
  assert.match(s.detail, /Son 3 turn/);
});

test('analyze: 2 ardışık hata yetmez', () => {
  const stats = {
    contextTokens: 10000,
    model: 'claude-opus-4-8',
    history: history([1000, 2000, 3000], { errors: [1, 2] }),
  };
  assert.strictEqual(
    analyze(stats, OPTS).suggestions.find((x) => x.id === 'error_loop'),
    undefined
  );
});

test('burnRate: gerçekleşen harcamadan hesaplanır', () => {
  // 6 turn, 1 dk arayla (5 dk aralık), ilki hariç 5 turn × $1 = $5 → $60/saat
  const h = history([1, 2, 3, 4, 5, 6], { costUsd: 1 });
  const rate = burnRateUsdPerHour(h);
  assert.ok(Math.abs(rate - 60) < 0.001, `beklenen ~60, gelen ${rate}`);
});

test('burnRate: az turn varsa null (erken karar yok)', () => {
  assert.strictEqual(burnRateUsdPerHour(history([1, 2], { costUsd: 1 })), null);
});

test('burnRate: 15 dk penceresi dışındaki turn sayılmaz', () => {
  // 5 turn 1 saat önce + 5 turn şimdi → yalnızca son grup sayılmalı
  const old = history([1, 2, 3, 4, 5], { costUsd: 99, startMs: Date.UTC(2026, 6, 16, 10, 0, 0) });
  const recent = history([6, 7, 8, 9, 10], { costUsd: 1, startMs: Date.UTC(2026, 6, 16, 12, 0, 0) });
  const rate = burnRateUsdPerHour([...old, ...recent]);
  // son 5 turn: ilki hariç 4 × $1 = $4 / 4dk = $60/saat ($99'lar sızmamalı)
  assert.ok(rate < 100, `eski pahalı turn'ler sızdı: ${rate}`);
  assert.ok(Math.abs(rate - 60) < 0.001);
});

test('analyze: yüksek harcama hızı → cost_velocity', () => {
  const stats = {
    contextTokens: 10000,
    model: 'claude-opus-4-8',
    history: history([1, 2, 3, 4, 5, 6], { costUsd: 1 }), // ~$60/saat
  };
  const s = analyze(stats, OPTS).suggestions.find((x) => x.id === 'cost_velocity');
  assert.ok(s);
  assert.strictEqual(s.severity, 'info');
});

test('analyze: düşük hızda cost_velocity yok', () => {
  const stats = {
    contextTokens: 10000,
    model: 'claude-opus-4-8',
    history: history([1, 2, 3, 4, 5, 6], { costUsd: 0.001 }),
  };
  assert.strictEqual(
    analyze(stats, OPTS).suggestions.find((x) => x.id === 'cost_velocity'),
    undefined
  );
});

test('analyze: boş geçmişte çökmez, öneri üretmez', () => {
  const r = analyze({ contextTokens: 0, model: null, history: [] }, OPTS);
  assert.deepStrictEqual(r.suggestions, []);
  assert.strictEqual(r.carryPerTurn, null);
});

test('analyze: eksik/bozuk stats ile çökmez', () => {
  assert.deepStrictEqual(analyze({}, OPTS).suggestions, []);
  assert.deepStrictEqual(analyze(null, OPTS).suggestions, []);
});

test('analyze: bilinmeyen modelde clear_now yine çıkar, maliyet cümlesi düşer', () => {
  const stats = {
    contextTokens: 158000,
    model: 'bilinmeyen-model',
    history: history(stalePlateau(150000, STALE_WINDOW + 1)),
  };
  const { suggestions, carryPerTurn } = analyze(stats, OPTS);
  const s = suggestions.find((x) => x.id === 'clear_now');
  assert.ok(s);
  assert.strictEqual(carryPerTurn, null);
  assert.doesNotMatch(s.detail, /\$/, 'model bilinmiyorsa uydurma maliyet yazılmamalı');
});
