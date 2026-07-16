// Model fiyat tablosu (USD / milyon token) — rules/pricing.py'ın JS aynası.
// Cache okuma 0.1x input; cache yazma 1.25x (5dk TTL) / 2x (1saat TTL).
// Claude Code 1 saatlik TTL ile yazar; 5dk/1saat ayrımı yoksa 1saat varsayılır.

'use strict';

const PRICING = {
  'claude-fable-5': { input: 10.0, output: 50.0 },
  'claude-mythos-5': { input: 10.0, output: 50.0 },
  'claude-opus-4-8': { input: 5.0, output: 25.0 },
  'claude-opus-4-7': { input: 5.0, output: 25.0 },
  'claude-opus-4-6': { input: 5.0, output: 25.0 },
  'claude-opus-4-5': { input: 5.0, output: 25.0 },
  'claude-opus-4-1': { input: 5.0, output: 25.0 },
  'claude-sonnet-5': { input: 3.0, output: 15.0 },
  'claude-sonnet-4-6': { input: 3.0, output: 15.0 },
  'claude-sonnet-4-5': { input: 3.0, output: 15.0 },
  'claude-haiku-4-5': { input: 1.0, output: 5.0 },
};

const CACHE_READ_FACTOR = 0.1;
const CACHE_WRITE_5M_FACTOR = 1.25;
const CACHE_WRITE_1H_FACTOR = 2.0;

/** Model ID'sini (tarih ekli olabilir) PRICING anahtarına indirger; bilinmiyorsa null. */
function canonical(model) {
  if (!model) return null;
  for (const known of Object.keys(PRICING)) {
    if (model === known || model.startsWith(known + '-')) return known;
  }
  return null;
}

/**
 * Bir assistant kaydının `message.usage` objesini modelin tarifesiyle
 * fiyatlandırır. Bilinmeyen model için null döner.
 */
function turnCostUsd(model, usage) {
  const key = canonical(model);
  if (!key || !usage) return null;
  const p = PRICING[key];

  const detail = usage.cache_creation || {};
  let cache5m = detail.ephemeral_5m_input_tokens || 0;
  let cache1h = detail.ephemeral_1h_input_tokens || 0;
  const cacheCreation = usage.cache_creation_input_tokens || 0;
  if (cache5m + cache1h === 0 && cacheCreation) cache1h = cacheCreation;

  const usd =
    (usage.input_tokens || 0) * p.input +
    (usage.output_tokens || 0) * p.output +
    (usage.cache_read_input_tokens || 0) * p.input * CACHE_READ_FACTOR +
    cache5m * p.input * CACHE_WRITE_5M_FACTOR +
    cache1h * p.input * CACHE_WRITE_1H_FACTOR;
  return usd / 1_000_000;
}

module.exports = { PRICING, canonical, turnCostUsd };
