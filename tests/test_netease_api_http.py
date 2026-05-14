"""Test NeteaseCloudMusicApi HTTP API availability."""
import requests
import sys
import os
import io
import traceback

# Fix Windows GBK encoding for Unicode output
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_URL = os.environ.get("NETEASE_API_URL", "http://localhost:3000")

def test_server_startup():
    """Test that the server starts and responds."""
    response = requests.get(f"{BASE_URL}/", timeout=5)
    assert response.status_code in (200, 404)  # server responds at all

def test_login_status():
    """Test /login/status endpoint."""
    response = requests.get(f"{BASE_URL}/login/status", timeout=10)
    assert response.status_code == 200
    data = response.json()
    # Enhanced API wraps login/status response in {"data": {...}}
    login_data = data.get("data", data)
    assert login_data.get("code") == 200

def test_search():
    """Test /search endpoint with POST (required when MUSIC_U cookie is set)."""
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

def test_song_detail():
    """Test /song/detail endpoint."""
    search_response = requests.post(
        f"{BASE_URL}/search",
        data={"keywords": "周杰伦", "limit": 1},
        timeout=10,
    )
    search_data = search_response.json()
    assert search_data.get("code") == 200, f"Search failed: {search_data}"
    assert search_data["result"]["songs"], "Search returned no songs"
    song_id = search_data["result"]["songs"][0]["id"]

    response = requests.get(f"{BASE_URL}/song/detail", params={"ids": str(song_id)}, timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert data.get("code") == 200
    assert "songs" in data

if __name__ == "__main__":
    # Run tests manually
    print("Testing NeteaseCloudMusicApi HTTP API...")
    tests = [
        ("test_server_startup", test_server_startup),
        ("test_login_status", test_login_status),
        ("test_search", test_search),
        ("test_song_detail", test_song_detail),
    ]
    try:
        for name, test_fn in tests:
            test_fn()
            print(f"✓ {name} passed")

        print("\nAll tests passed!")
    except Exception as e:
        traceback.print_exc()
        print(f"\n✗ Test failed: {e}", file=sys.stderr)
        sys.exit(1)
