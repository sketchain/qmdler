# 真实响应快照

**这个目录现在是空的，而且必须保持为空，直到有人放进真实响应的脱敏快照。**

## 为什么空着

`tests/integration/test_engine_flow.py` 里那些 mock，结构是**照着模型定义想出来的**，
不是从线上抓的。它们能证明引擎的控制流对，但证明不了「我们对响应长什么样的理解是对的」。

往这里塞几个自己编的 JSON，只会把凭空 mock 换个地方再凭空一次，契约测试就退化成
自己跟自己对答案。所以宁可空着，让 `test_api_contract_snapshots.py` 整体 skip。

## 怎么填

1. 抓一份真实响应（`--single --json`，或直接把 CGI 的原始返回存成文件）；
2. 过脱敏脚本：

   ```bash
   python scripts/redact_snapshot.py raw.json -o tests/fixtures/api/query_song.json
   ```

   脚本按**键名**抹 `vkey` / `ekey` / `purl` / `musickey` / `uin` / `encrypt_uin` /
   `openid` / `access_token` / `refresh_token` 等，保留结构与字段类型
   （字符串换成同类型占位串、数字换成 0），并在落盘前自检有没有残留；

3. 快照一旦存在，`test_api_contract_snapshots.py` 会自动开始跑，拿它当契约。

## 文件命名

| 文件名 | 对应接口 |
|---|---|
| `api/query_song.json` | `music.trackInfo.UniformRuleCtrl / CgiGetTrackInfo` |
| `api/song_urls.json` | `music.vkey.GetVkey / UrlGetVkey` |
| `api/cdn_dispatch.json` | `music.audioCdnDispatch.cdnDispatch / GetCdnDispatch` |
| `api/songlist_detail.json` | `music.srfDissInfo.DissInfo / CgiGetDiss` |
| `api/lyric.json` | `music.musichallSong.PlayLyricInfo / GetPlayLyricInfo` |

存的是 CGI 信封里 `req_0` 那一层的内容（即 `data` 字段本身），不是整个信封。
