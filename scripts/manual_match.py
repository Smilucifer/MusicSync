"""Manual matching web UI — paste a URL to link one-sided songs."""
import asyncio
import os
import re
import sys
from pathlib import Path

from flask import Flask, jsonify, request, render_template_string

# Ensure scripts/ parent is on sys.path so we can import sibling modules
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import connect, get_link, upsert_platform_link, update_match_source
from matcher import check_link_conflict, clean_name, clean_artist
from merge import propose_merge, execute_merge

app = Flask(__name__)

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_QQ_DIRECT_RE = re.compile(r"y\.qq\.com/(?:.*?/)songDetail/([a-zA-Z0-9]+)")
_NE_RE = re.compile(r"(?:music|y\.music)\.163\.com/.*?[?&]id=(\d+)")
_QQ_SHARE_RE = re.compile(r"(?:c\d+\.y\.qq\.com|y\.qq\.com)/.*\b__=\w+")


def parse_url(url: str) -> tuple[str, str, str | None]:
    """Return (platform, track_id, error).  error is None on success."""
    url = url.strip()
    m = _NE_RE.search(url)
    if m:
        return "netease", m.group(1), None
    m = _QQ_DIRECT_RE.search(url)
    if m:
        return "qq", m.group(1), None
    if _QQ_SHARE_RE.search(url):
        return "", "", "QQ 分享链接暂不支持，请在 QQ 音乐中打开歌曲后复制地址栏链接（形如 y.qq.com/.../songDetail/xxx）"
    return "", "", "无法识别链接格式，请粘贴网易云或 QQ 音乐的歌曲页面链接"


# ---------------------------------------------------------------------------
# Track info fetchers
# ---------------------------------------------------------------------------

def _fetch_ne(track_id: str) -> dict | None:
    from netease_api import NetEaseAPI
    api = NetEaseAPI()
    tracks = api.get_track_details_batch([int(track_id)])
    if not tracks:
        return None
    t = tracks[0]
    return {
        "platform": "netease",
        "track_id": str(t["id"]),
        "name": t.get("name", ""),
        "artist": t.get("artist", ""),
        "album": t.get("album", ""),
        "duration": t.get("duration", 0),
    }


def _fetch_qq(mid: str) -> dict | None:
    from qqmusic_api import Client, Credential
    musicid = int(os.getenv("QQMUSIC_UIN", "0"))
    musickey = os.getenv("QQMUSIC_KEY", "")
    if not musickey or not musicid:
        return None

    async def _do():
        async with Client(credential=Credential(musicid=musicid, musickey=musickey)) as client:
            result = await client.song.query_song([mid])
            tracks = result.tracks if hasattr(result, "tracks") else result
            if not tracks:
                return None
            song = tracks[0]
            singer_name = song.singer[0].name if song.singer else ""
            return {
                "platform": "qq",
                "track_id": song.mid if hasattr(song, "mid") else mid,
                "name": song.name if hasattr(song, "name") else "",
                "artist": singer_name,
                "album": song.album.name if hasattr(song, "album") and song.album else "",
                "duration": song.interval if hasattr(song, "interval") else 0,
            }

    try:
        return asyncio.run(_do())
    except Exception as e:
        print(f"[QQ fetch error] mid={mid}: {e}")
        return None


def fetch_track_info(platform: str, track_id: str) -> dict | None:
    if platform == "netease":
        return _fetch_ne(track_id)
    if platform == "qq":
        return _fetch_qq(track_id)
    return None


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def _get_stats(conn) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM songs WHERE deleted_at IS NULL").fetchone()[0]
    ne_only = conn.execute("""
        SELECT COUNT(*) FROM songs s WHERE s.deleted_at IS NULL AND
        EXISTS (SELECT 1 FROM platform_links WHERE song_id=s.id AND platform='netease' AND liked=1)
        AND NOT EXISTS (SELECT 1 FROM platform_links WHERE song_id=s.id AND platform='qq' AND liked=1)
    """).fetchone()[0]
    qq_only = conn.execute("""
        SELECT COUNT(*) FROM songs s WHERE s.deleted_at IS NULL AND
        EXISTS (SELECT 1 FROM platform_links WHERE song_id=s.id AND platform='qq' AND liked=1)
        AND NOT EXISTS (SELECT 1 FROM platform_links WHERE song_id=s.id AND platform='netease' AND liked=1)
    """).fetchone()[0]
    matched = total - ne_only - qq_only
    return {"total": total, "netease_only": ne_only, "qq_only": qq_only, "matched": matched}


def _get_all_songs(conn) -> list[dict]:
    """Return all songs with their NE and QQ link info."""
    rows = conn.execute("""
        SELECT s.id, s.name, s.artist, s.album, s.match_source,
               ne.platform_track_id AS ne_tid, ne.platform_name AS ne_name,
               ne.platform_artist AS ne_artist, ne.liked AS ne_liked,
               qq.platform_track_id AS qq_tid, qq.platform_name AS qq_name,
               qq.platform_artist AS qq_artist, qq.liked AS qq_liked
        FROM songs s
        LEFT JOIN platform_links ne ON ne.song_id = s.id AND ne.platform = 'netease'
        LEFT JOIN platform_links qq ON qq.song_id = s.id AND qq.platform = 'qq'
        WHERE s.deleted_at IS NULL
        ORDER BY s.id
    """).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/api/health")
def health():
    ne_ok = False
    try:
        import requests
        r = requests.get("http://localhost:3000/inner/version", timeout=3)
        ne_ok = r.status_code == 200
    except Exception:
        pass
    db_ok = False
    try:
        with connect() as conn:
            conn.execute("SELECT 1 FROM songs LIMIT 1")
            db_ok = True
    except Exception:
        pass
    return jsonify({"netease_api": ne_ok, "database": db_ok})


@app.route("/api/stats")
def stats():
    with connect() as conn:
        return jsonify(_get_stats(conn))


@app.route("/api/songs")
def songs():
    with connect() as conn:
        return jsonify(_get_all_songs(conn))


@app.route("/api/resolve", methods=["POST"])
def resolve():
    data = request.get_json(force=True)
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "请提供链接"}), 400

    platform, track_id, err = parse_url(url)
    if err:
        return jsonify({"error": err}), 400

    track = fetch_track_info(platform, track_id)
    if not track:
        return jsonify({"error": f"无法获取{platform}歌曲信息（track_id={track_id}）"}), 404

    # Check if this track is already in our DB
    with connect() as conn:
        existing = get_link(conn, platform, track_id)
        already_in_db = existing is not None
        existing_song_id = existing["song_id"] if existing else None

    return jsonify({
        "platform": platform,
        "track_id": track_id,
        "name": track["name"],
        "artist": track["artist"],
        "album": track.get("album", ""),
        "duration": track.get("duration", 0),
        "already_in_db": already_in_db,
        "existing_song_id": existing_song_id,
    })


@app.route("/api/match", methods=["POST"])
def match():
    data = request.get_json(force=True)
    song_id = data.get("song_id")
    platform = data.get("platform")
    track_id = data.get("track_id")
    name = data.get("name", "")
    artist = data.get("artist", "")
    album = data.get("album", "")

    if not all([song_id, platform, track_id]):
        return jsonify({"error": "缺少必要参数"}), 400

    with connect() as conn:
        # Conflict check
        conflict = check_link_conflict(
            conn, platform=platform, platform_track_id=str(track_id),
            current_song_id=int(song_id),
        )
        if conflict is not None:
            # Return merge proposal instead of 409
            preview = propose_merge(
                conn,
                source_song_id=conflict,
                target_song_id=int(song_id),
            )
            return jsonify({
                "action": "merge_proposal",
                "source_song_id": conflict,
                "target_song_id": int(song_id),
                "preview": preview,
            })

        upsert_platform_link(
            conn,
            song_id=int(song_id),
            platform=platform,
            platform_track_id=str(track_id),
            platform_name=name,
            platform_artist=artist,
            platform_album=album,
            liked=1,
        )

        # Only upgrade match_source for low-confidence entries
        song = conn.execute("SELECT match_source FROM songs WHERE id=?", (int(song_id),)).fetchone()
        if song and song["match_source"] in ("unmatched", "l3_name_artist"):
            update_match_source(conn, int(song_id), "manual")

    return jsonify({"ok": True})


@app.route("/api/unmatch", methods=["POST"])
def unmatch():
    data = request.get_json(force=True)
    song_id = data.get("song_id")
    platform = data.get("platform")
    if not all([song_id, platform]):
        return jsonify({"error": "缺少必要参数"}), 400

    with connect() as conn:
        conn.execute(
            "DELETE FROM platform_links WHERE song_id=? AND platform=?",
            (int(song_id), platform),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/merge", methods=["POST"])
def merge_songs():
    data = request.get_json(force=True)
    source_song_id = data.get("source_song_id")
    target_song_id = data.get("target_song_id")
    name = data.get("name", "")
    artist = data.get("artist", "")
    album = data.get("album")

    if not all([source_song_id, target_song_id, name, artist]):
        return jsonify({"error": "缺少必要参数"}), 400

    with connect() as conn:
        try:
            execute_merge(
                conn,
                source_song_id=int(source_song_id),
                target_song_id=int(target_song_id),
                name=name,
                artist=artist,
                album=album,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    return jsonify({"ok": True, "target_song_id": int(target_song_id)})


# ---------------------------------------------------------------------------
# HTML (inline single page)
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MusicSync 手动匹配</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f5f5;color:#333;padding:20px}
.header{background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.stats{display:flex;gap:24px;font-size:15px}
.stats .num{font-weight:700;font-size:20px}
.health{margin-top:8px;font-size:13px;color:#888}
.health .ok{color:#22c55e} .health .fail{color:#ef4444}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{padding:8px 16px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;font-size:14px}
.tab.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.tab:hover:not(.active){background:#f0f0f0}
.controls{display:flex;gap:12px;margin-bottom:16px;align-items:center}
.controls input{flex:1;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px}
.controls button{padding:8px 16px;border:none;border-radius:6px;background:#3b82f6;color:#fff;font-size:14px;cursor:pointer}
.controls button:hover{background:#2563eb}
.controls button:disabled{background:#94a3b8;cursor:not-allowed}
table{width:100%;background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eee;font-size:14px}
th{background:#f9fafb;font-weight:600;position:sticky;top:0}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;color:#fff}
.badge-ne{background:#e74c3c} .badge-qq{background:#3b82f6}
.btn{padding:4px 10px;border:none;border-radius:4px;cursor:pointer;font-size:13px}
.btn-match{background:#22c55e;color:#fff} .btn-match:hover{background:#16a34a}
.btn-unmatch{background:#f59e0b;color:#fff;margin-left:4px} .btn-unmatch:hover{background:#d97706}
.btn-cancel{background:#94a3b8;color:#fff;margin-left:4px}
.btn-confirm{background:#22c55e;color:#fff;margin-left:8px;padding:6px 14px}
.expand-row{background:#f0fdf4}
.expand-row td{padding:12px 16px}
.resolve-box{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.resolve-box input{flex:1;padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:13px}
.resolve-box button{padding:6px 12px;border:none;border-radius:4px;background:#3b82f6;color:#fff;font-size:13px;cursor:pointer}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px;padding:12px;background:#f9fafb;border-radius:6px;font-size:13px}
.compare .side h4{margin-bottom:4px;color:#555}
.compare .side p{margin:2px 0}
.msg{padding:8px 12px;border-radius:6px;margin-top:8px;font-size:13px}
.msg-ok{background:#dcfce7;color:#166534} .msg-err{background:#fef2f2;color:#991b1b}
.empty{text-align:center;padding:40px;color:#999;font-size:15px}
.row-name{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell-ne{background:#fef2f2} .cell-qq{background:#eff6ff}
.cell-linked{color:#166534;font-weight:500} .cell-missing{color:#999}
.merge-dialog{margin-top:8px;border:2px solid #f97316;border-radius:8px;overflow:hidden}
.merge-header{background:#fff7ed;padding:12px 16px;font-size:14px;font-weight:600;color:#9a3412}
.merge-body{padding:16px;background:#fff}
.merge-sides{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.merge-side{padding:12px;border-radius:6px;font-size:13px}
.merge-side.source{background:#fef2f2;border:1px solid #fecaca}
.merge-side.target{background:#f0fdf4;border:1px solid #bbf7d0}
.merge-side h4{margin-bottom:6px;color:#555}
.merge-side p{margin:2px 0}
.merge-fields{margin-bottom:12px}
.merge-field{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:13px}
.merge-field label{width:50px;font-weight:600;color:#555}
.merge-field input{flex:1;padding:6px 10px;border:1px solid #ddd;border-radius:4px;font-size:13px}
.merge-warnings{background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:10px 12px;margin-bottom:12px;font-size:13px;color:#991b1b}
.merge-warnings li{margin:4px 0}
.merge-actions{display:flex;gap:8px}
.btn-merge-confirm{background:#dc2626;color:#fff;border:none;padding:8px 16px;border-radius:6px;font-size:14px;cursor:pointer;font-weight:600}
.btn-merge-confirm:hover{background:#b91c1c}
</style>
</head>
<body>
<div class="header">
  <div class="stats">
    <span>总计 <span class="num" id="s-total">-</span></span>
    <span>已匹配 <span class="num" id="s-matched">-</span></span>
    <span>仅网易云 <span class="num" id="s-ne">-</span></span>
    <span>仅QQ <span class="num" id="s-qq">-</span></span>
  </div>
  <div class="health" id="health"></div>
</div>
<div class="tabs">
  <button class="tab active" data-filter="all">全部</button>
  <button class="tab" data-filter="ne_only">仅网易云</button>
  <button class="tab" data-filter="qq_only">仅QQ</button>
  <button class="tab" data-filter="matched">已匹配</button>
</div>
<div class="controls">
  <input type="text" id="url-input" placeholder="粘贴网易云或 QQ 音乐歌曲链接，按回车或点击解析">
  <button id="resolve-btn" onclick="resolveURL()">解析</button>
</div>
<div id="resolve-result"></div>
<table>
  <thead><tr><th>ID</th><th>歌曲</th><th>歌手</th><th>网易云</th><th>QQ音乐</th><th>操作</th></tr></thead>
  <tbody id="song-list"><tr><td colspan="6" class="empty">加载中…</td></tr></tbody>
</table>

<script>
let allSongs = [];
let currentFilter = 'all';
let expandId = null;
let expandPlatform = null;

async function loadHealth() {
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    const el = document.getElementById('health');
    el.innerHTML = `网易云API: <span class="${d.netease_api?'ok':'fail'}">${d.netease_api?'✓ 运行中':'✗ 未运行'}</span> · 数据库: <span class="${d.database?'ok':'fail'}">${d.database?'✓ 可用':'✗ 不可用'}</span>`;
  } catch(e) {}
}

async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('s-total').textContent = d.total;
    document.getElementById('s-matched').textContent = d.matched;
    document.getElementById('s-ne').textContent = d.netease_only;
    document.getElementById('s-qq').textContent = d.qq_only;
  } catch(e) {}
}

async function loadSongs() {
  try {
    const r = await fetch('/api/songs');
    allSongs = await r.json();
    renderList();
  } catch(e) {
    document.getElementById('song-list').innerHTML = '<tr><td colspan="6" class="empty">加载失败</td></tr>';
  }
}

function getFilteredSongs() {
  return allSongs.filter(s => {
    const hasNe = s.ne_tid && s.ne_liked;
    const hasQq = s.qq_tid && s.qq_liked;
    if (currentFilter === 'ne_only') return hasNe && !hasQq;
    if (currentFilter === 'qq_only') return hasQq && !hasNe;
    if (currentFilter === 'matched') return hasNe && hasQq;
    return true;
  });
}

function renderList() {
  const tbody = document.getElementById('song-list');
  const songs = getFilteredSongs();
  if (!songs.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">没有歌曲</td></tr>';
    return;
  }
  let html = '';
  for (const s of songs) {
    const hasNe = s.ne_tid && s.ne_liked;
    const hasQq = s.qq_tid && s.qq_liked;
    const neCell = hasNe
      ? `<span class="cell-linked" title="${esc(s.ne_name||'')} · ${esc(s.ne_artist||'')}">${esc(s.ne_name||s.ne_tid)}</span>
         <button class="btn btn-unmatch" onclick="doUnmatch(${s.id},'netease')">撤销</button>`
      : `<span class="cell-missing">—</span>
         <button class="btn btn-match" onclick="startMatch(${s.id},'netease')">匹配</button>`;
    const qqCell = hasQq
      ? `<span class="cell-linked" title="${esc(s.qq_name||'')} · ${esc(s.qq_artist||'')}">${esc(s.qq_name||s.qq_tid)}</span>
         <button class="btn btn-unmatch" onclick="doUnmatch(${s.id},'qq')">撤销</button>`
      : `<span class="cell-missing">—</span>
         <button class="btn btn-match" onclick="startMatch(${s.id},'qq')">匹配</button>`;

    html += `<tr>
      <td>${s.id}</td>
      <td class="row-name" title="${esc(s.name)}">${esc(s.name)}</td>
      <td class="row-name" title="${esc(s.artist)}">${esc(s.artist)}</td>
      <td class="cell-ne">${neCell}</td>
      <td class="cell-qq">${qqCell}</td>
      <td></td>
    </tr>`;
    if (expandId === s.id && expandPlatform) {
      const targetName = expandPlatform === 'netease' ? '网易云' : 'QQ音乐';
      html += `<tr class="expand-row"><td colspan="6" id="expand-${s.id}">
        <div style="margin-bottom:8px"><b>为 "${esc(s.name)}" 匹配${targetName}歌曲</b></div>
        <div class="resolve-box">
          <input type="text" id="expand-url-${s.id}" placeholder="粘贴${targetName}歌曲链接">
          <button onclick="resolveForSong(${s.id},'${expandPlatform}')">解析</button>
          <button class="btn btn-cancel" onclick="cancelExpand()">取消</button>
        </div>
        <div id="expand-result-${s.id}"></div>
      </td></tr>`;
    }
  }
  tbody.innerHTML = html;
}

function startMatch(songId, platform) {
  expandId = songId;
  expandPlatform = platform;
  renderList();
  setTimeout(() => {
    const inp = document.getElementById('expand-url-'+songId);
    if (inp) inp.focus();
  }, 50);
}

function cancelExpand() {
  expandId = null;
  expandPlatform = null;
  renderList();
}

async function resolveForSong(songId, targetPlatform) {
  const inp = document.getElementById('expand-url-'+songId);
  const url = inp.value.trim();
  if (!url) return;
  const resDiv = document.getElementById('expand-result-'+songId);
  resDiv.innerHTML = '<div class="msg">解析中…</div>';

  try {
    const r = await fetch('/api/resolve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
    const d = await r.json();
    if (d.error) {
      resDiv.innerHTML = `<div class="msg msg-err">${esc(d.error)}</div>`;
      return;
    }
    if (d.platform !== targetPlatform) {
      resDiv.innerHTML = `<div class="msg msg-err">需要${targetPlatform==='netease'?'网易云':'QQ音乐'}的链接，但你提供的是${d.platform==='netease'?'网易云':'QQ音乐'}</div>`;
      return;
    }
    const song = allSongs.find(s => s.id === songId);
    const durStr = d.duration > 0 ? `${Math.floor(d.duration/60)}:${String(d.duration%60).padStart(2,'0')}` : '';
    window._resolved = {songId, platform:d.platform, trackId:d.track_id, name:d.name, artist:d.artist, album:d.album||''};
    resDiv.innerHTML = `
      <div class="compare">
        <div class="side"><h4>现有歌曲</h4><p><b>${esc(song.name)}</b></p><p>${esc(song.artist)}</p><p>${esc(song.album||'')}</p></div>
        <div class="side"><h4>匹配目标 (${d.platform==='netease'?'网易云':'QQ'})</h4><p><b>${esc(d.name)}</b></p><p>${esc(d.artist)}</p><p>${esc(d.album||'')} ${durStr}</p></div>
      </div>
      ${d.already_in_db ? '<div class="msg msg-err" style="margin-top:8px">⚠ 该歌曲已关联到 song_id='+d.existing_song_id+'，匹配将重新关联到当前歌曲</div>' : ''}
      <button class="btn btn-confirm" onclick="doMatch()">确认匹配</button>
    `;
  } catch(e) {
    resDiv.innerHTML = `<div class="msg msg-err">请求失败: ${esc(e.message)}</div>`;
  }
}

async function doMatch() {
  const d = window._resolved;
  if (!d) return;
  try {
    const r = await fetch('/api/match', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({song_id:d.songId, platform:d.platform, track_id:d.trackId, name:d.name, artist:d.artist, album:d.album})});
    const resp = await r.json();
    if (resp.action === 'merge_proposal') {
      renderMergeDialog(d.songId, resp);
      return;
    }
    if (resp.error) {
      alert(resp.error);
      return;
    }
    expandId = null;
    expandPlatform = null;
    window._resolved = null;
    await loadSongs();
    await loadStats();
  } catch(e) {
    alert('匹配失败: ' + e.message);
  }
}

function renderMergeDialog(songId, resp) {
  const d = window._resolved;
  const p = resp.preview;
  const resDiv = document.getElementById('expand-result-' + songId);
  if (!resDiv || !d) return;

  const srcPlats = p.source.plats.map(pl => pl === 'netease' ? '网易云' : 'QQ').join(', ') || '无';
  const tgtPlats = p.target.plats.map(pl => pl === 'netease' ? '网易云' : 'QQ').join(', ') || '无';

  let warningsHtml = '';
  if (p.warnings && p.warnings.length > 0) {
    warningsHtml = '<div class="merge-warnings"><ul>' +
      p.warnings.map(w => '<li>' + esc(w) + '</li>').join('') +
      '</ul></div>';
  }

  resDiv.innerHTML = `
    <div class="merge-dialog">
      <div class="merge-header">检测到这首 ${esc(d.platform === 'netease' ? '网易云' : 'QQ')} 歌曲已属于另一首歌 (song #${resp.source_song_id})。要合并这两首吗?</div>
      <div class="merge-body">
        <div class="merge-sides">
          <div class="merge-side source">
            <h4>来源 (将被合并)</h4>
            <p><b>${esc(p.source.name)}</b></p>
            <p>${esc(p.source.artist)}</p>
            <p>${esc(p.source.album || '')}</p>
            <p style="color:#888;margin-top:4px">平台: ${srcPlats}</p>
          </div>
          <div class="merge-side target">
            <h4>目标 (保留)</h4>
            <p><b>${esc(p.target.name)}</b></p>
            <p>${esc(p.target.artist)}</p>
            <p>${esc(p.target.album || '')}</p>
            <p style="color:#888;margin-top:4px">平台: ${tgtPlats}</p>
          </div>
        </div>
        ${warningsHtml}
        <div class="merge-fields">
          <div class="merge-field"><label>歌名</label><input id="merge-name" value="${esc(p.recommended.name)}"></div>
          <div class="merge-field"><label>歌手</label><input id="merge-artist" value="${esc(p.recommended.artist)}"></div>
          <div class="merge-field"><label>专辑</label><input id="merge-album" value="${esc(p.recommended.album || '')}"></div>
        </div>
        <div class="merge-actions">
          <button class="btn-merge-confirm" onclick="doMerge(${resp.source_song_id}, ${resp.target_song_id})">确认合并</button>
          <button class="btn btn-cancel" onclick="cancelExpand()">取消</button>
        </div>
      </div>
    </div>
  `;
}

async function doMerge(sourceId, targetId) {
  const name = document.getElementById('merge-name').value.trim();
  const artist = document.getElementById('merge-artist').value.trim();
  const album = document.getElementById('merge-album').value.trim();
  if (!name || !artist) {
    alert('歌名和歌手不能为空');
    return;
  }
  try {
    const r = await fetch('/api/merge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        source_song_id: sourceId,
        target_song_id: targetId,
        name: name,
        artist: artist,
        album: album || null,
      })
    });
    const resp = await r.json();
    if (resp.error) {
      alert('合并失败: ' + resp.error);
      return;
    }
    expandId = null;
    expandPlatform = null;
    window._resolved = null;
    await loadSongs();
    await loadStats();
  } catch(e) {
    alert('合并失败: ' + e.message);
  }
}

async function doUnmatch(songId, platform) {
  const platName = platform === 'netease' ? '网易云' : 'QQ音乐';
  if (!confirm(`撤销 song_id=${songId} 的 ${platName} 关联？`)) return;
  try {
    const r = await fetch('/api/unmatch', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({song_id:songId, platform})});
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    await loadSongs();
    await loadStats();
  } catch(e) {
    alert('撤销失败: ' + e.message);
  }
}

async function resolveURL() {
  const inp = document.getElementById('url-input');
  const url = inp.value.trim();
  if (!url) return;
  const resDiv = document.getElementById('resolve-result');
  const btn = document.getElementById('resolve-btn');
  btn.disabled = true;
  resDiv.innerHTML = '<div class="msg">解析中…</div>';

  try {
    const r = await fetch('/api/resolve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
    const d = await r.json();
    if (d.error) {
      resDiv.innerHTML = `<div class="msg msg-err">${esc(d.error)}</div>`;
    } else {
      const badge = d.platform === 'netease' ? '<span class="badge badge-ne">网易云</span>' : '<span class="badge badge-qq">QQ</span>';
      const durStr = d.duration > 0 ? `${Math.floor(d.duration/60)}:${String(d.duration%60).padStart(2,'0')}` : '';
      resDiv.innerHTML = `<div class="msg msg-ok">${badge} <b>${esc(d.name)}</b> — ${esc(d.artist)} ${esc(d.album||'')} ${durStr} (id: ${d.track_id})${d.already_in_db ? ' · ⚠ 已在数据库中 (song_id='+d.existing_song_id+')' : ''}</div>`;
    }
  } catch(e) {
    resDiv.innerHTML = `<div class="msg msg-err">请求失败: ${esc(e.message)}</div>`;
  }
  btn.disabled = false;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// Tab click handlers
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    expandId = null;
    expandPlatform = null;
    renderList();
  });
});

// Enter key on URL input
document.getElementById('url-input').addEventListener('keydown', e => { if(e.key==='Enter') resolveURL(); });

// Init
loadHealth();
loadStats();
loadSongs();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting Manual Match UI at http://localhost:5000")
    print("Make sure the NetEase API service is running on port 3000.")
    app.run(host="127.0.0.1", port=5000, debug=True)
