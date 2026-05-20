# MusicSync 同步修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复反向同步（QQ→网易）每次返回 0 matched 的回归，让 712 个 `qq_only` 行能正常流动；同时加上 4xx/5xx 容错和更清晰的 dry-run summary。

**Architecture:** 三个核心改动在两个文件里：(1) `netease_api.py` 的 `search()` 改用 POST，并让 `_request` 在 HTTPError 时 raise 让调用方决定；(2) `sync.py` Step 7 反向同步循环捕获 HTTPError，做有限指数退避重试并在连续 3 次失败后 abort；(3) `sync.py` summary 区分 dry-run 和真实执行。

**Tech Stack:** Python 3.10+, `requests`, NeteaseCloudMusicApiEnhanced (Node.js), pytest 风格断言（仓库无 pytest 配置）

**前置条件：** 实施前请阅读对应规范 `docs/superpowers/specs/2026-05-20-musicsync-sync-fixes-design.md`。

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `scripts/netease_api.py` | 修改 | `_request` 抛 HTTPError；`search` 已经走 POST 之外的调用方加 `try/except HTTPError` 兜底 |
| `scripts/sync.py` | 修改 | Step 7 加连续失败计数 + 退避 helper；summary 区分 dry-run |
| `tests/test_netease_api_http.py` | 修改 | 现有 `test_search` 已是 POST，补一条注释解释为什么必须 POST，避免回归 |

不新建任何文件。

---

## Task 1: 修复 `_request` 让它对 HTTPError 透传，对其他错误兜底

**Files:**
- Modify: `scripts/netease_api.py:66-84`

- [ ] **Step 1: 修改 `_request`，区分 HTTPError 和其他异常**

打开 `scripts/netease_api.py`，把 `_request` 方法（第 66-84 行）替换为：

```python
def _request(self, path: str, method: str = "GET", **params) -> dict:
    try:
        url = f"{self.base_url}{path}"
        if method.upper() == "POST":
            response = self._session.post(
                url, data=params, timeout=self._timeout,
            )
        else:
            response = self._session.get(
                url, params=params, timeout=self._timeout,
            )
        response.raise_for_status()
        self._rate_limit()
        data = response.json()
        # Enhanced API wraps some responses in {"data": {...}}
        return data.get("data", data)
    except requests.HTTPError as e:
        print(f"HTTP error: {e}")
        raise  # let caller decide (search wants to fail-fast; others swallow)
    except Exception as e:
        print(f"Request failed: {e}")
        return {}
```

- [ ] **Step 2: 给所有其他调用方包一层 `try/except requests.HTTPError`**

`search` 不包（它需要透传给 sync.py Step 7）。其他四个调用方原本依赖 `_request` 永远返回 dict，现在需要兜底。

修改 `get_liked_track_ids`（第 86-94 行）为：

```python
def get_liked_track_ids(self) -> list[int]:
    if not self.uid:
        return []

    try:
        result = self._request("/likelist", uid=str(self.uid))
    except requests.HTTPError:
        return []
    if result.get("code") != 200:
        return []

    return result.get("ids", [])
```

修改 `get_track_details_batch`（第 96-118 行）为（仅前几行变）：

```python
def get_track_details_batch(self, track_ids: list[int]) -> list[dict]:
    if not track_ids:
        return []

    ids_str = ",".join(str(tid) for tid in track_ids)
    try:
        result = self._request("/song/detail", ids=ids_str)
    except requests.HTTPError:
        return []

    if result.get("code") != 200:
        return []
    # ... 后续不变
```

修改 `add_to_liked`（第 159-161 行）为：

```python
def add_to_liked(self, track_id: str) -> bool:
    try:
        result = self._request("/like", method="POST", id=track_id, like="true")
    except requests.HTTPError:
        return False
    return result.get("code") == 200
```

修改 `remove_from_liked`（第 163-165 行）为：

```python
def remove_from_liked(self, track_id: str) -> bool:
    try:
        result = self._request("/like", method="POST", id=track_id, like="false")
    except requests.HTTPError:
        return False
    return result.get("code") == 200
```

修改 `get_lyric`（第 167-181 行）的 `_request` 行为：

```python
def get_lyric(self, track_id: str) -> tuple[str, str]:
    """Fetch lyrics for a track. Returns (original, translated) plain text."""
    import re
    try:
        result = self._request("/lyric", id=track_id)
    except requests.HTTPError:
        return "", ""
    # ... 后续不变
```

`search`（第 134-157 行）不动 try/except，让它原样把 HTTPError 抛给 caller。但是要注意：当前 `search` 已经走 `_request("/search", method="POST", ...)`（这是上一次提交修复的；如果你看到的版本仍是 `method="GET"`，把它也一并改成 `method="POST"`）。

- [ ] **Step 3: 启动 NetEase 服务并跑现有测试，确认没破坏其他 caller**

在仓库根目录另开一个终端运行：

```powershell
bash scripts/start_netease_api.sh
```

确认它监听 `http://localhost:3000` 后，回到主终端运行：

```powershell
python tests\test_netease_api_http.py
```

Expected 输出：

```
✓ test_server_startup passed
✓ test_login_status passed
✓ test_search passed
✓ test_song_detail passed

All tests passed!
```

如果 `test_search` 失败（返回 405 或非 200），先确认 `search` 里 `_request` 用的是 `method="POST"`。

- [ ] **Step 4: Commit**

```powershell
git add scripts/netease_api.py
git commit -m "refactor: raise HTTPError from NetEaseAPI._request, swallow in non-search callers"
```

---

## Task 2: 给 sync.py 加 search-with-backoff helper

**Files:**
- Modify: `scripts/sync.py`（在 `import` 区下方加 helper；Step 7 暂时不调用，下个 task 才接进去）

- [ ] **Step 1: 在 sync.py 顶部加 `import requests`**

打开 `scripts/sync.py`，找到 `import time` 之类的导入行（约第 1-30 行）。如果还没有 `import requests`，在合适位置加上：

```python
import requests
```

- [ ] **Step 2: 在 `main()` 函数定义之前加 helper 函数**

在 `REVERSE_BATCH = int(os.getenv("REVERSE_BATCH", "0"))` 那行（约 30 行）下方、第一个函数定义之前，加入这块代码：

```python
SEARCH_FAILURE_THRESHOLD = 3
BACKOFF_SECONDS = [30, 60, 120]


class ReverseSearchState:
    """Tracks consecutive failures + backoff progression across the reverse-sync loop."""

    def __init__(self):
        self.consecutive_failures = 0
        self.backoff_idx = 0
        self.aborted = False

    def record_success(self):
        self.consecutive_failures = 0
        self.backoff_idx = 0

    def record_failure(self) -> int:
        """Returns the wait time in seconds for the next retry."""
        self.consecutive_failures += 1
        wait = BACKOFF_SECONDS[min(self.backoff_idx, len(BACKOFF_SECONDS) - 1)]
        self.backoff_idx += 1
        return wait

    @property
    def should_abort(self) -> bool:
        return self.consecutive_failures >= SEARCH_FAILURE_THRESHOLD


def search_netease_with_backoff(ne_api, query: str, state: ReverseSearchState, limit: int = 3) -> list[dict]:
    """Search NetEase with retry on HTTPError. Returns [] on persistent failure.

    Sets state.aborted=True if SEARCH_FAILURE_THRESHOLD consecutive failures hit.
    """
    for attempt in range(2):  # original + 1 retry
        try:
            results = ne_api.search(query, limit=limit)
            state.record_success()
            return results
        except requests.HTTPError as e:
            wait = state.record_failure()
            status = e.response.status_code if e.response is not None else "?"
            if state.should_abort:
                print(f"  ABORT reverse sync — {SEARCH_FAILURE_THRESHOLD} consecutive search failures (last HTTP {status})")
                state.aborted = True
                return []
            print(f"  HTTP {status} on search — waiting {wait}s before retry")
            time.sleep(wait)
    return []
```

- [ ] **Step 3: 运行 sync.py 的导入完整性检查**

```powershell
python -c "import scripts.sync; print('imports ok')"
```

Expected: 输出 `imports ok`，无 traceback。如果报错，按提示修。

- [ ] **Step 4: Commit**

```powershell
git add scripts/sync.py
git commit -m "feat: add search-with-backoff helper for reverse sync"
```

---

## Task 3: 把 helper 接进 Step 7 反向同步循环

**Files:**
- Modify: `scripts/sync.py:516-569`

- [ ] **Step 1: 替换 Step 7 的循环体**

打开 `scripts/sync.py`，找到 Step 7 循环（第 516-569 行）。当前代码是：

```python
        for qq_key, qq_row in qq_only:
            qq_name = qq_row.get("qq_name", qq_row.get("name", ""))
            qq_artist = qq_row.get("qq_artist", qq_row.get("artist", ""))
            qq_id = qq_row.get("qq_id", "")

            # Build a qq_track dict for matcher
            qq_track = {"id": qq_id, "name": qq_name, "artist": qq_artist}

            ne_match = None
            level = ""

            # 1) Try L1 against existing NetEase liked tracks
            for ne_t in ne_tracks:
                if match_l1(ne_t, qq_track):
                    ne_match = ne_t
                    level = "isrc"
                    break

            # 2) Search NetEase if no match in liked tracks
            if not ne_match:
                time.sleep(5)  # rate-limit: avoid 405 errors
                try:
                    results = ne_api.search(f"{qq_name} {qq_artist}", limit=3)
                    if results:
                        # Try L1 first
                        for r in results:
                            if match_l1(qq_track, r):
                                ne_match = r
                                level = "isrc"
                                break
                        # Then try lyrics+duration
                        if not ne_match:
                            qq_lyrics = fetch_lyrics_with_cache(state, qq_api, qq_id)
                            qq_track["_lyrics"] = qq_lyrics
                            for r in results:
                                ne_lyrics = fetch_lyrics_with_cache(state, ne_api, str(r["id"]))
                                r["_lyrics"] = ne_lyrics
                                if match_l2(qq_track, r):
                                    ne_match = r
                                    level = "lyrics_duration"
                                    break
                        # Finally try name+artist
                        if not ne_match:
                            best, best_lv = match_track(qq_track, results)
                            if best_lv:
                                ne_match = best
                                level = "name_artist"
                except Exception:
                    pass

            if ne_match and level:
                rev_matched.append((qq_key, ne_match, level))
            else:
                rev_unmatched.append((qq_key, qq_track))
```

替换为：

```python
        search_state = ReverseSearchState()
        for qq_key, qq_row in qq_only:
            if search_state.aborted:
                print(f"  Skipping remaining {len(qq_only) - len(rev_matched) - len(rev_unmatched)} tracks after abort")
                break

            qq_name = qq_row.get("qq_name", qq_row.get("name", ""))
            qq_artist = qq_row.get("qq_artist", qq_row.get("artist", ""))
            qq_id = qq_row.get("qq_id", "")

            # Build a qq_track dict for matcher
            qq_track = {"id": qq_id, "name": qq_name, "artist": qq_artist}

            ne_match = None
            level = ""

            # 1) Try L1 against existing NetEase liked tracks
            for ne_t in ne_tracks:
                if match_l1(ne_t, qq_track):
                    ne_match = ne_t
                    level = "isrc"
                    break

            # 2) Search NetEase if no match in liked tracks
            if not ne_match:
                time.sleep(5)  # rate-limit: avoid 405 errors
                results = search_netease_with_backoff(
                    ne_api, f"{qq_name} {qq_artist}", search_state, limit=3,
                )
                if search_state.aborted:
                    rev_unmatched.append((qq_key, qq_track))
                    continue
                if results:
                    # Try L1 first
                    for r in results:
                        if match_l1(qq_track, r):
                            ne_match = r
                            level = "isrc"
                            break
                    # Then try lyrics+duration
                    if not ne_match:
                        qq_lyrics = fetch_lyrics_with_cache(state, qq_api, qq_id)
                        qq_track["_lyrics"] = qq_lyrics
                        for r in results:
                            ne_lyrics = fetch_lyrics_with_cache(state, ne_api, str(r["id"]))
                            r["_lyrics"] = ne_lyrics
                            if match_l2(qq_track, r):
                                ne_match = r
                                level = "lyrics_duration"
                                break
                    # Finally try name+artist
                    if not ne_match:
                        best, best_lv = match_track(qq_track, results)
                        if best_lv:
                            ne_match = best
                            level = "name_artist"

            if ne_match and level:
                rev_matched.append((qq_key, ne_match, level))
            else:
                rev_unmatched.append((qq_key, qq_track))
```

关键变化：
- 入口判断 `search_state.aborted` 提前 break
- 把 `try/except Exception: pass` 整段拆掉，让 HTTPError 由 helper 处理；其他真正的异常（程序员错误、bug）保持暴露
- helper 返回 `[]` + `aborted=True` 表示触发熔断，本轨道按 unmatched 处理后继续 break

- [ ] **Step 2: 验证语法**

```powershell
python -c "import scripts.sync; print('imports ok')"
```

Expected: `imports ok`。

- [ ] **Step 3: dry-run 端到端验证**

确保 NetEase Node 服务在跑（Task 1 Step 3 起的），运行：

```powershell
$env:DRY_RUN="true"; python scripts\sync.py
```

Expected:
- Step 7 不再产生连续 `Request failed: 405 Client Error...`
- `Lyrics+Duration matches:` 或 `Name+Artist matches:` 行至少有一个非零数字
- `[Step 7]` 完整跑完，不会卡住

如果实际触发了熔断（连续 3 次 HTTP 错误），会看到 `ABORT reverse sync` 然后 step 提前结束，这也是预期行为。

- [ ] **Step 4: Commit**

```powershell
git add scripts/sync.py
git commit -m "fix: reverse sync uses HTTPError-aware backoff with 3-strike abort"
```

---

## Task 4: Summary 区分 dry-run 和真实执行

**Files:**
- Modify: `scripts/sync.py:691-704`

- [ ] **Step 1: 替换 summary 字符串构造**

打开 `scripts/sync.py`，找到 `# --- Summary ---` 块（第 691 行附近）。把第 693-703 行的 `summary = f"""...` 整段替换为：

```python
    if DRY_RUN:
        forward_line = (
            f"**Forward (Ne→QQ) [DRY-RUN]:** would add {len(matched_l1) + len(matched_l2)} "
            f"({isrc_count} ISRC + {len(matched_l2)} lyrics + {manual_count} manual) "
            f"/ {len(matched_l3)} name_artist preview / {len(unmatched_new)} unmatched"
        )
        reverse_line = (
            f"**Reverse (QQ→Ne) [DRY-RUN]:** would add {rev_matched_count} matched "
            f"/ {rev_skipped} already liked"
        )
    else:
        forward_line = (
            f"**Forward (Ne→QQ):** {executed_l1 + executed_l2} executed "
            f"({isrc_count} ISRC + {len(matched_l2)} lyrics) / "
            f"{len(failed_l1) + len(failed_l2)} failed / "
            f"{len(matched_l3)} dry-run / {len(unmatched_new)} unmatched"
        )
        reverse_line = (
            f"**Reverse (QQ→Ne):** {rev_executed} executed / "
            f"{rev_skipped} already liked / {len(rev_failed)} failed / "
            f"{rev_matched_count} matched"
        )

    summary = f"""## MusicSync {'DRY-RUN' if DRY_RUN else 'Complete'}
**Time:** {now}
**NetEase liked:** {len(ne_tracks)} tracks
**QQ Music liked:** {len(qq_tracks)} tracks
{forward_line}
{reverse_line}
**Unliked cleanup:** {ne_removed_executed} Ne→QQ / {qq_removed_executed} QQ→Ne
**Dead unmatched:** {dead_count}

**CSV database:** `{csv_path}` — edit `match_source` to `manual` and fill `qq_id` to map unmatched tracks.
"""
```

- [ ] **Step 2: 在 dry-run 跑通后看 summary 输出**

```powershell
$env:DRY_RUN="true"; python scripts\sync.py
```

到 `Done.` 之前会通过 `write_summary` 输出。在终端打印里搜 `[DRY-RUN]`：

Expected:
- `**Forward (Ne→QQ) [DRY-RUN]:** would add ...`
- `**Reverse (QQ→Ne) [DRY-RUN]:** would add ...`

- [ ] **Step 3: Commit**

```powershell
git add scripts/sync.py
git commit -m "feat: summary line distinguishes dry-run preview from real execution"
```

---

## Task 5: 给 `test_search` 加注释，固化 POST 不可回退

**Files:**
- Modify: `tests/test_netease_api_http.py:29-40`

仓库里 `test_search` 已经用 POST，但没有任何代码或注释提醒未来不要改回 GET。补一条注释作为回归围栏。

- [ ] **Step 1: 替换 `test_search` docstring**

打开 `tests/test_netease_api_http.py`，把 `test_search`（29-40 行）替换为：

```python
def test_search():
    """Test /search endpoint with POST.

    REGRESSION GUARD: NeteaseCloudMusicApiEnhanced returns 405 Method Not Allowed
    on GET /search after a MUSIC_U cookie is set (i.e. after login). All callers
    in scripts/netease_api.py must use POST. See
    docs/superpowers/specs/2026-05-20-musicsync-sync-fixes-design.md
    """
    response = requests.post(
        f"{BASE_URL}/search",
        data={"keywords": "周杰伦", "limit": 5},
        timeout=10,
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("code") == 200
    assert "result" in data
    assert "songs" in data["result"]
```

- [ ] **Step 2: 运行 `tests/test_netease_api_http.py`**

```powershell
python tests\test_netease_api_http.py
```

Expected: 全部通过（NetEase Node 服务必须在跑）。

- [ ] **Step 3: Commit**

```powershell
git add tests/test_netease_api_http.py
git commit -m "test: document POST-only contract for /search regression guard"
```

---

## Task 6: 端到端小批量真跑验证

**Files:** 无修改，仅验证。

- [ ] **Step 1: 真执行 5 首反向同步**

```powershell
$env:DRY_RUN="false"; $env:REVERSE_BATCH="5"; python scripts\sync.py
```

Expected:
- 不再 0 reverse matches；至少有几条 `OK (lyrics_duration):` 或 `OK (name_artist):` 行
- CSV 里相应 `qq_only` 行被 promoted（出现新的 `netease_id` 值）
- 末尾 summary 显示 `**Reverse (QQ→Ne):** N executed / ...`，N ≥ 1

- [ ] **Step 2: 查 CSV 是否被更新**

```powershell
git status
git diff csv\song_mappings.csv | Select-Object -First 30
```

Expected: `csv/song_mappings.csv` 出现在 modified 列表里，diff 里能看到至少一行从 `qq_only` 升级（`netease_id` 字段从空变成数字）。

- [ ] **Step 3: 提交真跑产生的 CSV 变更**

```powershell
git add csv/song_mappings.csv state/sync_state.json
git commit -m "data: small-batch reverse sync verifies fix (REVERSE_BATCH=5)"
```

如果什么都没匹配上（罕见但可能——5 首都是冷门歌），把 `REVERSE_BATCH` 调到 10 再跑一次；仍是 0 则回头排查 helper 或 search 链路。

---

## 非目标（明确不在本计划内）

- 不调整 `DRY_RUN` 默认值
- 不改 `matcher.py` 阈值或评分逻辑
- 不动 `csv_database.py` 的 schema
- 不动 Step 8 unliked cleanup
- 不调 `REVERSE_BATCH` 默认值或 `time.sleep(30)` / `time.sleep(5)` 这些已知保守值
- 不接 CI；本次完全在本地验证（用户偏好：测试在本地完成，从不未经允许触发 CI）

---

## 实施顺序与回滚

每个 Task 一个 commit，独立可回滚：

1. Task 1 — `_request` HTTPError 透传
2. Task 2 — backoff helper（独立加进去，先不接）
3. Task 3 — 把 helper 接进 Step 7
4. Task 4 — summary 区分 dry-run
5. Task 5 — test 注释
6. Task 6 — 小批量真跑

如果 Task 3 的 dry-run 验证发现 helper 有 bug，`git revert` Task 3 就能回到只有 Task 1+2 的状态，sync 仍能跑（只是反向同步又回到吞异常的旧行为）。
