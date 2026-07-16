// Token Coach CLI'ını (cli.py) çalıştıran yardımcılar.

'use strict';

const cp = require('child_process');
const path = require('path');

/**
 * cli.py'ı verilen argümanlarla çalıştırır, stdout döndürür.
 * cwd = coachPath — cli.py, parser/rules paketlerini oradan import eder.
 */
function runCli(pythonPath, coachPath, args) {
  return new Promise((resolve, reject) => {
    cp.execFile(
      pythonPath,
      [path.join(coachPath, 'cli.py'), ...args],
      { cwd: coachPath, maxBuffer: 64 * 1024 * 1024, windowsHide: true },
      (err, stdout, stderr) => {
        if (err) reject(new Error(stderr || err.message));
        else resolve(stdout);
      }
    );
  });
}

/** JSONL loglarını SQLite'a alır (artımlı — değişmeyen dosyalar atlanır). */
function runIngest(pythonPath, coachPath, dbPath) {
  return runCli(pythonPath, coachPath, ['ingest', '--db', dbPath]);
}

/**
 * Teşhis kurallarını çalıştırır.
 * @param {number} days 0 = tüm geçmiş; >0 = son N günün oturum bulguları
 * @returns {Promise<{generated_at: string, total_est_wasted_tokens: number,
 *                    total_est_wasted_usd: number, findings: Array<object>}>}
 */
async function runDiagnose(pythonPath, coachPath, dbPath, projectRoots, days) {
  const args = ['diagnose', '--json', '--db', dbPath];
  for (const root of projectRoots) args.push('--project', root);
  if (days > 0) args.push('--days', String(days));
  const stdout = await runCli(pythonPath, coachPath, args);
  try {
    return JSON.parse(stdout);
  } catch (err) {
    throw new Error(`diagnose çıktısı JSON değil: ${err.message}`);
  }
}

/** Statik HTML paneli üretir; çıktı dosyasının yolunu döndürür. */
async function runDashboard(pythonPath, coachPath, dbPath, projectRoots, outPath) {
  const args = ['dashboard', '--db', dbPath, '--out', outPath];
  for (const root of projectRoots) args.push('--project', root);
  await runCli(pythonPath, coachPath, args);
  return outPath;
}

module.exports = { runCli, runIngest, runDiagnose, runDashboard };
