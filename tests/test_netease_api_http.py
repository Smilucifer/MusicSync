"""Test NeteaseCloudMusicApi HTTP API availability."""
import requests
import time
import subprocess
import os

BASE_URL = "http://localhost:3000"

def test_server_startup():
    """Test that the server starts and responds."""
    # Start server in background
    # This will be implemented in Task 3
    pass

def test_login_status():
    """Test /login/status endpoint."""
    response = requests.get(f"{BASE_URL}/login/status")
    assert response.status_code == 200
    data = response.json()
    assert "code" in data

def test_search():
    """Test /search endpoint."""
    response = requests.get(f"{BASE_URL}/search", params={"keywords": "周杰伦", "limit": 5})
    assert response.status_code == 200
    data = response.json()
    assert data.get("code") == 200
    assert "result" in data
    assert "songs" in data["result"]

def test_song_detail():
    """Test /song/detail endpoint."""
    # First search for a song to get an ID
    search_response = requests.get(f"{BASE_URL}/search", params={"keywords": "周杰伦", "limit": 1})
    search_data = search_response.json()
    if search_data.get("code") == 200 and search_data["result"]["songs"]:
        song_id = search_data["result"]["songs"][0]["id"]

        # Then get song detail
        response = requests.get(f"{BASE_URL}/song/detail", params={"ids": str(song_id)})
        assert response.status_code == 200
        data = response.json()
        assert data.get("code") == 200
        assert "songs" in data

if __name__ == "__main__":
    # Run tests manually
    print("Testing NeteaseCloudMusicApi HTTP API...")
    try:
        test_login_status()
        print("OK /login/status endpoint works")

        test_search()
        print("OK /search endpoint works")

        test_song_detail()
        print("OK /song/detail endpoint works")

        print("\nAll tests passed!")
    except Exception as e:
        print(f"FAIL Test failed: {e}")
