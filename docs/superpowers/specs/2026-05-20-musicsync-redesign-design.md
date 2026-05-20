# MusicSync 底层重做设计（SQLite + 流水线重写）

**日期**: 2026-05-20
**状态**: 待批准（用户已口头同意，待书面 review）
**前置**: 本设计取代旧 CSV 模型；与 `2026-05-20-musicsync-sync-fixes-design.md` 是替代关系，不是补充。

## 背景

旧 `csv/song_mappings.csv` 有三类数据腐败:

| 类别 | 数量 | 现象 |
|---|---|---|
| A1 | 132 行 | 同一个 qq_id 在多行（一行 `manual+synced=1`，一行 `qq_only`）。`csv_database._qq_index` 防重失败 |
| A2 | 19 行 | 同名同歌手不同专辑被识别为不同歌（例：`milet/Tell me` 在 `Prover/Tell me` 和 `eyes` 各一行） |
| B | 10 行 | 两边都 `qq_only` 但其实是同一首 |

L3 (name+artist) 当前 dry-run only，导致冷门歌全 unmatched；L2 duration tolerance 仅 ±3s 对不同混音/版本 fail；unlike 同步缺安全阈值，cache miss 时风险大。

旧架构修不好这些问题——没有结构性的唯一约束，全部依赖应用层 `_qq_index` 内存索引，已被证实漏防。

## 决策

**全量重做**。一次合并而非分阶段。保留 `scripts/netease_api.py` 和 `scripts/qqmusic_api_v2.py` 两个 API 客户端不动（这是经验沉淀最重的资产），重写其余所有数据/匹配/流水线代码。

旧 CSV 通过迁移脚本一次性导入新 SQLite 数据库后归档（保留在 git history），从此不再被任何代码读写。

## 数据模型

存储: `data/musicsync.db` (SQLite 单文件)。`data/` 加进 `.gitignore`。CI 通过 GitHub Actions Cache 持久化（替换旧 CSV 的 cache key）。

### Schema

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE songs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key          TEXT NOT NULL,              -- clean_name + "|" + clean_artist
    name                   TEXT NOT NULL,
    artist                 TEXT NOT NULL,
    album                  TEXT,
    match_source           TEXT NOT NULL CHECK(match_source IN
                           ('manual','l0_canonical','l1_isrc','l2_lyrics','l3_name_artist','unmatched','migrated')),
    original_match_source  TEXT,                       -- 仅 migrated 行使用：保留原 CSV 的 match_source 用于审计
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX idx_songs_canonical ON songs(canonical_key);

CREATE TABLE platform_links (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id           INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL CHECK(platform IN ('netease','qq')),
    platform_track_id TEXT NOT NULL,
    platform_name     TEXT,
    platform_artist   TEXT,
    platform_album    TEXT,
    liked             INTEGER NOT NULL DEFAULT 1 CHECK(liked IN (0,1)),
    synced_at         TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (platform, platform_track_id)
);
CREATE INDEX idx_links_song_platform ON platform_links(song_id, platform);
CREATE INDEX idx_links_liked ON platform_links(platform, liked);

CREATE TABLE lyrics_cache (
    platform          TEXT NOT NULL CHECK(platform IN ('netease','qq')),
    platform_track_id TEXT NOT NULL,
    original          TEXT,
    translated        TEXT,
    cached_at         TEXT NOT NULL,
    PRIMARY KEY (platform, platform_track_id)
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- meta 存 schema_version, last_sync_at 等
```

### 设计要点

**`songs.canonical_key` 不 UNIQUE，仅索引**。一首 logical song 可对应多个 platform_links（不同专辑版本）。这是审查发现的关键修正：早期方案的 `UNIQUE(canonical_key)` 会让 milet/Tell me 在两个专辑版本被强制合并，等于丢数据。

**A1 的结构性防护**: `UNIQUE(platform, platform_track_id)`。同一个 qq_id 不可能挂两次。这是 SQLite 物理强制，不靠应用层。

**A2 的语义处理**: 不再当 bug 处理。两个不同 qq_id 的 milet/Tell me 各自挂在同一个 song 下，作为独立 platform_link。L0 查询 canonical_key 命中多条时按规则挑"主版本"（见后文同步规则）。

**`liked` 字段**: 取代旧 CSV 的 `synced`。语义是"该 platform_link 应在该平台 liked 列表里"。1=应该 liked；0=曾经 liked 但已被 unlike（保留行用于历史追溯）。

**`synced_at`**: 我们最后一次成功通过 API add 到该平台的 ISO 时间戳；NULL = 我们没主动同步过（例如这条 link 是从平台 fetch 出来的，不是我们 add 进去的）。

**lyrics_cache 进 DB**: 取消旧 `state/sync_state.json` 里的 `lyrics_cache` 字段。state.json 直接删除，`last_sync_at` 进 `meta` 表。**cleanup 检测 unlike 不再依赖 `prev_*_ids` 快照**——DB 里 `liked=1` 就是上次 sync 结束后的真值，本次 fetch 不在该平台即认定用户 unlike（前提是本次 fetch 通过完整性校验，见流水线 Step 2.5）。

**state.json 中现存的 lyrics_cache 迁移**: `migrate_csv_to_db.py` 读 `state/sync_state.json`，把 `lyrics_cache` 整段灌入 `lyrics_cache` 表，并删除 state.json。

**`original_match_source` 字段**: 仅迁移行写入，用于保留旧 CSV 的 `match_source`（`isrc` / `name_artist` / `qq_only` 等）。新建 song 时该字段为 NULL。审计错配时可查"这条记录原本是怎么匹配上的"。

## 派生快照

每次 sync 末尾自动 dump 一个 `csv/song_mappings_snapshot.csv`，read-only。仅供 git diff 和人类查阅，**任何代码不再 read**。

格式（一行一对 link，方便人类读）:
```csv
song_id,canonical_key,name,artist,album,
  ne_track_id,ne_liked,ne_synced_at,
  qq_track_id,qq_liked,qq_synced_at,
  match_source
```

snapshot 不进 CI cache（每次 sync 重新生成）。git 里 commit snapshot 是为了让 PR diff 能看出每次 sync 改了哪些行——这是 SQLite 二进制存储的可观测性补救。

## 迁移策略

一次性脚本 `scripts/migrate_csv_to_db.py`，跑完即丢（保留在 git history）。

**输入**: 旧 `csv/song_mappings.csv`（1021 行）
**输出**: `data/musicsync.db`

**合并 key**: 主合并 key 是 `qq_id` 和 `netease_id`，**不是 canonical_key**。这是审查发现的关键修正：A1 重复行的 `name` 字段在两边平台可能不同（例如 NetEase 写 `Love Story (Taylor's Version)`，QQ 写 `Love Story`），按 canonical_key 分桶根本不会合并它们。

**算法**:
1. Pass 1: 读 CSV 全部行进内存。
2. Pass 2: 对每一行，确定它对应的 (netease_id, qq_id) 对。
3. Pass 3: 用 union-find 合并—— 任何两行只要共享 netease_id 或 qq_id 就合并到同一个 song 簇。
4. 对每个簇:
   - `match_source` 一律标记为 `migrated`（除非原始是 manual——保留 manual 标记）
   - `original_match_source` 记录簇里最高优先级（manual > l1/l2/l3 > qq_only > unmatched）行的原 `match_source`（manual 行该字段也填 'manual' 便于审计统一）
   - `name`/`artist`/`album` 取该最高优先级行的值
   - 把所有 distinct (platform, platform_track_id) 写进 `platform_links`，`liked=1`
5. 写入 SQLite，每条 INSERT 触发的 UNIQUE 冲突视为迁移逻辑 bug，整体 abort。

**预期结果**: 1021 行 CSV → 约 855 song 行 + 约 1400 platform_link 行（双边 link）。
**验证**: 迁移后 `scripts/verify_migration.py` 跑断言:
- 所有原 CSV 的 (netease_id, qq_id) 对在 DB 里都能找到对应的 song
- 所有 manual 行被保留为 `match_source='manual'`
- A1 数 = 0（即不存在两个 platform_links 共享 (platform, platform_track_id)）

## 匹配算法

`scripts/matcher.py` 扩展为四层。**关键区分两个使用场景**:

- **场景 A — Canonicalize（Step 3）**: 已知 (platform, platform_track_id) 来自本次 fetch，问"这个 track 应该挂在 DB 里哪个 song 下"。这是本地查询，**只用 L0**。
- **场景 B — Match unlinked（Step 5）**: 已知一个 song 单边 link（例如只有 NetEase），要在对面平台（QQ）通过搜索 API 找对应 track。**只用 L1/L2/L3**，不用 L0 — 因为 L0 命中的是 *本地另一个 song*，不是对面平台的真实 track，错误复用会导致 album 错配（例如把 eyes 版的 Tell me 同步成 Prover 版的 NetEase link）。

```
L0 canonical_key  ── 仅用于 Step 3 canonicalize：
                     fetch 到一个新 (platform, platform_track_id)，
                     若 platform_links 里已存在该 (platform, platform_track_id) → 更新该行
                     否则在 songs 表查 platform_name+platform_artist 的 canonical_key
                       → 命中 0 条：新建 song + 新建 platform_link
                       → 命中 1 条：在该 song 下新建 platform_link
                       → 命中 ≥2 条：走"挑主版本"规则选 song，新建 platform_link
L1 ISRC           ── Step 5 用：保留代码路径；QQ API 暂不可用
L2 lyrics+duration── Step 5 用：lyrics ≥0.6 AND duration ±15s（旧 ±3s 放宽）
L3 name+artist    ── Step 5 用：exact match 升级为可执行
                       Ambiguity 安全网：搜索结果有 ≥2 个相同 canonical_key 候选 → 跳过让人工 review
```

**L0 主版本挑选规则**（Step 3 canonicalize 时，canonical_key 命中多个 song 决定挂在哪个 song 下）:
1. 优先 `match_source='manual'` 的 song
2. 否则优先存在 `liked=1` 且 `synced_at IS NOT NULL` 的 platform_link 的 song
3. 否则取 `created_at` 最早的 song
4. 同分时取 song.id 最小的

**L2 ambiguity**: lyrics+duration 同时放宽（旧 lyrics≥0.6 + duration±3s → 新 lyrics≥0.6 + duration±15s）有乘法误匹配风险。引入额外保护：若候选歌词长度 <100 字符，仍需 duration±5s。

**L3 ambiguity 实现**: 对搜索 API 返回的 candidate list 在 in-memory 计算 `clean_name + clean_artist`；若有 ≥2 条相同则跳过；恰好 1 条则执行。

**Step 5 UNIQUE 冲突处理**（关键边界 case）: L1/L2/L3 在对面平台搜到一个候选 track_id X，但 `platform_links` 里 X 已被另一个 song B 占用（A2 历史造成的双 song 同 track_id 不应存在，但搜索 API 可能命中跨 song 的边界）。处理路径:

1. 在执行 INSERT platform_link 之前，先查 `SELECT song_id FROM platform_links WHERE platform=? AND platform_track_id=?`
2. 若已存在且 `song_id != current_song.id` → **跳过该候选**，记 warn 日志（含 song_a.id / song_b.id / track_id），继续 L2/L3 找下一个候选
3. 若所有候选都冲突或耗尽 → 该 song 标 `unmatched`
4. 不依赖 SQLite UNIQUE 异常传播——异常砸到主事务回滚成本太高（参见事务边界节）

预期此分支极少触发；若 warn 日志频繁出现，说明 canonical_key 计算或迁移合并有 bug，需要人工介入。

## CI Bootstrap（cache miss 自动恢复）

GitHub Actions Cache 是 best-effort 存储（7 天不活跃 eviction、容量上限、key 失效都可能丢）。每次 cache miss 都人工重跑迁移不现实——会让无人值守 cron 随机停摆。

设计 `scripts/bootstrap_db_from_snapshot.py`，CI 启动时若 DB 不存在则自动跑:

```
sync.yml restore cache → DB 存在？
  yes → continue normally
  no  → 检查 csv/song_mappings_snapshot.csv 存在？
         yes → 跑 bootstrap → 继续 sync（首次跑视为 last_sync_at=NULL，自动跳过 Step 2.5 sanity）
         no  → abort（首次部署，需要先本地跑 migrate）
```

`bootstrap_db_from_snapshot.py` 比完整迁移脚本简单：snapshot CSV 格式已扁平化（一行一对 link），不需要 union-find，直接逐行 INSERT 即可:
- 读 snapshot CSV
- 创建 schema
- 对每行 INSERT song (id 用 snapshot 的 song_id) + INSERT ne/qq platform_link（如果 track_id 非空）
- `original_match_source` 留 NULL（snapshot 没保留这个信息）

bootstrap 后的 DB 状态与迁移产出在功能上等价：所有 song / platform_link / liked 状态都有；唯一损失的是 lyrics_cache（snapshot 不含），需要靠下次 sync 重新累积。

**workflows/sync.yml 调整**:
```yaml
- uses: actions/cache/restore@v4
  with:
    path: data/musicsync.db
    key: musicsync-db-${{ github.run_id }}
    restore-keys: musicsync-db-
- name: Bootstrap from snapshot if DB missing
  run: |
    if [ ! -f data/musicsync.db ]; then
      python scripts/bootstrap_db_from_snapshot.py
    fi
```

## 流水线（10 步）

```
1. Auth check                 两边登录，否则 abort
2. Fetch both sides           拉 NetEase liked + QQ liked 当前列表
2.5 Fetch sanity check        对每个平台，比较 fetch 数 vs DB 中该平台 liked=1 的总数
                                条件: meta.last_sync_at IS NOT NULL（即非首跑）
                                若 (db_liked_count - fetch_count) / db_liked_count > FETCH_DROP_THRESHOLD
                                  → abort 并打印两边数字（避免 NetEase cookie 半失效导致的批量误 unlike）
                                FORCE_FULL_SYNC=true 时跳过此检查
3. Canonicalize               UPSERT songs (by id) + platform_links (by platform+track_id)
                                新 link 默认 liked=1；已存在 link 更新 platform_*/liked
4. Diff vs DB                 找出:
                                (a) 单边 link 的 song（需要双向同步 ADD）
                                (b) 双边都 link 但 liked=1 且本次 fetch 不在该平台 → 用户 unlike
                                (c) 双边都 link 但本次 fetch 都在 → no-op
5. Match unlinked             对 (a) 跑 L1→L2→L3
                                成功 → 创建对面 platform_link (liked=0, synced_at=NULL, 待 step 8 add)
                                失败 → song.match_source='unmatched'
                                每次成功 match 后立即 COMMIT（细事务，见事务边界节）
6. Plan                       构造 action list:
                                ADD_TO_NE(track_id), ADD_TO_QQ(track_id), UNLIKE_NE(id), UNLIKE_QQ(id)
                                打印 plan summary
                                DRY_RUN=true → 到此为止，跳到 9
7. Safety gate                |UNLIKE_NE| + |UNLIKE_QQ| > UNLIKE_ABORT_THRESHOLD (默认 10)
                                → cleanup 部分整体跳过 + 警告日志
                                → ADD 部分仍执行（爆炸半径小）
                                FORCE_FULL_SYNC=true 时 cleanup 整段跳过
8. Execute                    按 plan 调 API；逐条更新 platform_links.liked / synced_at
                                每步独立的 try/except；单条失败不阻塞下一条
                                每条成功后立即 COMMIT（细事务，见事务边界节）
9. Snapshot                   dump csv/song_mappings_snapshot.csv
                                更新 meta.last_sync_at（独立 commit）
```

**事务边界（细粒度）**: SQLite WAL 模式下每个原子操作独立 commit，**不**包整个 sync 在单事务里。具体:
- Step 3 canonicalize: 每条 UPSERT 独立 commit（短事务，仅一两行）
- Step 5 match unlinked: 每首歌 match 完（成功/失败）后立即 commit
- Step 8 execute: 每条 API 调用成功后立即更新 `platform_links.liked` / `synced_at` 并 commit；失败仅日志，不动 DB
- `lyrics_cache` 写入: 用独立的 autocommit connection，与主流程完全解耦——歌词 fetch 是只增缓存，与 sync 原子性无关
- Step 9 snapshot dump 是 read-only，不需事务

**收益**: sync 中途 crash（NetEase 限流、API 异常、网络断）时已完成的工作全部保留：已 match 的歌不丢、已 fetch 的歌词不丢、已 add 到对面平台的状态在 DB 里准确反映（与平台实际状态一致）。下次 sync 从断点继续而非重跑 30 分钟。

**代价**: 失去"sync 是原子的"这个简单语义。例如 sync 中途 crash 时，`csv/song_mappings_snapshot.csv` 不会被生成——但 DB 是真源，下次 sync 再 dump 即可。Step 8 部分成功也是现实情况：API 调用本来就不是事务的，新设计只是停止假装它是。

**与旧 8 步的对应**: 旧 Step 7（反向同步）和 Step 4（正向匹配）合并为新 Step 5（match unlinked）；新 Step 2.5（fetch sanity）/ Step 6（Plan）/ Step 7（Safety gate）是新增层。

## 配置与环境变量

```
DRY_RUN                  默认 true     不变。Step 6 后退出
REVERSE_BATCH            默认 25       不变。Step 5 中限制本轮 NetEase 搜索次数
FORCE_FULL_SYNC          默认 false    Step 2.5 sanity + Step 7 cleanup 整段跳过（首次启动 / cache miss 兜底）
UNLIKE_ABORT_THRESHOLD   默认 10       Step 7 阈值
FETCH_DROP_THRESHOLD     默认 0.15     Step 2.5 阈值（fetch 数比 DB 少 >15% 则 abort）
DB_PATH                  默认 data/musicsync.db
```

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `scripts/db.py` | 新建 | SQLite schema 创建 + UPSERT helpers + 查询 helpers |
| `scripts/migrate_csv_to_db.py` | 新建 | 一次性迁移脚本（运行后保留在 git，不再调用） |
| `scripts/verify_migration.py` | 新建 | 迁移后断言验证 |
| `scripts/bootstrap_db_from_snapshot.py` | 新建 | CI cache miss 时自动从 snapshot CSV 重建 DB |
| `scripts/matcher.py` | 重写 | L0 + L1 + L2 + L3 四层；duration ±15s；ambiguity 安全网；Step 5 UNIQUE 冲突跳过 |
| `scripts/sync.py` | 重写 | 10 步流水线，细粒度事务 |
| `scripts/netease_api.py` | 不动 | |
| `scripts/qqmusic_api_v2.py` | 不动 | |
| `scripts/csv_database.py` | 删除 | 完全被 db.py 取代 |
| `data/musicsync.db` | 新建 | SQLite 真源；进 .gitignore；CI cache 持久化 |
| `csv/song_mappings.csv` | 归档 | 迁移完后保留为最后一次状态，提交到 git，不再被任何代码读写 |
| `csv/song_mappings_snapshot.csv` | 新建 | 派生只读快照；每次 sync 末尾 dump |
| `state/sync_state.json` | 删除 | lyrics_cache 迁进 DB；last_sync_at 进 meta 表；prev_*_ids 不再需要（DB liked 字段直接反映"应同步状态"） |
| `tests/test_db.py` | 新建 | UNIQUE 约束的反向测试 |
| `tests/test_migrate.py` | 新建 | 迁移脚本对 fixture（含 132 个 A1 + 19 个 A2 + 10 个 B）测试 |
| `tests/test_matcher.py` | 新建 | L0/L1/L2/L3 各 fixture + 主版本规则全分支 + Step 5 UNIQUE 冲突跳过 |
| `tests/test_sync_plan.py` | 新建 | mock API，断言 Step 6 plan 输出；含 fetch partial（少 5 条）的 sanity check 触发 |
| `tests/test_netease_api_http.py` | 保留 | 现有 HTTP 集成测试不动 |
| `.github/workflows/sync.yml` | 修改 | cache key 从 csv 改为 db；加 bootstrap fallback 步骤 |
| `.gitignore` | 修改 | 加 `data/` |

## 测试策略

按风险等级，全部本地跑（用户偏好不触发 CI）:

1. **`test_db.py`**: 反向测试 UNIQUE 约束。试图在 platform_links 插入重复 (platform, platform_track_id) → 应抛 IntegrityError。
2. **`test_migrate.py`**: 用 fixture CSV（构造 132 个 A1 + 19 个 A2 + 10 个 B 缩小样本）跑迁移，断言: 行数下降量符合预期；所有 manual 保留；`original_match_source` 正确填充；UNIQUE 约束未被触发；union-find 合并正确。
3. **`test_matcher.py`**: 必须覆盖 4 个主版本规则分支:
   - 两个 song 同 canonical_key，一个 manual → 选 manual
   - 两个非 manual，一个有 `liked=1 + synced_at NOT NULL` → 选有 synced_at
   - 都无 synced_at → 选 created_at 更早
   - created_at 相同 → 选 song.id 更小
   另外: L3 ambiguity 跳过；L2 短歌词时收紧 duration；**Step 5 UNIQUE 冲突跳过**（候选 track_id 已挂在另一 song 下 → 跳过，记 warn，找下一个）。
4. **`test_sync_plan.py`**: mock netease_api/qqmusic_api，构造 fetch 返回 → 断言 Step 6 plan 的 ADD/UNLIKE 计数。**关键 fixture**: fetch 返回比 DB 少 5 条（在 ABORT_THRESHOLD 之下但触发 sanity check）→ 断言 Step 2.5 abort。
5. **本地 dry-run 端到端**: 真跑 `python scripts/sync.py`，对比 plan 输出与现有 CSV 状态的预期 diff。
6. **本地小批量真跑**: `DRY_RUN=false REVERSE_BATCH=5 python scripts/sync.py`，验证至少 1 首歌被双向 add。
7. **bootstrap 验证**: 手动删除 `data/musicsync.db` → 跑 `python scripts/bootstrap_db_from_snapshot.py` → 跑 sync.py dry-run → 对比 plan 与有 DB 时是否一致。
8. **回归对照**: 跑完后用 `scripts/verify_migration.py` 比对：旧 CSV 里所有 manual 映射在新 DB 里都还在。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 迁移脚本 bug 导致数据丢失 | 旧 CSV 不删（仅停止读写），保留在 git history；迁移失败可 git revert + 重跑 |
| SQLite 文件在 Actions Cache 损坏或丢失（CI cache miss） | `bootstrap_db_from_snapshot.py` 自动从 snapshot CSV（在 git 里）重建 DB；snapshot 同时是人类可读对照 + CI 灾备源。仅首次部署需要本地跑 migrate |
| L3 升级导致冷门同名异曲被错配 | Ambiguity 安全网（≥2 候选则跳过）；executed 操作在 snapshot CSV 里可见，可发现后人工修正 |
| L2 ±15s 放宽导致短歌误匹配 | 短歌词（<100 字符）保留 ±5s 收紧；snapshot 可见 |
| Step 5 搜索结果与已有 platform_link 冲突（UNIQUE 违反） | 显式 SELECT 预检 + 跳过冲突候选 + warn 日志；不依赖异常传播 |
| Fetch 列表不完整导致批量误 unlike（NetEase cookie 半失效场景） | Step 2.5 fetch sanity check：fetch 数比 DB 少 >FETCH_DROP_THRESHOLD（默认 15%）则 abort |
| sync 中途 crash 后重跑代价大 | 细粒度事务：每条 match / API 成功后立即 commit；下次 sync 从断点继续 |
| 多源真相风险 | 明确 contract：DB 唯一真源；snapshot 衍生（兼任 CI 灾备源）；state.json 已删除 |
| 一锅炖 PR 太大难审 | 5 个 task 独立 commit，每 commit 后 sync.py 至少 dry-run 不 crash |

## 不做的取舍

- **不**保留旧 csv_database.py 的兼容层。删干净。
- **不**给 SQLite 加 ORM（SQLAlchemy 等）。stdlib `sqlite3` 够用，加依赖反而是噪音。
- **不**保留 state/sync_state.json。`lyrics_cache` 迁进 DB；`last_sync_at` 进 `meta` 表；`prev_*_ids` 不再需要（DB liked 字段直接反映"应同步状态"）。
- **不**改 NetEase 速率参数（30s 启动、5s per search、REVERSE_BATCH=25）。封禁周期 7-10 天的代价在这次重做里不能再踩。
- **不**实现"用户在两个平台都喜欢同一首歌的不同专辑版本"的合并 UI 或 prompt。snapshot CSV 是观测出口；用户想合并时手动改 DB（写一个 admin script 即可，但不在本次范围）。
- **不**包整个 sync 在单事务里。细粒度事务的代价（失去"sync 是原子的"语义）已被 NetEase 限流场景下的重跑成本压倒。

## 实施顺序（写计划阶段细化）

预期分 5 个 task，每 task 一个 commit:

1. `db.py` schema 创建 + 基础 helpers + `test_db.py`
2. `migrate_csv_to_db.py` + `test_migrate.py` + `verify_migration.py` + `bootstrap_db_from_snapshot.py`
3. `matcher.py` 四层重写 + `test_matcher.py`（含主版本规则全分支 + UNIQUE 冲突跳过测试）
4. `sync.py` 10 步流水线整体重写 + `test_sync_plan.py`（含 fetch partial sanity check 触发）+ 删除 `csv_database.py`/`state/sync_state.json`
5. `.github/workflows/sync.yml` cache key + bootstrap 步骤 + `.gitignore` 调整 + 端到端真跑验证

**Task 4 合并说明**: 早期设计把 sync.py 切成 Step 1-3 / 4-6 / 7-9 三个 task，但中间 commit 因新逻辑读 DB、旧逻辑读 CSV 而无法运行（git bisect 失效）。改为单 commit 全量重写，保证每个 commit 都是可运行状态。

每 task 独立可回滚，并且每 task commit 后 `python scripts/sync.py` 至少 dry-run 不 crash。
