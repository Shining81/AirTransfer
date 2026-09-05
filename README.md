# AirTransfer - Mac 本地文件传输服务器

局域网 / 公网手机传文件到 Mac，打开即用。

## 一键启动

```bash
./start.sh
```

首次运行自动创建虚拟环境、安装依赖、启动公网隧道。

手机扫码或输入终端显示的地址即可传文件。

## 功能

- 多文件/文件夹上传，保留目录结构
- 手机相机直接拍照上传
- 实时上传进度（百分比、速度、剩余时间）
- 6 位配对码安全认证
- **自动检测网络**：局域网直连极速 / 公网隧道自动穿透
- **公网穿透**：内置 Cloudflare Tunnel，无需注册、免费
- **大文件加速**：公网自动分片 3 路并行上传
- 文件管理：网格/列表视图、图片视频预览、删除
- 毛玻璃暗色主题 UI
- 移动端优先 + PWA 支持
- 流式写入，无文件大小限制
- 自动文件名冲突处理

## 启动参数

```bash
# 默认：公网穿透开启
./start.sh

# 仅局域网（不需要公网穿透）
./start.sh --no-tunnel

# 自定义端口和目录
./start.sh --port 9000 --dir ~/Desktop/Transfer

# 跳过配对码
./start.sh --no-auth
```

| 参数 | 说明 |
|------|------|
| `--port PORT` | 指定端口（默认 8899） |
| `--dir PATH` | 指定保存目录（默认 ~/Downloads/AirTransfer） |
| `--no-auth` | 跳过配对码验证 |
| `--no-ssl` | 禁用 SSL |
| `--no-tunnel` | 禁用公网穿透，仅局域网可用 |
| `--tunnel cloudflare` | 启用 Cloudflare 穿透（默认开启） |

## 网络模式

| 模式 | 速度 | 说明 |
|------|------|------|
| 局域网 WiFi | 极速（取决于路由器） | 同一 WiFi 下直连 Mac IP |
| 公网隧道 | 一般（Cloudflare 免费带宽） | 手机 4G/5G 或异地访问 |

页面会自动检测网络类型并显示状态：
- 绿色「局域网 · 极速」= 直连模式
- 橙色「公网隧道」= Cloudflare 穿透模式

## 技术栈

- **后端**: Python 3 + FastAPI + Uvicorn
- **前端**: 纯 HTML/CSS/JS（单文件，零依赖）
- **通信**: WebSocket 实时进度 + HTTP multipart 上传
- **穿透**: Cloudflare Tunnel（cloudflared，自动下载）
- **压缩**: GZip 响应压缩
- **加速**: 大文件分片 3 路并行上传

## 依赖

```
fastapi
uvicorn[standard]
python-multipart
websockets
qrcode
```

无需额外安装 cloudflared，首次运行自动下载。
