// Token Coach VS Code eklentisi.
//
// Yüzeyler:
//  - Kenar çubuğu (Activity Bar > Token Coach): ana GUI — canlı oturum
//    göstergesi, israf özeti, tıklanabilir bulgu kartları.
//  - Durum çubuğu: aktif oturumun canlı bağlam boyutu, model ve oturum
//    maliyeti. Eşik aşımlarında sarı/kırmızı (/clear hatırlatması).
//  - Diagnostics (Problems paneli): CLAUDE.md lint bulguları satır bazında.
//  - Çıktı kanalı: tüm teşhis bulguları (oturum kuralları dahil).

'use strict';

const fs = require('fs');
const path = require('path');
const vscode = require('vscode');
const { SessionWatcher, claudeProjectDirFor } = require('./sessionWatcher');
const { runDiagnose, runIngest, runDashboard } = require('./diagnose');
const { SidebarProvider } = require('./sidebar');

const SEVERITY_BY_RULE = {
  total_size: vscode.DiagnosticSeverity.Warning,
  duplicated_line: vscode.DiagnosticSeverity.Warning,
  path_scoped_candidate: vscode.DiagnosticSeverity.Information,
};

/** @type {vscode.StatusBarItem} */
let statusBar;
/** @type {vscode.DiagnosticCollection} */
let diagnostics;
/** @type {vscode.OutputChannel} */
let channel;
/** @type {SessionWatcher[]} */
let watchers = [];
let lastDiagnoseResult = null;
/** @type {vscode.ExtensionContext} */
let extContext;
/** @type {SidebarProvider} */
let sidebar;

const NOTIFY_STATE_KEY = 'tokenCoach.lastNotifiedAt';
const NOTIFY_INTERVAL_MS = 24 * 60 * 60 * 1000;

function config() {
  return vscode.workspace.getConfiguration('tokenCoach');
}

function workspaceRoots() {
  return (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
}

/** cli.py'ı içeren klasör: ayar > cli.py barındıran çalışma alanı klasörü. */
function resolveCoachPath() {
  const configured = config().get('coachPath');
  if (configured && fs.existsSync(path.join(configured, 'cli.py'))) return configured;
  for (const root of workspaceRoots()) {
    if (fs.existsSync(path.join(root, 'cli.py')) && fs.existsSync(path.join(root, 'rules'))) {
      return root;
    }
  }
  return null;
}

function resolveDbPath(coachPath) {
  const configured = config().get('dbPath');
  if (configured) return configured;
  return path.join(coachPath, 'token_coach.db');
}

function fmtTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 100_000 ? 0 : 1) + 'K';
  return String(n);
}

function shortModel(model) {
  return model ? model.replace(/^claude-/, '') : '?';
}

/** Canlı oturum güncellemesi: hem durum çubuğunu hem kenar çubuğunu besler. */
function onSessionUpdate(stats) {
  updateStatusBar(stats);
  sidebar.setState({
    session: stats,
    warn: config().get('contextWarnTokens'),
    danger: config().get('contextDangerTokens'),
  });
}

function updateStatusBar(stats) {
  const warn = config().get('contextWarnTokens');
  const danger = config().get('contextDangerTokens');
  const ctx = stats.contextTokens;

  statusBar.text = `$(zap) ${fmtTokens(ctx)} ctx · ${shortModel(stats.model)}`;
  if (ctx >= danger) {
    statusBar.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
  } else if (ctx >= warn) {
    statusBar.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
  } else {
    statusBar.backgroundColor = undefined;
  }

  const md = new vscode.MarkdownString();
  md.appendMarkdown(`**Token Coach — aktif oturum**\n\n`);
  md.appendMarkdown(`| | |\n|---|---|\n`);
  md.appendMarkdown(`| Bağlam | ${ctx.toLocaleString()} token |\n`);
  md.appendMarkdown(`| Model | ${stats.model || '—'} |\n`);
  md.appendMarkdown(`| Turn | ${stats.turns} |\n`);
  md.appendMarkdown(`| Oturum maliyeti | ≈ $${stats.costUsd.toFixed(2)} |\n`);
  md.appendMarkdown(`| Log | ${path.basename(stats.file)} |\n`);
  if (ctx >= warn) {
    md.appendMarkdown(
      `\n⚠️ Bağlam ${fmtTokens(warn)} eşiğini aştı — konu değiştiyse **/clear** iyi bir fikir.`
    );
  }
  statusBar.tooltip = md;
  statusBar.show();
}

function showUnconfiguredStatus() {
  statusBar.text = '$(zap) Token Coach';
  statusBar.tooltip =
    'Aktif Claude Code oturumu bulunamadı ya da cli.py yok. ' +
    '"tokenCoach.coachPath" ayarını Token-Coach klasörüne yöneltin.';
  statusBar.backgroundColor = undefined;
  statusBar.show();
}

async function refreshDiagnostics() {
  const coachPath = resolveCoachPath();
  if (!coachPath) {
    const msg =
      'cli.py bulunamadı. "tokenCoach.coachPath" ayarını Token-Coach klasörüne yöneltin ' +
      '(teşhis ve panel Python arka ucunu kullanır).';
    channel.appendLine(msg);
    sidebar.setState({ loading: false, error: msg });
    return;
  }
  sidebar.setState({ loading: true, error: null });
  const python = config().get('pythonPath');
  const dbPath = resolveDbPath(coachPath);
  const roots = workspaceRoots();
  const days = config().get('diagnoseDays');

  if (config().get('autoIngest')) {
    try {
      const out = await runIngest(python, coachPath, dbPath);
      channel.appendLine(`[${new Date().toLocaleTimeString()}] ingest: ${out.trim()}`);
    } catch (err) {
      // ingest başarısızlığı teşhisi engellemesin — eldeki DB ile devam
      channel.appendLine(`ingest başarısız (mevcut DB ile devam): ${err.message}`);
    }
  }

  let result;
  try {
    result = await runDiagnose(python, coachPath, dbPath, roots, days);
  } catch (err) {
    const msg = String(err.message || err);
    channel.appendLine(msg);
    sidebar.setState({ loading: false, error: 'Teşhis başarısız: ' + msg.split('\n')[0] });
    vscode.window.setStatusBarMessage('Token Coach: diagnose başarısız (çıktı kanalına bakın)', 5000);
    return;
  }
  lastDiagnoseResult = result;
  sidebar.setState({ loading: false, error: null, diagnose: result });

  // CLAUDE.md bulguları -> Problems paneli
  diagnostics.clear();
  const byFile = new Map();
  for (const f of result.findings) {
    if (f.scope_type !== 'file') continue;
    const line = Math.max(0, (f.line || 1) - 1);
    const diag = new vscode.Diagnostic(
      new vscode.Range(line, 0, line, 1000),
      `${f.message} (~${f.est_wasted_tokens} token)`,
      SEVERITY_BY_RULE[f.rule] ?? vscode.DiagnosticSeverity.Information
    );
    diag.source = 'Token Coach';
    diag.code = f.rule;
    if (!byFile.has(f.scope)) byFile.set(f.scope, []);
    byFile.get(f.scope).push(diag);
  }
  for (const [file, diags] of byFile) {
    diagnostics.set(vscode.Uri.file(file), diags);
  }

  const fileCount = [...byFile.values()].reduce((a, d) => a + d.length, 0);
  const sessionCount = result.findings.length - fileCount;
  channel.appendLine(
    `[${new Date().toLocaleTimeString()}] diagnose: ${result.findings.length} bulgu ` +
      `(${fileCount} CLAUDE.md, ${sessionCount} oturum), tahmini israf ` +
      `~${result.total_est_wasted_tokens.toLocaleString()} token / $${result.total_est_wasted_usd}`
  );

  maybeNotify(result);
}

/** Bulgu toplamı eşiği aşarsa günde en fazla bir kez koçluk bildirimi gösterir. */
function maybeNotify(result) {
  const threshold = config().get('notifyUsdThreshold');
  if (!threshold || result.total_est_wasted_usd < threshold) return;

  const last = extContext.globalState.get(NOTIFY_STATE_KEY, 0);
  if (Date.now() - last < NOTIFY_INTERVAL_MS) return;
  extContext.globalState.update(NOTIFY_STATE_KEY, Date.now());

  const days = config().get('diagnoseDays');
  const window = days > 0 ? `son ${days} günde ` : '';
  vscode.window
    .showInformationMessage(
      `Token Coach: ${window}~$${result.total_est_wasted_usd} tahmini token israfı tespit edildi.`,
      'Bulguları Göster'
    )
    .then((choice) => {
      if (choice === 'Bulguları Göster') showFindings();
    });
}

/** Dashboard HTML'ini üretip webview panelinde açar. */
async function openDashboard() {
  const coachPath = resolveCoachPath();
  if (!coachPath) {
    vscode.window.showWarningMessage('Token Coach: cli.py bulunamadı — tokenCoach.coachPath ayarını yapın.');
    return;
  }
  const python = config().get('pythonPath');
  const dbPath = resolveDbPath(coachPath);
  const roots = workspaceRoots();

  const storageDir = extContext.globalStorageUri.fsPath;
  fs.mkdirSync(storageDir, { recursive: true });
  const outPath = path.join(storageDir, 'dashboard.html');

  try {
    if (config().get('autoIngest')) {
      await runIngest(python, coachPath, dbPath).catch(() => {});
    }
    await runDashboard(python, coachPath, dbPath, roots, outPath);
  } catch (err) {
    channel.appendLine(`dashboard başarısız: ${err.message}`);
    vscode.window.showErrorMessage('Token Coach: panel üretilemedi (çıktı kanalına bakın).');
    return;
  }

  const html = fs.readFileSync(outPath, 'utf8');
  const panel = vscode.window.createWebviewPanel(
    'tokenCoachDashboard',
    'Token Coach Panel',
    vscode.ViewColumn.One,
    { enableScripts: true } // panelin proje seçici dropdown'u inline script kullanıyor
  );
  panel.webview.html = html;
}

function showFindings() {
  channel.clear();
  if (!lastDiagnoseResult) {
    channel.appendLine('Henüz teşhis çalışmadı — "Token Coach: Bulguları Yenile" komutunu kullanın.');
    channel.show();
    return;
  }
  const r = lastDiagnoseResult;
  const scopeLabels = { session: 'oturum', file: 'dosya' };
  channel.appendLine(`Token Coach — ${r.generated_at}`);
  channel.appendLine(
    `Toplam tahmini israf: ~${r.total_est_wasted_tokens.toLocaleString()} token / $${r.total_est_wasted_usd}`
  );
  channel.appendLine('');
  for (const f of r.findings) {
    const usd = f.est_wasted_usd ? ` · $${f.est_wasted_usd.toFixed(2)}` : '';
    const scopeLabel = scopeLabels[f.scope_type] || f.scope_type;
    channel.appendLine(`[${f.rule}] ${scopeLabel} ${f.scope}${usd}`);
    channel.appendLine(`  ${f.message}`);
    channel.appendLine('');
  }
  channel.show();
}

function startSessionWatchers() {
  for (const w of watchers) w.dispose();
  watchers = [];

  let found = false;
  for (const root of workspaceRoots()) {
    const dir = claudeProjectDirFor(root);
    if (!dir) continue;
    const watcher = new SessionWatcher(dir, onSessionUpdate);
    watcher.start();
    watchers.push(watcher);
    found = true;
  }
  if (!found) {
    showUnconfiguredStatus();
    sidebar.setState({ session: null });
  }
}

/** Bir dosya bulgusunu editörde ilgili satırda açar. */
async function openFinding(file, line) {
  try {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    const editor = await vscode.window.showTextDocument(doc);
    const pos = new vscode.Position(Math.max(0, (line || 1) - 1), 0);
    editor.selection = new vscode.Selection(pos, pos);
    editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
  } catch (err) {
    vscode.window.showWarningMessage(`Token Coach: dosya açılamadı — ${file}`);
  }
}

function activate(context) {
  extContext = context;
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  // Tıklayınca asıl GUI'yi (kenar çubuğu) aç — çıktı kanalı yedek yüzey.
  statusBar.command = 'workbench.view.extension.tokenCoach';
  diagnostics = vscode.languages.createDiagnosticCollection('tokenCoach');
  channel = vscode.window.createOutputChannel('Token Coach');
  context.subscriptions.push(statusBar, diagnostics, channel);

  sidebar = new SidebarProvider(context.extensionUri, {
    onRefresh: () => refreshDiagnostics(),
    onOpenDashboard: () => openDashboard(),
    onOpenFinding: (file, line) => openFinding(file, line),
  });
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('tokenCoach.sidebar', sidebar)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('tokenCoach.refresh', async () => {
      await refreshDiagnostics();
      vscode.window.setStatusBarMessage('Token Coach: bulgular yenilendi', 3000);
    }),
    vscode.commands.registerCommand('tokenCoach.showFindings', showFindings),
    vscode.commands.registerCommand('tokenCoach.openDashboard', openDashboard),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (path.basename(doc.fileName) === 'CLAUDE.md') refreshDiagnostics();
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => startSessionWatchers()),
    { dispose: () => watchers.forEach((w) => w.dispose()) }
  );

  startSessionWatchers();
  refreshDiagnostics();
}

function deactivate() {
  for (const w of watchers) w.dispose();
}

module.exports = { activate, deactivate };
