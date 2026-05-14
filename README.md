# MusicSync

每天自动同步网易云音乐"我喜欢的音乐" → QQ音乐"我喜欢"歌单。

## 工作原理

- 从网易云音乐获取"我喜欢的音乐"列表
- 通过 L1 (ISRC精确匹配) 和 L2 (歌名+歌手匹配) 在QQ音乐找到对应歌曲
- L1匹配自动添加到QQ音乐收藏，L2匹配仅预览（需手动确认）
- 通过 GitHub Actions 每天 CST 0:00 和 12:00 自动运行

## 快速开始

### 1. 配置 GitHub Secrets

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 |
|--------|------|
| `NETEASE_PHONE` | 网易云手机号 |
| `NETEASE_MD5_PASSWORD` | 密码的 MD5 值（`echo -n "your_password" | md5sum`） |
| `QQMUSIC_REFRESH_TOKEN` | QQ音乐 refresh_token |
| `QQMUSIC_UIN` | QQ号 |

### 2. 获取 QQ音乐 refresh_token

（待 Phase 3 实现本地 QR 辅助脚本）

### 3. 触发同步

- **自动**：每天 CST 0:00 和 12:00
- **手动**：Actions 页面 → Run workflow → 选择 dry_run=true/false

### 4. 首次运行

首次运行为 dry-run 模式，仅预览变更不执行。确认无误后通过 `workflow_dispatch` 手动触发执行。

## 本地开发

```bash
# 创建虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 配置凭据
cp .env.example .env
# 编辑 .env 填入真实凭据

# 干运行预览
DRY_RUN=true python scripts/sync.py

# 执行同步
DRY_RUN=false python scripts/sync.py
```

## 架构

```
MusicSync/
├── .github/workflows/sync.yml   # GitHub Actions 定时触发
├── scripts/
│   ├── sync.py                  # 主入口（7步流水线）
│   ├── weapi.py                 # 网易云 weapi 加密
│   ├── netease_api.py           # 网易云 API 封装
│   ├── qqmusic_api.py           # QQ音乐 API 封装
│   ├── qqmusic_sign.py          # QQ音乐签名算法
│   ├── matcher.py               # L1+L2 歌曲匹配引擎
│   ├── auth_manager.py          # 认证管理
│   ├── verify_netease.py        # Phase 0: 网易云连通性验证
│   ├── verify_qqmusic_sign.py   # Phase 0: QQ签名算法验证
│   └── verify_qqmusic.py        # Phase 0: QQ认证+收藏验证
├── state/sync_state.json        # 同步状态（GitHub Actions Cache持久化）
└── requirements.txt
```

### 同步流水线

1. Auth pre-check — 验证双方平台认证
2. Load state — 从 Cache 加载上次同步状态
3. Fetch — 获取双方"我喜欢"列表
4. Diff — 对比状态文件，识别新增
5. Match — L1 ISRC精确匹配 + L2 歌名清理匹配
6. Execute — L1自动添加，L2 dry-run预览
7. Save state — 持久化新状态到 Cache

### 匹配策略

- **L1**: ISRC 精确匹配，自动执行
- **L2**: 歌名清理后精确匹配 + 歌手包含检查，仅 dry-run 预览
- **L3**: 延后。模糊匹配对中文准确率不足，待收集 L1+L2 真实数据后评估

## License

MIT
