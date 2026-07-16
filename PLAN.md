# Token Coach — MVP Planı

## Faz 0 — Keşif (30 dk, Sonnet)
Amaç: Gerçek log formatını kendi makinende doğrulamak.
- `C:\Users\Yigit\Desktop\Project` altındaki bir JSONL dosyasından 5-10 örnek satırı incele
- Kayıt tiplerini listele (assistant, user, summary, sidechain vb.)
- usage alanlarının hangi kayıtlarda bulunduğunu belgele → `docs/log-format.md`
Kabul: log-format.md dosyası gerçek örneklerle dolu.

## Faz 1 — Parser + SQLite (Sonnet)
- `parser/ingest.py`: JSONL → SQLite (sessions, turns tabloları)
- Artımlı çalışma: işlenmiş dosyaları mtime + hash ile atla
- `cli.py ingest` komutu
Kabul: `python cli.py ingest` çalışır, ikinci çalıştırma 0 yeni kayıt işler.

## Faz 2 — CLAUDE.md Linter (Sonnet)
- Verilen proje kökündeki CLAUDE.md dosyalarını bul
- Token sayısı tahmini (tiktoken yerine basit char/4 yaklaşımı yeterli)
- Bulgular: toplam boyut, path-scoped'a taşınabilir bölümler, tekrarlar
- `cli.py lint <proje-yolu>` komutu
Kabul: Kendi gerçek projelerinden birinde anlamlı bulgu üretir.

## Faz 3 — Oturum teşhis kuralları (Sonnet, kural başına ayrı oturum)
Sıra: stale_context → model_mismatch → cache_efficiency → subagent_overuse
Her kural: `rules/<isim>.py` + fixture'lı test + est_wasted_tokens hesabı
Kabul: `cli.py diagnose` tüm kuralları çalıştırıp Finding listesi döner.

## Faz 4 — Rapor (Haiku yeterli)
- `cli.py report --weekly`: markdown rapor, bulgular önem sırasına göre
- Her bulguda: kanıt + somut aksiyon + tahmini token tasarrufu
Kabul: Bir haftalık gerçek verinle okunabilir rapor.

## Faz 5 — GUI + Koçluk (yapıldı, 2026-07-10)
- `cli.py dashboard [--project <yol>]... [--projects-root <kök>]` → tek dosyalık statik HTML panel
- Proje seçici dropdown (çok proje taranınca), veriye dayalı koçluk önerileri, /clear-/compact-/model kılavuzu
- stale_context düzeltmesi: context = input + cache_read + cache_creation (input_tokens tek başına yanıltıcı)

## Faz 6 — Şema genişletme + kural motoru (yapıldı, 2026-07-16)
- turns: prompt_id, parent_uuid, stop_reason, is_error, cache_5m/1h, speed, service_tier, num_iterations;
  sessions: title; yeni tool_calls tablosu. Eski DB otomatik migrate olur; `ingest --rebuild` backfill eder.
- `cli.py diagnose [--json] [--project ...]` — VS Code eklentisinin tüketeceği çıktı
- `rules/pricing.py` — model fiyat tablosu; bulgular artık est_wasted_usd da taşıyor
- `rules/cache_efficiency.py` — oturum içi /model değişimi → cache yeniden yazım maliyeti
- `rules/model_mismatch.py` — karşı-olgusal maliyet ($) + güven sinyalleri (hata oranı, turns/prompt;
  promptId sadece user kayıtlarında — session_tpp bundan türetiliyor, bkz. docs/log-format.md düzeltmesi)

## Faz 7 — VS Code eklenti iskeleti (yapıldı, 2026-07-16)
- `vscode-extension/` — saf JS, sıfır bağımlılık (Node/npm kurulu değil; derleme adımı yok, F5 ile çalışır)
- Durum çubuğu: aktif oturumun canlı bağlamı (offset tabanlı JSONL tail, `~/.claude/projects/<slug>`),
  model, oturum maliyeti; 120K sarı / 180K kırmızı eşikleri
- CLAUDE.md lint bulguları → Problems paneli (claude_md_tax.Finding'e `line` alanı eklendi,
  diagnose --json şemasına `line` girdi)
- `Token Coach: Bulguları Yenile / Göster` komutları; CLAUDE.md kaydında otomatik lint

## Faz 8 — Eklenti: panel + otomatik ingest + koçluk bildirimi (yapıldı, 2026-07-16)
- `diagnose --days N` (oturum bulgularını pencerele; dosya bulguları her zaman dahil)
- Eklenti: teşhis/panel öncesi otomatik artımlı ingest (`autoIngest`)
- `Token Coach: Paneli Aç` — dashboard.html webview'de (enableScripts, self-contained)
- Günlük koçluk bildirimi: son N günün israfı `notifyUsdThreshold` üstündeyse
- Tam Türkçeleştirme: kural mesajları, haftalık rapor, CLI çıktıları/yardımları, panel lejantı,
  eklenti etiketleri. Kural ID'leri ve JSON alan adları (makine sözleşmesi) İngilizce kaldı.

## Faz 9 — Kenar çubuğu GUI + Node araç zinciri (yapıldı, 2026-07-16)
- Node.js 24 LTS kuruldu (winget) → artık `node --check` ile JS doğrulanabiliyor, `vsce` ile
  resmi paketleme yapılabiliyor (önceki `.vsix` elle zip'lenmişti)
- `src/sidebar.js` — Activity Bar'da webview view (`tokenCoach.sidebar`): canlı bağlam
  göstergesi (eşiğe göre renkli bar), israf özeti, $'a göre sıralı tıklanabilir bulgu kartları
  (dosya bulgusu → editörde ilgili satır). Tema değişkenleriyle açık/koyu uyumlu.
- Durum çubuğu tıklaması artık çıktı kanalı yerine kenar çubuğunu açar
- Webview inline script'i template string içinde olduğu için `node --check` görmez →
  ayrı doğrulayıcı ile denetlendi (sözdizimi + render + XSS kaçışı + boş/hata durumları)

## Sonrası (backlog)
- Kalan Faz 3 kuralı: subagent_overuse (+ subagent_underuse sinyali — mevcut veride sidechain hiç yok)
- stale_context est_wasted hesabını rafine et (baseline x turn abartıyor; "temizlenebilir fark" daha dürüst)
- Eklenti canlı çalışma zamanı denetimi: kurulu (`yigit.token-coach@0.1.0`) ama kenar çubuğu/
  durum çubuğu gerçek VS Code oturumunda henüz gözle doğrulanmadı (statik doğrulama yapıldı)
- Dağıtım: Marketplace yerine GitHub release'e `.vsix` (karar 2026-07-16). Marketplace'e uygun
  değil — eklenti Python arka ucuna (`cli.py`) bağımlı, kuran kişide teşhis/panel çalışmaz;
  Marketplace istenirse önce kural motorunun JS'e portu gerekir. Repo henüz git değil.
- Python dashboard webview'i: yenile düğmesi / canlı veriye bağlama (kenar çubuğunda var, panelde yok)
- Koçluk önerilerini genişlet: sidechain/subagent oranı, plan modu kullanımı sinyali
- Dashboard'a oturum-detay görünümü (bir oturumun context büyüme eğrisi, /clear noktalarıyla)
- ccusage entegrasyonu (monitoring'i ona bırak)
- Haiku + Batch API ile doğal dilde rapor özeti

## Çalışma disiplini (kendi aracının felsefesi)
- Her faz = ayrı Claude Code oturumu. Faz bitince /rename + /clear.
- Faz 3'te kural başına oturum: bağlam küçük kalır.
- Model: Sonnet varsayılan; Faz 4 gibi basit işlerde Haiku'ya geç (/model).
- Karmaşık değişiklik öncesi Shift+Tab ile plan modu.