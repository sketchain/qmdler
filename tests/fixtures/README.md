# 真实响应快照

`api/` 下的五份快照是**真实响应脱敏后的产物**，不是手写的。
`test_api_contract_snapshots.py` 拿它们当契约：模型能不能吃下真实结构，全靠这几份。

## 为什么不能手写

`tests/integration/test_engine_flow.py` 里那些 mock，结构是**照着模型定义想出来的**，
不是从线上抓的。它们能证明引擎的控制流对，但证明不了「我们对响应长什么样的理解是对的」。

往这里塞几个自己编的 JSON，只会把凭空 mock 换个地方再凭空一次，契约测试就退化成
自己跟自己对答案。**没有真快照时宁可空着**，让契约测试整体 skip —— 那是诚实的 skip。

## 采集条件（重要，会影响判读）

这批快照采于 **2026-08**，采集环境有一个必须记下来的限制：

> **出口 IP 受地区版权分区限制。** 同一个有效凭证下，「起风了」全档 `result=0`，
> 而「晴天」「海阔天空」「小城夏天」**所有档位**都是 `104003`。已排除请求参数缺失
> （补齐 `song_type` / `media_mid` 重试，结果不变）。

因此：

- `song_urls.json` 里**故意放了两条**：一条 `result=0` 的正常曲目，一条 `result=104003`
  的受限曲目。两种形态都要能解析；
- 在别的网络环境重抓时，`104003` 那条**很可能变成 `result=0`**。
  **那不是回归**，是环境不同。要对比结构差异，别对比 `result` 的值。

账号侧条件：超级会员（`svip=1`、`huge_vip=1`），所以 `query_song.json` 里
`MASTER` / `ATMOS_DB` 等高档位的 `size_*` 是非零的。普通账号抓出来这些会是 0。

## 怎么更新

```bash
python scripts/redact_snapshot.py raw.json -o tests/fixtures/api/query_song.json
```

脚本按**键名**抹 `vkey` / `ekey` / `purl` / `musickey` / `uin` / `encrypt_uin` /
`openid` / `access_token` / `refresh_token` / `test_file` 等，保留结构与字段类型
（字符串换成同类型占位串、数字换成 0），并在落盘前自检有没有残留，有残留就拒绝写。

**除了凭证，第三方的身份信息也抹掉**：歌单详情里带着歌单作者的 `nick` 与 `headurl`，
没有理由把别人的昵称和头像地址提交进公开仓库。头像 URL 里还有 32 位十六进制哈希，
不抹会直接把落盘自检打红（自检的十六进制模式大小写都认，就是为了兜住这个）。

抓完**立刻**脱敏，原始文件不要留在仓库里，也不要留在任何会被提交的目录下。

## 文件命名

| 文件名 | 对应接口 |
|---|---|
| `api/query_song.json` | `music.trackInfo.UniformRuleCtrl / CgiGetTrackInfo` |
| `api/song_urls.json` | `music.vkey.GetVkey / UrlGetVkey` |
| `api/cdn_dispatch.json` | `music.audioCdnDispatch.cdnDispatch / GetCdnDispatch` |
| `api/songlist_detail.json` | `music.srfDissInfo.DissInfo / CgiGetDiss` |
| `api/lyric.json` | `music.musichallSong.PlayLyricInfo / GetPlayLyricInfo` |

存的是 CGI 信封里 `req_0` 那一层的内容（即 `data` 字段本身），不是整个信封。

与 `tests/data/`（二维码图片、音频素材）无关，别混淆。
