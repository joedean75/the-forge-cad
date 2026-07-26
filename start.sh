#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Start backend in background
cd backend
source .venv/bin/activate
# --no-proxy-headers: don't let a client's X-Forwarded-For become the peer
# address, which would defeat the auth rate limiter (see app/rate_limit.py).
uvicorn main:app --host 0.0.0.0 --port 8000 --no-proxy-headers &
BACKEND_PID=$!
cd ..

# Start frontend
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

echo "The Forge started:"
echo "  Frontend: http://localhost:5173"
echo "  Backend:  http://localhost:8000"
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID" INT
wait
