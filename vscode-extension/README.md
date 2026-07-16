# Token Coach — VS Code Eklentisi

Claude Code token kullanım koçu. Saf JavaScript — derleme adımı ve `npm install` gerektirmez.

## Yüzeyler

- **Kenar çubuğu (ana GUI):** Activity Bar'daki Token Coach ikonu. Canlı bağlam
  göstergesi (eşiğe göre yeşil/sarı/kırmızı bar), tahmini israf özeti ($ / token /
  bulgu sayısı) ve maliyete göre sıralı bulgu kartları. CLAUDE.md bulgusuna
  tıklayınca dosya ilgili satırda açılır. Başlıktaki düğmelerden yenile / panel.
- **Durum çubuğu:** aktif Claude Code oturumunun canlı bağlam boyutu
  (`input + cache_read + cache_creation`), model ve tahmini oturum maliyeti.
  120K'da sarı, 180K'da kırmızı (ayarlanabilir) — `/clear` hatırlatması.
  Tıklayınca kenar çubuğunu açar.
- **Problems paneli:** `CLAUDE.md` lint bulguları (`total_size`,
  `path_scoped_candidate`, `duplicated_line`) satır bazında. CLAUDE.md
  kaydedilince otomatik yenilenir.
- **Çıktı kanalı (Token Coach):** oturum kuralları dahil tüm teşhis bulguları
  ($ tahminleriyle).
- **Panel (webview):** `Token Coach: Paneli Aç` — mevcut HTML dashboard'u
  VS Code sekmesinde üretip açar.
- **Koçluk bildirimi:** son N günün tahmini israfı eşiği aşarsa günde en
  fazla bir bilgi bildirimi.

## Kurulum

```
npx @vscode/vsce package --allow-missing-repository --skip-license
code --install-extension token-coach-0.1.0.vsix --force
```

Sonra VS Code penceresini yeniden yükle (`Developer: Reload Window`).

## Geliştirme

1. VS Code'da **bu klasörü** (`vscode-extension/`) aç.
2. `F5` — Extension Development Host açılır.
3. Host pencerede Token-Coach projesini (veya Claude Code kullandığın herhangi
   bir projeyi) aç.

Değişiklikten sonra hızlı denetim (webview script'i template string içinde
olduğu için `node --check` onu görmez — render'ı ayrıca doğrula):

```
node --check src/*.js
```

## Ayarlar

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `tokenCoach.pythonPath` | `python` | Python yorumlayıcısı |
| `tokenCoach.coachPath` | *(boş)* | `cli.py` içeren Token-Coach klasörü; boşsa çalışma alanında aranır |
| `tokenCoach.dbPath` | *(boş)* | `token_coach.db` yolu; boşsa `coachPath/token_coach.db` |
| `tokenCoach.contextWarnTokens` | `120000` | Sarı eşik |
| `tokenCoach.contextDangerTokens` | `180000` | Kırmızı eşik |
| `tokenCoach.autoIngest` | `true` | Teşhis/panel öncesi logları artımlı ingest et |
| `tokenCoach.diagnoseDays` | `7` | Oturum bulgularını son N günle sınırla (0 = tümü) |
| `tokenCoach.notifyUsdThreshold` | `5` | İsraf eşiği ($); aşılırsa günde bir bildirim (0 = kapalı) |

## Komutlar

- **Token Coach: Bulguları Yenile** — (ingest +) `cli.py diagnose --json` çalıştırır.
- **Token Coach: Bulguları Göster** — son teşhisi çıktı kanalında listeler.
- **Token Coach: Paneli Aç** — dashboard'u webview'de açar.

## Mimari not

Eklenti ince bir kabuk: tüm kural motoru Python tarafında
(`cli.py diagnose --json`). Canlı oturum izleme ise tamamen eklentide —
`~/.claude/projects/<slug>/*.jsonl` dosyası offset tabanlı artımlı okunur,
Python'a ihtiyaç duymaz.
