#!/bin/bash
# Start NeteaseCloudMusicApi service (Enhanced fork)
#
# Uses: https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced
# npm package: @neteasecloudmusicapienhanced/api

set -e

NETEASE_API_DIR="/tmp/netease-api"

# Clone repository if not exists
if [ ! -d "$NETEASE_API_DIR" ]; then
    echo "Cloning NeteaseCloudMusicApiEnhanced..."
    git clone https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced.git "$NETEASE_API_DIR"
fi

# Navigate to directory
cd "$NETEASE_API_DIR"

# Install dependencies
echo "Installing dependencies..."
npm install --no-fund --no-audit

# Start server in background
echo "Starting NeteaseCloudMusicApi server..."
node app.js &
SERVER_PID=$!

# Wait for server to start
echo "Waiting for server to start (PID: $SERVER_PID)..."
for i in $(seq 1 30); do
    if curl -s http://localhost:3000/login/status > /dev/null 2>&1; then
        echo "Server started successfully on http://localhost:3000 (PID: $SERVER_PID)"
        exit 0
    fi
    sleep 2
done

echo "Server failed to start within 60 seconds"
kill $SERVER_PID 2>/dev/null || true
exit 1
