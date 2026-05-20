# MusicSync 同步修复设计

**日期**: 2026-05-20
**作者**: 协同 Claude + MusicSync
**状态**: 待审批

## 背景

最近一次 dry-run summary 显示：

```
NetEase liked: 304   QQ Music liked: 857
Forward (Ne→QQ): 0 executed (0 ISRC + 6 lyrics) / 0 failed / 1 dry-run / 0 unmatched
Reverse (QQ→Ne): 0 executed / 0 already liked / 0 failed / 0 matched
```

CSV 里有 712 个 `qq_only` 行在等反向同步，但 `0 matched`。`sync_output.txt` 显示反向同步阶段每次搜索都返回 `405 Method Not Allowed`。

NeteaseCloudMusicApiEnhanced 在登录后强制要求对 `/search` 使用 POST，而 `scripts/netease_api.py:134` 的 `search()` 仍走 GET。`add_to_liked` 已经用 POST，唯独 `search` 漏改。结果：反向同步永远找不到候选，712 首 `qq_only` 永远不会流动。

此外，反向同步对 4xx/5xx 错误用 `try: ... except Exception: pass` 静默吞掉，会让一次封禁后剩余 24 次搜索徒劳消耗配额。

## 范围

修复 4 件事：

1. NetEase `search` 改 POST（P0，阻塞性）
2. 反向同步对连续失败 fail-fast，保护后续配额（P0）
3. DRY_RUN summary 输出更显眼地展示"would add"数量（P1）
4. 反向同步加 4xx/5xx 指数退避（P4，保守版本）

不在范围内：

- DRY_RUN 默认值
- 匹配引擎 (`matcher.py`)
- CSV schema
- Step 8 unliked cleanup

## Fix 1：NetEase `search` 改用 POST

**文件**: `scripts/netease_api.py:134`

**变更**：

```python
def search(self, keyword: str, limit: int = 10) -> list[dict]:
    result = self._request(
        "/search",
        method="POST",
        keywords=keyword,
        type="1",
        limit=str(limit),
    )
    ...
```

`_request` 已支持 method 参数（line 66），把 GET 改成 POST 即可，参数走 form-data。

**验证**：
- `tests/test_netease_api_http.py:29` 已注释"POST required when MUSIC_U cookie is set"
- 该测试本身在登录后用 POST 已能通过

## Fix 2：反向同步连续失败时跳出 Step 7

**文件**: `scripts/sync.py` Step 7 循环

**当前问题**：

```python
try:
    results = ne_api.search(...)
    ...
except Exception:
    pass
```

任何异常都吞掉，循环继续推进。如果 NetEase 在某次响应了 405/限流，剩余 24 次搜索会继续触发，可能加剧封禁。

**变更**：在 Step 7 循环内维护连续搜索失败计数器：

```python
consecutive_search_failures = 0
SEARCH_FAILURE_THRESHOLD = 3

for qq_key, qq_row in qq_only:
    ...
    if not ne_match:
        try:
            results = ne_api.search(...)
            consecutive_search_failures = 0  # 成功重置
        except Exception as e:
            consecutive_search_failures += 1
            print(f"  Search failed ({consecutive_search_failures}/{SEARCH_FAILURE_THRESHOLD}): {e}")
            if consecutive_search_failures >= SEARCH_FAILURE_THRESHOLD:
                print(f"  ABORT reverse sync — {SEARCH_FAILURE_THRESHOLD} consecutive failures")
                break
            continue
```

为了让 sync.py 的 Step 7 能感知 4xx/5xx 错误，需要让 `_request` 对 HTTP 错误重新抛出，而其他错误（网络、JSON 解析等）继续返回 `{}` 保持向后兼容。改动 `scripts/netease_api.py:66` 的 `_request`：

```python
def _request(self, path, method="GET", **params):
    try:
        url = f"{self.base_url}{path}"
        if method.upper() == "POST":
            response = self._session.post(url, data=params, timeout=self._timeout)
        else:
            response = self._session.get(url, params=params, timeout=self._timeout)
        response.raise_for_status()
        self._rate_limit()
        data = response.json()
        return data.get("data", data)
    except requests.HTTPError as e:
        print(f"HTTP error: {e}")
        raise  # 让 caller 决定如何处理
    except Exception as e:
        print(f"Request failed: {e}")
        return {}
```

调用方处理策略：

- `search`：不再用 try 包裹 `_request`，让 `HTTPError` 透传到 sync.py Step 7
- `add_to_liked`、`remove_from_liked`、`get_lyric`、`get_liked_track_ids`、`get_track_details_batch`：包一层 `try/except requests.HTTPError: return ...` 兜底（旧行为不变）

sync.py Step 7 里捕获 `requests.HTTPError` 触发 Fix 2 的计数和 Fix 4 的 backoff。

**Step 0 与 Step 4 中的 search 调用怎么办**：

- Step 4 的 QQ search（`qq_api.search(...)` line 313）走的是 `qqmusic_api_v2.py`，与本次修复无关
- Step 7 的 NetEase search 是本次修复的对象

**state 保护**：跳出 Step 7 后，`state` 仍需要写回（包含 lyrics_cache 等本次新拿到的内容），所以 break 之后照常执行 Step 8 和最终的 `save_state`。Step 8 不依赖 Step 7 完整跑完，安全。

## Fix 3：DRY_RUN summary 更显眼

**文件**: `scripts/sync.py:693`

**变更**：summary 区分 dry-run 和真实执行：

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
    forward_line = f"**Forward (Ne→QQ):** {executed_l1 + executed_l2} executed ..."
    reverse_line = f"**Reverse (QQ→Ne):** {rev_executed} executed / {rev_skipped} already liked / ..."
```

让看 summary 的人立刻分清"什么都没动"和"该执行的都执行了"。

## Fix 4：反向同步指数退避（保守版）

**文件**: `scripts/sync.py` Step 7

**当前**：起步 sleep 30s，每次搜索前 sleep 5s。

**保留**：起步 30s，每次搜索前 5s（用户决定保守）。

**新增**：当 `_request` 抛 HTTPError（非网络错误，是 4xx/5xx），sleep 后再 retry 当前 track 一次。退避：30s → 60s → 120s。第三次仍失败累计到 Fix 2 的失败计数。

伪码：

```python
BACKOFF_SECONDS = [30, 60, 120]
backoff_idx = 0
consecutive_search_failures = 0
abort = False

for qq_key, qq_row in qq_only:
    if abort:
        break
    ...
    if not ne_match:
        time.sleep(5)
        results = []
        for attempt in range(2):  # 原始尝试 + 1 次重试
            try:
                results = ne_api.search(...)
                consecutive_search_failures = 0
                backoff_idx = 0
                break
            except requests.HTTPError as e:
                consecutive_search_failures += 1
                if consecutive_search_failures >= SEARCH_FAILURE_THRESHOLD:
                    print(f"  ABORT reverse sync — {SEARCH_FAILURE_THRESHOLD} consecutive failures")
                    abort = True
                    break
                wait = BACKOFF_SECONDS[min(backoff_idx, len(BACKOFF_SECONDS) - 1)]
                backoff_idx += 1
                status = e.response.status_code if e.response is not None else "?"
                print(f"  HTTP {status} — waiting {wait}s before retry")
                time.sleep(wait)
        if abort:
            break
        ...
```

实际实现可以提个 helper 函数 `search_with_backoff(api, query, state)` 让循环体清爽，state 里携带 `consecutive_search_failures` 和 `backoff_idx`。

## Fix 5（顺带）：tests 加一条 POST 验证

**文件**: `tests/test_netease_api_http.py`

新增：

```python
def test_search_with_post():
    """Search must work with POST after login (the fix for Fix 1)."""
    response = requests.post(
        f"{BASE_URL}/search",
        data={"keywords": "周杰伦 晴天", "type": "1", "limit": "1"},
        timeout=10,
    )
    assert response.status_code == 200
```

仅当 NetEase 服务跑起来时手动跑：`python tests/test_netease_api_http.py`。不接 CI（CI 不需要——CI 里 sync.py 跑通就证明了）。

## 测试计划

按顺序执行，每步通过才进下一步：

1. **单元级**：`tests/test_netease_api_http.py` 新增的 `test_search_with_post` 通过
2. **本地 dry-run**：`bash scripts/start_netease_api.sh` 后 `python scripts/sync.py`
   - 期望：Reverse 阶段不再 0 matched，有具体数字（例如 ~100/712）
   - 期望：summary 行显示 `[DRY-RUN] would add ...`
3. **本地小批量真跑**：`$env:DRY_RUN="false"; $env:REVERSE_BATCH="5"; python scripts/sync.py`
   - 期望：5 首中有 ≥ 1 首在网易云成功 add（验证 NetEase add API 仍可用）
   - 期望：CSV 中对应的 `qq_only` 行被 `promote_qq_row` 升级为带 `netease_id`
4. **故意触发 4xx**（可选）：临时把 `ne_api.search` 第一行改成 raise HTTPError，验证 backoff + abort 工作

## 风险

| 风险 | 缓解 |
|---|---|
| POST search 仍 405 | 直接读 NeteaseCloudMusicApiEnhanced 源码确认。Fallback：QueryString + POST body 同时发送 |
| Backoff 30s/60s/120s 实际仍被封 | 保留 `REVERSE_BATCH=25`，单次最多 25×5 + 30+60+120 = ~335s 后必停 |
| `_request` 修改破坏其他 caller | 只对 4xx/5xx raise，其他场景保持返回 `{}`，向后兼容 |
| Summary 输出格式变化破坏外部解析 | 没有外部消费者；GitHub Actions step summary 只人看 |

## 不做的取舍

- **不**调整 `REVERSE_BATCH`：依赖时间扩散来"治"封禁，激进无收益
- **不**改 DRY_RUN 默认值：用户安全网
- **不**改匹配阈值：本次目标是修流水线，不是调参

## 实施顺序

按可独立验证的最小提交划分：

1. Fix 1（一行改动）+ Fix 5（test）
2. 本地 dry-run 验证 reverse 不再 405
3. Fix 4（backoff helper）
4. Fix 2（连续失败 abort）
5. Fix 3（summary）
6. 本地小批量 `REVERSE_BATCH=5` 真跑
7. 全量 commit

每个 commit 独立可回滚。
