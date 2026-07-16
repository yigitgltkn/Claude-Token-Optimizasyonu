// SessionWatcher._apply — JSONL kaydı -> istatistik/geçmiş dönüşümü.
//   cd vscode-extension && node --test

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { SessionWatcher, slugKey } = require('../src/sessionWatcher');

/** Geçmişi izlenebilir, boş bir watcher kurar (dosya sistemine dokunmaz). */
function fresh() {
  const w = new SessionWatcher('/yok', () => {});
  w.stats = w._freshStats('x.jsonl');
  return w;
}

function assistantRec({ ts = '2026-07-16T12:00:00.000Z', model = 'claude-opus-4-8', usage, extra = {} }) {
  return { type: 'assistant', timestamp: ts, message: { model, usage }, ...extra };
}

const USAGE = { input_tokens: 100, cache_read_input_tokens: 50000, cache_creation_input_tokens: 0, output_tokens: 200 };

test('assistant turn: bağlam = input + cache_read + cache_creation', () => {
  const w = fresh();
  assert.strictEqual(w._apply(assistantRec({ usage: USAGE })), true);
  assert.strictEqual(w.stats.contextTokens, 50100);
  assert.strictEqual(w.stats.turns, 1);
  assert.strictEqual(w.stats.history.length, 1);
  assert.strictEqual(w.stats.history[0].isError, false);
  assert.ok(w.stats.costUsd > 0);
});

test('sidechain (subagent) turn sayılmaz', () => {
  const w = fresh();
  assert.strictEqual(w._apply(assistantRec({ usage: USAGE, extra: { isSidechain: true } })), false);
  assert.strictEqual(w.stats.history.length, 0);
});

test('user kaydı yok sayılır', () => {
  const w = fresh();
  assert.strictEqual(w._apply({ type: 'user', timestamp: '2026-07-16T12:00:00.000Z' }), false);
  assert.strictEqual(w.stats.history.length, 0);
});

test('usage taşımayan API hatası: geçmişe hata olarak yazılır, maliyete katılmaz', () => {
  const w = fresh();
  w._apply(assistantRec({ usage: USAGE }));
  const costBefore = w.stats.costUsd;
  const turnsBefore = w.stats.turns;

  const ok = w._apply({
    type: 'assistant',
    timestamp: '2026-07-16T12:01:00.000Z',
    isApiErrorMessage: true,
    message: { model: '<synthetic>' },
  });

  assert.strictEqual(ok, true, 'hata kaydı işlenmeliydi');
  assert.strictEqual(w.stats.history.length, 2);
  assert.strictEqual(w.stats.history[1].isError, true);
  assert.strictEqual(w.stats.history[1].costUsd, 0, 'hata turn maliyete katılmamalı');
  assert.strictEqual(w.stats.costUsd, costBefore, 'maliyet değişmemeli');
  assert.strictEqual(w.stats.turns, turnsBefore, 'hata turn sayılmamalı');
  // bağlam son bilinen değeri korur
  assert.strictEqual(w.stats.history[1].contextTokens, 50100);
});

test('record.error alanı da hata sayılır (ingest.py ile aynı ölçüt)', () => {
  const w = fresh();
  const ok = w._apply({
    type: 'assistant',
    timestamp: '2026-07-16T12:00:00.000Z',
    error: { message: 'overloaded' },
    message: { model: '<synthetic>' },
  });
  assert.strictEqual(ok, true);
  assert.strictEqual(w.stats.history[0].isError, true);
});

test('hatasız synthetic/usage-suz kayıt yok sayılır', () => {
  const w = fresh();
  assert.strictEqual(w._apply(assistantRec({ model: '<synthetic>', usage: USAGE })), false);
  assert.strictEqual(w._apply(assistantRec({ usage: undefined })), false);
  assert.strictEqual(w.stats.history.length, 0);
});

test('usage taşıyan hata turn: maliyete katılır ama hata işaretlenir', () => {
  const w = fresh();
  w._apply(assistantRec({ usage: USAGE, extra: { isApiErrorMessage: true } }));
  assert.strictEqual(w.stats.history[0].isError, true);
  assert.ok(w.stats.costUsd > 0, 'gerçek usage varsa maliyet sayılmalı');
});

test('geçmiş MAX_HISTORY ile sınırlı (bellek sızmaz), son turn korunur', () => {
  const w = fresh();
  for (let i = 0; i < 350; i++) {
    w._apply(assistantRec({ ts: new Date(Date.UTC(2026, 6, 16, 12, 0, i)).toISOString(), usage: USAGE }));
  }
  assert.strictEqual(w.stats.history.length, 300);
  assert.strictEqual(w.stats.turns, 350, 'turn sayacı kırpılmamalı');
  assert.strictEqual(w.stats.history.at(-1).ts, '2026-07-16T12:05:49.000Z');
});

test('slugKey: proje yolu -> Claude Code klasör anahtarı', () => {
  assert.strictEqual(
    slugKey('C:\\Users\\Yigit\\Desktop\\Project\\Token-Coach'),
    slugKey('c--users-yigit-desktop-project-token-coach')
  );
});
