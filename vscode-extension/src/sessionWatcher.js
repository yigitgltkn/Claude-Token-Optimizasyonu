// Aktif Claude Code oturumunun JSONL logunu canlı izler.
//
// Claude Code, her projenin loglarını ~/.claude/projects/<slug>/*.jsonl
// altında tutar (slug: proje yolundaki alfanümerik olmayan karakterler '-'
// olur). Bu modül çalışma alanına karşılık gelen log klasörünü bulur, en son
// değişen .jsonl dosyasını "aktif oturum" sayar ve dosyayı offset tabanlı
// artımlı okur: her değişiklikte yalnızca yeni eklenen satırlar ayrıştırılır
// (JSONL append-only olduğu için bedava artımlılık).

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { turnCostUsd } = require('./pricing');

/** Yol -> Claude Code proje slug karşılaştırma anahtarı. */
function slugKey(p) {
  return p.replace(/[^a-zA-Z0-9-]/g, '-').toLowerCase();
}

/**
 * Çalışma alanı yoluna karşılık gelen ~/.claude/projects/<slug> klasörünü
 * bulur; yoksa null.
 */
function claudeProjectDirFor(workspacePath) {
  const root = path.join(os.homedir(), '.claude', 'projects');
  let names;
  try {
    names = fs.readdirSync(root);
  } catch {
    return null;
  }
  const want = slugKey(workspacePath);
  for (const name of names) {
    if (slugKey(name) === want) return path.join(root, name);
  }
  return null;
}

class SessionWatcher {
  /**
   * @param {string} projectDir ~/.claude/projects/<slug>
   * @param {(stats: object) => void} onUpdate her yeni turn sonrası çağrılır
   */
  constructor(projectDir, onUpdate) {
    this.dir = projectDir;
    this.onUpdate = onUpdate;
    this.activeFile = null;
    this.offset = 0;
    this.partial = '';
    this.stats = null;
    this.watcher = null;
    this.timer = null;
  }

  start() {
    this._scan();
    try {
      this.watcher = fs.watch(this.dir, () => {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => this._scan(), 500);
      });
    } catch {
      // klasör izlenemiyorsa ilk okuma yine de yapılmış olur
    }
  }

  dispose() {
    clearTimeout(this.timer);
    if (this.watcher) this.watcher.close();
  }

  _freshStats(file) {
    return {
      file,
      model: null,
      contextTokens: 0,
      outputTokens: 0,
      turns: 0,
      costUsd: 0,
      lastTs: null,
    };
  }

  _newestJsonl() {
    let best = null;
    let bestMtime = -1;
    let names;
    try {
      names = fs.readdirSync(this.dir);
    } catch {
      return null;
    }
    for (const name of names) {
      if (!name.endsWith('.jsonl')) continue;
      const full = path.join(this.dir, name);
      let mtime;
      try {
        mtime = fs.statSync(full).mtimeMs;
      } catch {
        continue;
      }
      if (mtime > bestMtime) {
        bestMtime = mtime;
        best = full;
      }
    }
    return best;
  }

  _scan() {
    const newest = this._newestJsonl();
    if (!newest) return;
    if (newest !== this.activeFile) {
      // yeni oturum başladı — durumu sıfırla, dosyayı baştan oku
      this.activeFile = newest;
      this.offset = 0;
      this.partial = '';
      this.stats = this._freshStats(newest);
    }
    this._readIncremental();
  }

  _readIncremental() {
    let size;
    try {
      size = fs.statSync(this.activeFile).size;
    } catch {
      return;
    }
    if (size < this.offset) {
      // dosya kısalmış (beklenmez ama) — baştan oku
      this.offset = 0;
      this.partial = '';
      this.stats = this._freshStats(this.activeFile);
    }
    if (size === this.offset) return;

    let fd;
    try {
      fd = fs.openSync(this.activeFile, 'r');
    } catch {
      return;
    }
    let text;
    try {
      const buf = Buffer.alloc(size - this.offset);
      fs.readSync(fd, buf, 0, buf.length, this.offset);
      text = buf.toString('utf8');
    } finally {
      fs.closeSync(fd);
    }
    this.offset = size;

    const lines = (this.partial + text).split('\n');
    this.partial = lines.pop(); // son satır yarım olabilir — sonraki okumada tamamlanır

    let changed = false;
    for (const line of lines) {
      if (!line.trim()) continue;
      let rec;
      try {
        rec = JSON.parse(line);
      } catch {
        continue;
      }
      if (this._apply(rec)) changed = true;
    }
    if (changed) this.onUpdate(this.stats);
  }

  /** Bir JSONL kaydını istatistiklere işler; assistant+usage ise true döner. */
  _apply(rec) {
    if (rec.type !== 'assistant' || rec.isSidechain) return false;
    const msg = rec.message || {};
    const usage = msg.usage;
    const model = msg.model;
    if (!usage || !model || model === '<synthetic>') return false;

    const s = this.stats;
    s.model = model;
    s.contextTokens =
      (usage.input_tokens || 0) +
      (usage.cache_read_input_tokens || 0) +
      (usage.cache_creation_input_tokens || 0);
    s.outputTokens += usage.output_tokens || 0;
    s.turns += 1;
    s.costUsd += turnCostUsd(model, usage) || 0;
    s.lastTs = rec.timestamp || s.lastTs;
    return true;
  }
}

module.exports = { SessionWatcher, claudeProjectDirFor, slugKey };
