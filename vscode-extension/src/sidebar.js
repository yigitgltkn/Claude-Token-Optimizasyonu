// Token Coach kenar çubuğu (Activity Bar > webview view).
//
// Tüm yüzeyleri tek yerde toplar: canlı oturum göstergesi, israf özeti ve
// bulgu kartları. Eklenti tarafı yalnızca durum gönderir (postMessage);
// çizimi webview içindeki script yapar — böylece HTML bir kez kurulur,
// her canlı turn'de yeniden üretilmez.
//
// Renkler VS Code tema değişkenlerinden gelir (var(--vscode-*)), bu yüzden
// açık/koyu tema otomatik uyumludur.

'use strict';

const vscode = require('vscode');

/** Kural ID'leri (makine sözleşmesi, İngilizce) -> kullanıcıya görünen Türkçe etiket. */
const RULE_LABELS = {
  stale_context: 'Bayat bağlam',
  cache_efficiency: 'Önbellek verimi',
  model_mismatch: 'Model uyumsuzluğu',
  subagent_overuse: 'Subagent aşırı kullanımı',
  total_size: 'CLAUDE.md boyutu',
  path_scoped_candidate: 'Path-scoped adayı',
  duplicated_line: 'Tekrar eden satır',
};

const SCOPE_LABELS = { session: 'oturum', file: 'dosya' };

function nonce() {
  let s = '';
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  for (let i = 0; i < 32; i++) s += chars.charAt(Math.floor(Math.random() * chars.length));
  return s;
}

class SidebarProvider {
  /**
   * @param {vscode.Uri} extensionUri
   * @param {{onRefresh: () => void, onOpenDashboard: () => void,
   *          onOpenFinding: (file: string, line: number) => void}} actions
   */
  constructor(extensionUri, actions) {
    this.extensionUri = extensionUri;
    this.actions = actions;
    /** @type {vscode.WebviewView | null} */
    this.view = null;
    this.state = {
      session: null,
      /** @type {{suggestions: Array<object>, carryPerTurn: number|null, burnRate: number|null}|null} */
      live: null,
      diagnose: null,
      loading: false,
      error: null,
      warn: 120000,
      danger: 180000,
    };
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };
    webviewView.webview.html = this._html(webviewView.webview);

    webviewView.webview.onDidReceiveMessage((msg) => {
      if (msg.type === 'refresh') this.actions.onRefresh();
      else if (msg.type === 'openDashboard') this.actions.onOpenDashboard();
      else if (msg.type === 'openFinding') this.actions.onOpenFinding(msg.file, msg.line);
      else if (msg.type === 'ready') this._post();
    });

    this._post();
  }

  /** Durumun bir bölümünü günceller ve webview'e yollar. */
  setState(patch) {
    Object.assign(this.state, patch);
    this._post();
  }

  _post() {
    if (!this.view) return;
    this.view.webview.postMessage({ type: 'state', state: this.state });
  }

  _html(webview) {
    const n = nonce();
    const csp =
      `default-src 'none'; ` +
      `style-src ${webview.cspSource} 'unsafe-inline'; ` +
      `script-src 'nonce-${n}';`;

    return `<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  :root {
    --ok: var(--vscode-charts-green, #3fb950);
    --warn: var(--vscode-charts-yellow, #d29922);
    --danger: var(--vscode-charts-red, #f85149);
  }
  body {
    padding: 10px 12px 24px;
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
  }
  h2 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .06em;
    opacity: .65;
    margin: 18px 0 8px;
    font-weight: 600;
  }
  h2:first-child { margin-top: 4px; }

  .card {
    background: var(--vscode-editorWidget-background, rgba(127,127,127,.08));
    border: 1px solid var(--vscode-editorWidget-border, transparent);
    border-radius: 6px;
    padding: 10px 12px;
  }

  /* --- canlı oturum --- */
  .ctx-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .ctx-num { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .ctx-num .unit { font-size: 12px; opacity: .6; font-weight: 400; margin-left: 2px; }
  .ctx-model {
    font-size: 11px; opacity: .75; text-align: right;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .bar {
    height: 5px; border-radius: 3px; margin: 9px 0 8px;
    background: var(--vscode-progressBar-background, rgba(127,127,127,.25));
    overflow: hidden;
  }
  .bar > i { display: block; height: 100%; border-radius: 3px; transition: width .35s ease, background .35s ease; }
  .meta { display: flex; gap: 14px; font-size: 11px; opacity: .7; font-variant-numeric: tabular-nums; }
  .hint { margin-top: 8px; font-size: 11px; line-height: 1.5; color: var(--warn); }
  .hint.danger { color: var(--danger); }
  .carry { margin-top: 8px; font-size: 11px; opacity: .65; line-height: 1.5; }

  /* --- simdi (canli oneriler) --- */
  .sug {
    display: flex; gap: 9px; padding: 10px 12px; margin-bottom: 6px;
    border-radius: 6px; align-items: flex-start;
    background: var(--vscode-editorWidget-background, rgba(127,127,127,.08));
    border: 1px solid transparent;
    border-left: 3px solid var(--vscode-charts-blue, #58a6ff);
  }
  .sug.warn { border-left-color: var(--warn); background: color-mix(in srgb, var(--warn) 8%, transparent); }
  .sug.danger { border-left-color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); }
  .sug-icon { font-size: 14px; line-height: 1.3; flex-shrink: 0; }
  .sug-title { font-size: 12px; font-weight: 600; margin-bottom: 3px; }
  .sug-detail { font-size: 11.5px; line-height: 1.5; opacity: .85; }
  .sug-detail b, .sug-detail code {
    font-family: var(--vscode-editor-font-family);
    background: rgba(127,127,127,.18);
    padding: 0 4px; border-radius: 3px; font-weight: 600;
  }

  /* --- ozet --- */
  .totals { display: flex; gap: 8px; }
  .totals .card { flex: 1; text-align: center; padding: 9px 6px; }
  .totals .v { font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .totals .k { font-size: 10px; opacity: .6; margin-top: 2px; }

  /* --- bulgular --- */
  .finding {
    border-left: 3px solid var(--vscode-editorWidget-border, rgba(127,127,127,.4));
    padding: 8px 10px;
    margin-bottom: 6px;
    border-radius: 0 5px 5px 0;
    background: var(--vscode-editorWidget-background, rgba(127,127,127,.08));
  }
  .finding.clickable { cursor: pointer; }
  .finding.clickable:hover { background: var(--vscode-list-hoverBackground, rgba(127,127,127,.16)); }
  .finding.sev-high { border-left-color: var(--danger); }
  .finding.sev-mid { border-left-color: var(--warn); }
  .f-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 4px; }
  .f-rule { font-size: 11px; font-weight: 600; }
  .f-usd { font-size: 11px; font-variant-numeric: tabular-nums; opacity: .85; white-space: nowrap; }
  .f-msg { font-size: 11.5px; line-height: 1.5; opacity: .9; }
  .f-scope { font-size: 10px; opacity: .5; margin-top: 5px; font-family: var(--vscode-editor-font-family); }

  /* --- dugmeler --- */
  .actions { display: flex; gap: 6px; margin-top: 14px; }
  button {
    flex: 1; padding: 6px 10px; font-size: 12px; cursor: pointer;
    border: none; border-radius: 4px;
    color: var(--vscode-button-foreground);
    background: var(--vscode-button-background);
    font-family: inherit;
  }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button.secondary {
    color: var(--vscode-button-secondaryForeground);
    background: var(--vscode-button-secondaryBackground);
  }
  button.secondary:hover { background: var(--vscode-button-secondaryHoverBackground); }
  button:disabled { opacity: .5; cursor: default; }

  .empty { font-size: 11.5px; opacity: .6; line-height: 1.6; padding: 2px; }
  .error {
    font-size: 11.5px; line-height: 1.5; padding: 8px 10px; border-radius: 5px;
    color: var(--vscode-inputValidation-errorForeground, var(--danger));
    background: var(--vscode-inputValidation-errorBackground, rgba(248,81,73,.1));
    border: 1px solid var(--vscode-inputValidation-errorBorder, var(--danger));
  }
</style>
</head>
<body>
  <div id="root"><div class="empty">Yükleniyor…</div></div>

<script nonce="${n}">
  const vscode = acquireVsCodeApi();
  const RULE_LABELS = ${JSON.stringify(RULE_LABELS)};
  const SCOPE_LABELS = ${JSON.stringify(SCOPE_LABELS)};

  const SUG_ICONS = { clear_now: '🧹', error_loop: '🔁', cost_velocity: '💸' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  // Önce kaçış, sonra slash komutlarını <code>'a çevir — sıra önemli.
  function fmtDetail(s) {
    return esc(s).replace(/\\/(clear|compact|model)\\b/g, '<code>/$1</code>');
  }
  function fmtTokens(nn) {
    if (nn >= 1e6) return (nn / 1e6).toFixed(1) + 'M';
    if (nn >= 1e3) return (nn / 1e3).toFixed(nn >= 1e5 ? 0 : 1) + 'K';
    return String(nn || 0);
  }

  /** "Şimdi" — canlı öneriler. Öneri yoksa hiç yer kaplamaz. */
  function renderLive(live) {
    if (!live || !live.suggestions || !live.suggestions.length) return '';
    let html = '<h2>Şimdi</h2>';
    for (const s of live.suggestions) {
      const icon = SUG_ICONS[s.id] || '•';
      html +=
        '<div class="sug ' + esc(s.severity) + '">' +
          '<div class="sug-icon">' + icon + '</div>' +
          '<div><div class="sug-title">' + esc(s.title) + '</div>' +
          '<div class="sug-detail">' + fmtDetail(s.detail) + '</div></div>' +
        '</div>';
    }
    return html;
  }

  function renderSession(s, warn, danger, live) {
    if (!s) {
      return '<h2>Canlı oturum</h2><div class="card"><div class="empty">' +
        'Aktif Claude Code oturumu bulunamadı. Bu klasörde bir oturum başlatınca ' +
        'bağlam burada canlı görünür.</div></div>';
    }
    const ctx = s.contextTokens || 0;
    const pct = Math.min(100, (ctx / danger) * 100);
    const color = ctx >= danger ? 'var(--danger)' : ctx >= warn ? 'var(--warn)' : 'var(--ok)';
    const model = (s.model || '?').replace(/^claude-/, '');

    // Öğüt vermek "Şimdi" kartının işi; burası yalnızca olgu gösterir.
    // Taşıma maliyeti doğrudan hesaplanır — tahmin değil.
    let carry = '';
    if (live && live.carryPerTurn) {
      carry = '<div class="carry">Her yeni turn, bu geçmişi yeniden okumak için ' +
              '~$' + live.carryPerTurn.toFixed(3) + '</div>';
    }

    return '<h2>Canlı oturum</h2><div class="card">' +
      '<div class="ctx-top">' +
        '<div class="ctx-num">' + fmtTokens(ctx) + '<span class="unit">token bağlam</span></div>' +
        '<div class="ctx-model" title="' + esc(s.model || '') + '">' + esc(model) + '</div>' +
      '</div>' +
      '<div class="bar"><i style="width:' + pct.toFixed(1) + '%;background:' + color + '"></i></div>' +
      '<div class="meta"><span>' + (s.turns || 0) + ' turn</span>' +
        '<span>≈ $' + (s.costUsd || 0).toFixed(2) + '</span></div>' +
      carry +
    '</div>';
  }

  function renderTotals(d) {
    if (!d) return '';
    return '<h2>Tahmini israf</h2><div class="totals">' +
      '<div class="card"><div class="v">$' + (d.total_est_wasted_usd || 0).toFixed(2) + '</div>' +
        '<div class="k">maliyet</div></div>' +
      '<div class="card"><div class="v">' + fmtTokens(d.total_est_wasted_tokens || 0) + '</div>' +
        '<div class="k">token</div></div>' +
      '<div class="card"><div class="v">' + (d.findings || []).length + '</div>' +
        '<div class="k">bulgu</div></div>' +
    '</div>';
  }

  function renderFindings(d) {
    if (!d) return '';
    const list = (d.findings || []).slice().sort(
      (a, b) => (b.est_wasted_usd || 0) - (a.est_wasted_usd || 0)
    );
    if (!list.length) {
      return '<h2>Bulgular</h2><div class="card"><div class="empty">' +
        'Bulgu yok — bu pencerede ciddi bir israf görünmüyor. 👍</div></div>';
    }
    let html = '<h2>Bulgular (' + list.length + ')</h2>';
    for (const f of list) {
      const usd = f.est_wasted_usd || 0;
      const sev = usd >= 3 ? 'sev-high' : usd >= 1 ? 'sev-mid' : '';
      const isFile = f.scope_type === 'file';
      const label = RULE_LABELS[f.rule] || f.rule;
      const scopeLabel = SCOPE_LABELS[f.scope_type] || f.scope_type;
      const scopeText = isFile ? f.scope : String(f.scope || '').slice(0, 8) + '…';
      html +=
        '<div class="finding ' + sev + (isFile ? ' clickable' : '') + '"' +
          (isFile ? ' data-file="' + esc(f.scope) + '" data-line="' + (f.line || 1) + '"' : '') + '>' +
          '<div class="f-head"><span class="f-rule">' + esc(label) + '</span>' +
            '<span class="f-usd">' + (usd ? '$' + usd.toFixed(2) : fmtTokens(f.est_wasted_tokens)) + '</span></div>' +
          '<div class="f-msg">' + esc(f.message) + '</div>' +
          '<div class="f-scope">' + esc(scopeLabel) + ' · ' + esc(scopeText) + '</div>' +
        '</div>';
    }
    return html;
  }

  function render(st) {
    const root = document.getElementById('root');
    let html = renderLive(st.live) + renderSession(st.session, st.warn, st.danger, st.live);

    if (st.error) {
      html += '<h2>Teşhis</h2><div class="error">' + esc(st.error) + '</div>';
    } else if (st.loading && !st.diagnose) {
      html += '<h2>Teşhis</h2><div class="card"><div class="empty">Analiz ediliyor…</div></div>';
    } else if (!st.diagnose) {
      html += '<h2>Teşhis</h2><div class="card"><div class="empty">' +
              'Henüz analiz çalışmadı. <b>Yenile</b> ile başlat.</div></div>';
    } else {
      html += renderTotals(st.diagnose) + renderFindings(st.diagnose);
    }

    html += '<div class="actions">' +
      '<button id="refresh"' + (st.loading ? ' disabled' : '') + '>' +
        (st.loading ? 'Analiz ediliyor…' : 'Yenile') + '</button>' +
      '<button id="dash" class="secondary">Panel</button>' +
    '</div>';

    root.innerHTML = html;

    document.getElementById('refresh').onclick = () => vscode.postMessage({ type: 'refresh' });
    document.getElementById('dash').onclick = () => vscode.postMessage({ type: 'openDashboard' });
    for (const el of document.querySelectorAll('.finding.clickable')) {
      el.onclick = () => vscode.postMessage({
        type: 'openFinding',
        file: el.dataset.file,
        line: Number(el.dataset.line),
      });
    }
  }

  window.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'state') render(e.data.state);
  });
  vscode.postMessage({ type: 'ready' });
</script>
</body>
</html>`;
  }
}

module.exports = { SidebarProvider, RULE_LABELS, SCOPE_LABELS };
