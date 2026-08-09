# 二维码测试素材

这两张是从上游真实接口取回的登录二维码：

| 文件 | 来源 | 格式 | 模块数 |
|---|---|---|---|
| `qq-login.png` | `ssl.ptlogin2.qq.com/ptqrshow` | PNG 111×111 | 33（version 4） |
| `wx-login.jpg` | `open.weixin.qq.com/connect/qrcode/{uuid}` | JPEG 470×470 | 41（version 6） |

用真图而不是自己生成的图，是因为要覆盖真实形态：非整数的每模块像素数
（111 / 33 = 3.36…）、JPEG 压缩噪点、中心 logo。这些都是自己造的干净图片
不会暴露的问题。

**二维码在取回后约两分钟即过期失效，不含任何账号信息，可以入库。**
（与之无关的 API 响应快照放在 `tests/fixtures/`，那里只收脱敏后的真实响应。）
