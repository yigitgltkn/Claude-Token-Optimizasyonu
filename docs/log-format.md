# Claude Code Log Format (Faz 0 keşif notları)

Kaynak dosya: `C:\Users\Yigit\.claude\projects\c--Users-Yigit-Desktop-Project-Token-Coach\48f8562c-569b-44b3-8a81-d68f2dec52da.jsonl`
(sadece ilk 20 satır incelendi)

**Not:** PLAN.md `C:\Users\Yigit\Desktop\Project` altında JSONL olduğunu varsayıyor ama gerçek
logların yeri `C:\Users\Yigit\.claude\projects\<proje-slug>\*.jsonl` — proje yolu, `\` yerine
`-` ile slug'lanmış klasör adına çevrilip `.claude\projects\` altında saklanıyor. Parser bunu
hesaba katmalı.

Her satır bağımsız bir JSON objesi (JSONL). Üst düzey `type` alanı kaydın türünü belirliyor.

## Görülen kayıt tipleri (ilk 20 satırda)

| # | type | Açıklama |
|---|------|----------|
| 1 | `queue-operation` | Mesaj kuyruğu event'i (`operation: enqueue/dequeue`). Sohbet içeriği taşımıyor, sadece `timestamp` + `sessionId`. |
| 2 | `user` | Kullanıcı mesajı. `message.role`, `message.content` (text bloklarından oluşan array), `uuid`, `parentUuid`, `promptId`, `cwd`, `gitBranch`, `permissionMode`, `version` içeriyor. |
| 3 | `attachment` | Konuşmaya eklenen yan-bilgi (deferred tools listesi, agent listesi, skill listesi vb.). `attachment.type` alt-tipi belirler (`deferred_tools_delta`, `agent_listing_delta`, `skill_listing`). |
| 4 | `file-history-snapshot` | Bir mesaj anındaki dosya durumu snapshot'ı (`trackedFileBackups`). |
| 5 | `assistant` | Model cevabı. `message.model`, `message.content` (text/thinking/tool_use blokları), `message.usage` (**token sayıları burada**), `message.stop_reason`, `requestId`. Hata durumunda üst seviyede `error` ve `isApiErrorMessage: true` de olabiliyor. |
| 6 | `ai-title` | Oturuma otomatik verilen başlık (`aiTitle`, `sessionId`). İçerik/usage taşımıyor. |

`user` tipi ayrıca tool sonuçlarını da taşıyabiliyor: `message.content[].type === "tool_result"`
ve `toolUseResult` alanında (ör. Glob sonucu `filenames`/`numFiles`, Read sonucu dosya içeriği
`content`/`numLines`) yapılandırılmış veri bulunuyor.

`assistant` tipi ayrıca tool çağrılarını taşıyor: `message.content[].type === "tool_use"`
(`name`, `input`, `id`).

İlk 20 satırda **görülmeyen** ama PLAN.md'nin bahsettiği tipler: `summary`, `sidechain`
(sidechain, ayrı bir `type` değil — `isSidechain: true/false` olarak her kayıtta yer alan bir
bayrak; ilk 20 satırın tamamında `false`).

## `usage` alanının bulunduğu yer

`usage` **sadece `type: "assistant"`** kayıtlarında, `message.usage` altında bulunuyor.
`user`, `attachment`, `queue-operation`, `file-history-snapshot`, `ai-title` kayıtlarında yok.

Örnek `message.usage` yapısı (satır 14):

```json
{
  "input_tokens": 3267,
  "cache_creation_input_tokens": 6179,
  "cache_read_input_tokens": 32432,
  "output_tokens": 84,
  "server_tool_use": { "web_search_requests": 0, "web_fetch_requests": 0 },
  "service_tier": "standard",
  "cache_creation": {
    "ephemeral_1h_input_tokens": 6179,
    "ephemeral_5m_input_tokens": 0
  },
  "inference_geo": "not_available",
  "iterations": [
    {
      "input_tokens": 3267,
      "output_tokens": 84,
      "cache_read_input_tokens": 32432,
      "cache_creation_input_tokens": 6179,
      "cache_creation": { "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 6179 },
      "type": "message"
    }
  ],
  "speed": "standard"
}
```

Token maliyeti hesaplamak için kritik alanlar: `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`. `iterations` array'i, aynı
assistant adımı içinde birden fazla API çağrısı olduğunda (ör. tool-use döngüsü) her bir
çağrının kendi usage'ını taşıyor gibi görünüyor — üst seviye `usage` bunların toplamı olabilir
(doğrulanmadı, sadece tek-iterasyonlu örnekler görüldü).

Hatalı/sentetik cevaplarda (satır 8, `model: "<synthetic>"`, `error: "authentication_failed"`)
`usage` alanı yine mevcut ama tüm sayılar `0`.

## Diğer gözlemler

- Her kayıt `sessionId`, `timestamp`, `version`, `cwd`, `gitBranch`, `entrypoint`, `userType`
  gibi ortak zarf (envelope) alanları taşıyor (queue-operation ve ai-title hariç, onlarda
  sadece `sessionId`/`timestamp` var).
- `assistant` kayıtlarında `message.model` gerçek model adını taşıyor (ör. `"claude-sonnet-5"`);
  hata durumunda `"<synthetic>"` görülüyor — model_mismatch kuralı bu alanı kullanmalı.
- `parentUuid` zinciri, kayıtları konuşma ağacında sıralamak için kullanılabilir.
- `promptId`, aynı kullanıcı isteğine ait birden fazla assistant/tool_result kaydını
  gruplamak için kullanılabilir (satır 11, 14-20 hepsi aynı `promptId`).
  **Düzeltme (2026-07-16, tam veri üzerinde doğrulandı):** `promptId` yalnızca `user`
  kayıtlarında bulunuyor; `assistant` kayıtları bu alanı hiç taşımıyor. Turns/prompt
  gibi sinyaller assistant kayıtlarından değil, oturumdaki distinct user `promptId`
  sayısından türetilmeli (bkz. `rules/model_mismatch.py` `session_tpp`).

## Sonraki adım için not

Bu doküman sadece 20 satırlık tek bir dosyaya dayanıyor; `summary` ve gerçek `isSidechain: true`
kayıtları örneklemde yoktu. Faz 1 parser'ı yazılırken bu iki durum için ek doğrulama gerekir.
