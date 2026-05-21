# MusicSync

网易云音乐 ↔ QQ 音乐"我喜欢"歌单双向自动同步。通过 GitHub Actions 每天定时运行。

## 当前状态

**本地 Windows 端到端验证通过**(NE=304, QQ=858 dry-run)。**GitHub Actions CI 可运行** — 已从 `pymusiclibrary`(仅 Windows)迁移至 NeteaseCloudMusicApiEnhanced(Node.js HTTP 服务),支持跨平台。底层从 CSV 重做到 **SQLite**,匹配引擎为 L0/L1/L2/L3 四层。

## 工作原理

```
定时触发 (CST 0:00 / 12:00)
  │
  ▼
Step 1. Auth pre-check       — 双平台认证
Step 2. Fetch                — 拉取双方"我喜欢"列表
Step 2.5. Sanity check       — 抓取量陡降时中止,防数据丢失
Step 3. Canonicalize (L0)    — UPSERT songs + platform_links
Step 4. Diff vs DB           — 双侧 link / 单侧 link / 用户 unlike
Step 5. Match unlinked       — 对单侧歌曲做 L1→L2→L3 搜索匹配
Step 6. Plan                 — 汇总 ADD_NE / ADD_QQ / UNLIKE 动作
Step 7. Safety gate          — UNLIKE 超阈值 / FORCE_FULL_SYNC 时跳过清理
Step 8. Execute              — 调 API,每动作 commit
Step 9. Snapshot + meta      — 导出 CSV 快照,写 last_sync_at
```

### 匹配引擎

- **L0 canonicalize**:抓取时的 `(platform, track_id) → song_id` 归一化。命中现存 link 走 update;否则按 `canonical_key`(清洗后的 name|artist)挂到主版本(`manual` > 已同步 > 最早 > id 最小),都不匹配再新建。
- **L1 ISRC**:精确匹配。**QQ API 目前不暴露 ISRC,实际不可用**,保留以备未来。
- **L2 lyrics + duration**:`difflib.SequenceMatcher` 对清洗过的歌词比相似度(≥0.6),配合 duration 容差(短歌词 5s,长歌词 15s)。
- **L3 name + artist**:清洗 name 等值 + artist 子串包含,作为最后兜底。同 `canonical_key` 候选 ≥2 时跳过。

### SQLite Schema

```
songs(id, canonical_key, name, artist, album, match_source, original_match_source, created_at, updated_at)
platform_links(id, song_id, platform, platform_track_id, platform_name, platform_artist, platform_album,
               liked, synced_at, created_at, updated_at, UNIQUE(platform, platform_track_id))
lyrics_cache(platform, platform_track_id, original, translated, ...)
meta(key, value)
```

- `canonical_key` 上**仅索引**,**不 UNIQUE** — 保留同名异版(如 `Tell me` 在 Prover 和 eyes 是两首)。
- `(platform, platform_track_id)` UNIQUE — 同一外部 track 不可被两个 song 持有,Step 5 写入前用 `check_link_conflict` 预检。
- `liked=1` + `synced_at NOT NULL` 表示已实际同步到对端,与 `match_source` 解耦。

## 待解决

- **ISRC 匹配不可用**:`qqmusic-api-python` v0.6.0 不暴露 ISRC。
- **反向同步限速**:网易云搜索 API 短窗口 405 → cookie 标记 → IP 封禁 7-10 天。每次仅处理 25 首(`REVERSE_BATCH=25`)。连续 3 次 405 立即中止本批,保护 IP。
- **DRY_RUN 默认 true**:定时与手动触发都是 dry-run。要让定时触发真跑需改 workflow。

## 快速开始

### 1. Fork / Clone 仓库

```bash
git clone https://github.com/Smilucifer/MusicSync.git
cd MusicSync
```

### 2. 配置 GitHub Secrets

在 Settings → Secrets and variables → Actions 中添加:

| Secret | 说明 |
|---|---|
| `NETEASE_MUSIC_U` | 网易云 MUSIC_U cookie(浏览器登录后从 DevTools 获取) |
| `NETEASE_PHONE` | 网易云手机号(cookie 失效时 fallback 登录) |
| `NETEASE_MD5_PASSWORD` | 密码的 MD5 值 |
| `QQMUSIC_KEY` | QQ 音乐 musickey |
| `QQMUSIC_UIN` | QQ 号 |
| `QQMUSIC_EUIN` | QQ 音乐 euin |

### 3. 触发同步

- **自动**:每天 CST 0:00 和 12:00
- **手动**:Actions 页 → MusicSync → Run workflow → 选择 `dry_run` 与 `force_full_sync`

## 本地开发

### 环境要求

- Python 3.11+
- Node.js 18+(用于 NeteaseCloudMusicApi 服务)

### 安装步骤

```powershell
python -m venv .venv
.\.venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env             # 填入真实凭据

# 启动网易云 API 服务(另开终端,会话期常驻)
bash scripts/start_netease_api.sh

# Dry-run 预览(默认,不写任何东西)
$env:DRY_RUN="true"; python scripts/sync.py

# 真跑(写入双平台 + 更新 DB + 导出 snapshot)
$env:DRY_RUN="false"; python scripts/sync.py

# 反向同步小批量(防触发限速)
$env:DRY_RUN="false"; $env:REVERSE_BATCH="5"; python scripts/sync.py

# 强制全量(忽略 sanity,跳过清理)
$env:FORCE_FULL_SYNC="true"; python scripts/sync.py
```

### 测试

```powershell
python tests/test_db.py
python tests/test_matcher.py
python tests/test_migrate.py
python tests/test_sync_plan.py
python tests/test_netease_api_http.py   # 需先启动 Node.js 服务
```

### 数据迁移与诊断

```powershell
# 一次性:把老 CSV 数据库迁到 SQLite
python scripts/migrate_csv_to_db.py csv/song_mappings.csv data/musicsync.db

# 迁移后做 count + UNIQUE 校验
python scripts/verify_migration.py

# CI 上从 snapshot CSV 重建 DB(本地通常不用)
python scripts/bootstrap_db_from_snapshot.py
```

## 项目结构

```
MusicSync/
├── .github/workflows/sync.yml           # CI 定时 + 手动触发
├── scripts/
│   ├── sync.py                          # 主入口,10-step 流水线
│   ├── db.py                            # SQLite schema + 助手
│   ├── matcher.py                       # L0/L1/L2/L3 匹配引擎
│   ├── netease_api.py                   # NetEase HTTP 客户端
│   ├── qqmusic_api_v2.py                # QQ 音乐异步客户端
│   ├── auth_manager.py                  # 双平台认证
│   ├── migrate_csv_to_db.py             # CSV → SQLite 一次性迁移
│   ├── bootstrap_db_from_snapshot.py    # CI 从 snapshot 重建 DB
│   ├── verify_migration.py              # 迁移后校验
│   └── start_netease_api.sh             # 启动 NetEase API 服务
├── tests/
│   ├── test_db.py
│   ├── test_matcher.py
│   ├── test_migrate.py
│   ├── test_sync_plan.py
│   ├── test_netease_api_http.py
│   └── fixtures/song_mappings_fixture.csv
├── csv/song_mappings.csv                # 历史 CSV(已迁移,只读)
├── data/musicsync.db                    # SQLite 数据库(.gitignore)
└── requirements.txt
```

## 技术栈

- **Python 3.11+** + **SQLite**(WAL,foreign_keys=ON)
- **Node.js 18+** — NeteaseCloudMusicApiEnhanced HTTP 服务
- **QQ 音乐**:`qqmusic-api-python`(异步,微信凭据认证)
- **持久化**:SQLite DB + GitHub Actions Cache v4 + CSV snapshot(commit)
- **调度**:GitHub Actions workflow_dispatch + schedule (cron)

## License

MIT
