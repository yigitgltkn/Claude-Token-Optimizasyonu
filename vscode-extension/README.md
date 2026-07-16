# Token Coach — VS Code Eklentisi

Claude Code token kullanım koçu. Saf JavaScript — derleme adımı ve `npm install` gerektirmez.

## Yüzeyler

- **"Şimdi" kartı (canlı koç):** kenar çubuğunun en üstünde, *o an* eyleme
  dönüşebilir öneriler — `clear_now` (bağlam büyük **ve** yeni içerik gelmiyor),
  `error_loop` (ardışık API hatası), `cost_velocity` (harcama hızı). Öneri yoksa
  kart hiç görünmez. Bildirim (toast) yok: sessiz yüzey.
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

İki motor var:

- **Geriye dönük teşhis** — Python tarafında (`cli.py diagnose --json`).
  Karşı-olgusal tahminler üretir ("şunu yapsaydın şu kadar kazanırdın").
- **Canlı koç** (`src/liveCoach.js`) — tamamen eklentide, Python'suz, her
  turn'de anında. `~/.claude/projects/<slug>/*.jsonl` offset tabanlı artımlı
  okunur.

Canlı koç bilerek **yalnızca doğrudan ölçülen** büyüklükleri raporlar (mevcut
bağlamın turn başına okuma maliyeti, gerçekleşen harcama hızı, ardışık hata
sayısı) — "şu kadar israf ettin" demez, "şu an durum bu" der. Karşı-olgusal
tahminler abartmaya açıktır ve bir koçluk aracının tek sermayesi güvendir.

Aynı ilke arayüzde de geçerli: **sayı = olgu, renk = eylem çağrısı.** Durum
çubuğu yalnızca gerçekten bir öneri varken renklenir; bağlam büyük olsa da hâlâ
yeni bilgi geliyorsa `/clear` yanlış tavsiyedir ve renk yakılmaz.

## Testler

```
node --test
```

`liveCoach.js` ve `sessionWatcher.js` `vscode` modülüne bağımlı değildir, bu
yüzden doğrudan test edilebilir (30 test, bağımlılık yok).
