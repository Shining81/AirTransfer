# AirTransfer - Mac 本地文件传输服务器

## 功能特性

- 多文件/文件夹上传，保留目录结构
- 实时 WebSocket 进度推送
- 6 位配对码安全认证
- 自动检测局域网 IP，终端显示二维码
- **公网穿透**：内置 Cloudflare Tunnel，手机用流量也能传文件（无需注册、免费）
- 文件管理：列表/网格视图、预览、删除
- 毛玻璃暗色主题 UI
- 移动端优先，支持 PWA
- 流式写入，无文件大小限制
- 自动文件名冲突处理

## 快速启动

```bash
./start.sh
```

首次运行会自动创建虚拟环境并安装依赖。

## 启动参数

```bash
# 局域网模式（同一 WiFi 下传文件）
./start.sh

# 公网穿透模式（手机用 4G/5G 流量也能访问）
./start.sh --tunnel cloudflare

# 自定义参数
./start.sh --port 9000 --dir ~/Desktop/Transfer --no-auth --tunnel cloudflare
```

| 参数 | 说明 |
|------|------|
| `--port PORT` | 指定端口（默认 8899） |
| `--dir PATH` | 指定保存目录（默认 ~/Downloads/AirTransfer） |
| `--no-auth` | 跳过配对码验证 |
| `--no-ssl` | 禁用 SSL |
| `--tunnel cloudflare` | 启用 Cloudflare 公网穿透 |

### 公网穿透说明

使用 `--tunnel cloudflare` **无需任何配置**：
- 自动下载 cloudflared 二进制（首次约 30MB）
- 通过 Cloudflare 的全球网络建立安全隧道
- 免费、无需注册账号
- 启动后终端会显示公网地址（如 `https://xxx.trycloudflare.com`）
- 手机用 4G/5G 流量扫码即可访问

## 配置文件

编辑 `config.json` 自定义行为：

```json
{
  "port": 8899,
  "save_dir": "~/Downloads/AirTransfer",
  "auto_subfolder": false,
  "conflict_strategy": "rename",
  "enable_notification": true
}
```

## 使用方式

1. 启动服务器，终端显示配对码和访问地址
2. 手机浏览器扫描二维码或输入地址
3. 输入 6 位配对码完成连接
4. 拖拽或点击选择文件上传
5. 在"已接收"标签页管理文件

## 技术栈

- **后端**: Python 3 + FastAPI + Uvicorn
- **前端**: 纯 HTML/CSS/JS（单文件，零依赖）
- **通信**: WebSocket 实时进度 + HTTP multipart 上传
- **穿透**: Cloudflare Tunnel（cloudflared，自动下载）

## 依赖

```
fastapi
uvicorn[standard]
python-multipart
websockets
qrcode
```
