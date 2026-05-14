# MusicSync

网易云音乐 ↔ QQ音乐"我喜欢"歌单双向自动同步。通过 GitHub Actions 每天定时运行。

## 当前状态

**本地 Windows 验证通过**，298/299 首网易云歌曲已同步到 QQ 音乐，反向同步也已验证。**GitHub Actions CI 被阻断** — `pymusiclibrary` 只有 Windows wheel，无法在 Ubuntu runner 上运行。解决方向见下方"待解决"。

### CSV 数据库概况

`csv/song_mappings.csv` — 1011 行歌曲并集（每行一首歌，双平台 ID）：

| match_source | 数量 | 说明 |
|---|---|---|
| `manual` | 297 | 已手动确认匹配 |
| `qq_only` | 712 | QQ 独占，等待反向同步到网易云 |
| `unmatched` | 1 | 匹配失败 |
| `name_artist` | 1 | 歌名+歌手匹配，仅预览 |

- `synced=1`：298 首（已实际添加到 QQ 音乐）
- 双向 ID 都有的行：298
- 仅网易云 ID：299 行
- 仅 QQ ID（qq_only）：712 行

## 工作原理

```
定时触发 (CST 0:00 / 12:00)
  │
  ▼
Step 0: Auth pre-check — 验证双方平台认证
Step 1: Load state — 从 GitHub Actions Cache 恢复状态
Step 2: Fetch — 获取双方"我喜欢"列表，更新 CSV
Step 3: Diff — 找出需要处理的新增/待匹配歌曲
Step 4: Match — L1(ISRC) + L2(歌名+歌手) + manual 映射
Step 5: Execute — L1/manual 自动添加到对方平台，L2 仅预览
Step 6: Save — 写回 CSV 到 Cache
Step 7: Reverse sync — QQ 独占歌曲反向搜索网易云并添加
```

### 匹配策略

- **manual**: CSV 中手动填写的 QQ ID，优先级最高，自动执行
- **isrc (L1)**: ISRC 精确匹配。**QQ API 目前不暴露 ISRC 字段，实际不可用**
- **name_artist (L2)**: 清理歌名精确匹配 + 歌手包含检查，仅 dry-run 预览
- **搜索兜底**: 调用 QQ/网易云搜索 API，对结果做 L1/L2 匹配

### CSV Schema

```csv
netease_id | qq_id | name | artist | album | ne_name | ne_artist | qq_name | qq_artist | match_source | synced
```

- `netease_id` + `qq_id`：双 ID 模型，至少一个不为空
- `synced`：`1` 表示已实际添加到对方平台（与 match_source 独立）
- QQ 独占行用 `qq_{qq_id}` 作为临时 key，反向同步成功后 `promote_qq_row` 转为 ne_id 主键

## 待解决

### 🔴 阻断：GitHub Actions Ubuntu runner 无法运行

`netease_api.py` 依赖 `MusicLibrary`（来自 `pymusiclibrary`），这是一个 C 绑定的原生库，**只发布 Windows wheel**（`cp314-abi3-win_amd64`）。GitHub Actions 使用 `ubuntu-latest`，加载 `libengine.so` 时报：

```
OSError: libengine.so: cannot open shared object file: No such file or directory
```

**解决方向**（按推荐顺序）：

1. **换回 `pyncm`**（推荐）：纯 Python 实现，跨平台。PyPI 上有包（`pip install pyncm`，最新 1.6.8.4.2）。需重写 `netease_api.py` 用 `pyncm` API 替换 `MusicLibrary`。
2. **改用 `pyncm-async`**：pyncm 的异步变体，同样跨平台。
3. **自建 Windows runner**：如果坚持用 MusicLibrary，需要 Windows self-hosted runner（成本高，不推荐）。
4. **用 Docker 包装 Wine**：极不推荐，脆弱且慢。

### 🟡 其他

- **ISRC 匹配不可用**：`qqmusic-api-python` v0.6.0 不暴露 ISRC。曾测试过 `song.get_detail()` 的 `extras` 字段，无 ISRC 数据。
- **反向同步限速**：网易云搜索 API 限流严格（短窗口 405 → cookie 标记 → IP 封禁 7-10 天）。CI 每次仅处理 25 首（`REVERSE_BATCH=25`），712 首约需 29 次运行（~14 天）。
- **Node.js 20 actions 弃用警告**：GitHub Actions 将在 2026-09-16 移除 Node.js 20 支持，需关注 `actions/cache@v4`、`actions/checkout@v4` 等是否有更新版本。
- **DRY_RUN 默认 true**：无论是定时触发还是手动触发，默认都是 dry-run 模式。要让定时触发真正执行，需修改 workflow 文件。

## 快速开始

### 1. Fork/Clone 仓库

```bash
git clone https://github.com/Smilucifer/MusicSync.git
cd MusicSync
```

### 2. 配置 GitHub Secrets

在 Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 |
|---|---|
| `NETEASE_MUSIC_U` | 网易云 MUSIC_U cookie（浏览器登录后从 DevTools 获取） |
| `NETEASE_PHONE` | 网易云手机号（cookie 失效时 fallback 登录） |
| `NETEASE_MD5_PASSWORD` | 密码的 MD5 值（`echo -n "password" \| md5sum`） |
| `QQMUSIC_KEY` | QQ 音乐 musickey（浏览器登录后从 DevTools → Application → Cookies 获取） |
| `QQMUSIC_UIN` | QQ 号 |
| `QQMUSIC_EUIN` | QQ 音乐 euin（从 qqmusic API 请求参数中获取） |

### 3. 触发同步

- **自动**：每天 CST 0:00 和 12:00（定时触发）
- **手动**：Actions 页 → MusicSync → Run workflow → 选择 dry_run=true/false

### 4. 首次运行

手动触发时 `dry_run` 默认为 true，仅预览不执行。确认无误后以 `dry_run=false` 再次手动触发。

## 本地开发

```bash
# 创建虚拟环境
python -m venv .venv
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

# 只测试反向同步几首
DRY_RUN=false REVERSE_BATCH=5 python scripts/sync.py
```

### 工具脚本

```bash
# 批量搜索 QQ 音乐，为未匹配歌曲找候选
python scripts/resolve_unmatched.py

# 测试 QQ 音乐 API 是否返回 ISRC
python scripts/test_isrc.py

# 从旧 JSON 状态迁移到 CSV
python scripts/migrate_to_csv.py
```

## 项目结构

```
MusicSync/
├── .github/workflows/sync.yml      # GitHub Actions 定时触发（Cache 持久化）
├── scripts/
│   ├── sync.py                     # 主入口（8 步流水线）
│   ├── netease_api.py              # 网易云 API（MusicLibrary 绑定，当前不可跨平台）
│   ├── qqmusic_api_v2.py           # QQ 音乐 API（qqmusic-api-python 异步库）
│   ├── auth_manager.py             # 认证管理
│   ├── matcher.py                  # L1(ISRC) + L2(歌名+歌手) 匹配引擎
│   ├── csv_database.py             # CSV 读写/查询封装（双 ID 模型）
│   ├── resolve_unmatched.py        # 批量搜索 QQ 音乐匹配未关联歌曲
│   ├── migrate_to_csv.py           # 一次性从旧 JSON 状态迁移到 CSV
│   ├── test_isrc.py                # 探测 QQ API 是否暴露 ISRC
│   ├── search_qq.py                # QQ 音乐命令行搜索工具
│   └── bulk_search_qq.py           # QQ 音乐批量搜索
├── csv/song_mappings.csv           # 歌曲身份数据库（唯一真相源）
├── state/sync_state.json           # 同步元数据（GitHub Actions Cache 持久化）
└── requirements.txt
```

## 技术栈

- **Python 3.11+**
- **网易云**: `pymusiclibrary` (MusicLibrary C 绑定，仅 Windows)
- **QQ 音乐**: `qqmusic-api-python` (异步，微信凭据认证)
- **持久化**: CSV + GitHub Actions Cache v4（非 git push）
- **调度**: GitHub Actions workflow_dispatch + schedule (cron)

## License

MIT
