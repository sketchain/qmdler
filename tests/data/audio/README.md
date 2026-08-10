# 测试用音频素材

0.25 秒静音，每种容器一份，用 ffmpeg 生成：

```
ffmpeg -f lavfi -i "anullsrc=r=44100:cl=stereo" -t 0.25 -c:a <编码器> silence.<后缀>
```

| 文件 | 编码器 | 用途 |
|---|---|---|
| `silence.flac` | `flac` | FLAC 写 tag / 嵌封面 |
| `silence.mp3` | `libmp3lame` | MP3 写 tag（无预置 tag 的情形） |
| `silence.ogg` | `libvorbis` | OGG Vorbis 写 tag / `METADATA_BLOCK_PICTURE` |
| `silence.m4a` | `aac` | MP4/M4A 写 tag / `covr` |
| `preexisting-id3.mp3` | 同上 + mutagen | **服务端已经写过 ID3v2 + ID3v1** 的形态 |

`preexisting-id3.mp3` 是刻意造的：QQ 返回的 MP3 自带 `TIT2` / `TPE1` / `TALB` / `TCON`
和一份 ID3v1。写 tag 逻辑必须能在这种文件上正确清干净再重写 —— 曾经有个 bug 就是
只在这条路径上炸（`ID3.delete()` 拿不到 filename），而单元测试里没有任何真实音频，
所以一直没被发现。

**为什么用真文件而不是 mock**：这些 bug 全部出在 mutagen 与真实容器的交互上，
mock 掉 mutagen 等于把要测的东西测没了。文件总共不到 25 KB。

与 `tests/fixtures/`（API 响应快照，故意保持为空）无关，别混淆。
