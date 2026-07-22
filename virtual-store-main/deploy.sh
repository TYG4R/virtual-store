#!/data/data/com.termux/files/usr/bin/bash
# One-tap deploy: sets up the store the first time, then just runs it.
set -e
cd "$(dirname "$0")"

termux-wake-lock 2>/dev/null || true   # stop Android from killing Termux mid-session

# ---------- First-run setup ----------
if [ ! -d venv ]; then
  echo "==> First run: creating Python environment"
  python -m venv venv --system-site-packages
  source venv/bin/activate
  pip install --upgrade pip
  grep -vi '^pillow' requirements.txt > /tmp/req.txt   # use Termux's pillow, not a pip build
  pip install -r /tmp/req.txt
else
  source venv/bin/activate
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from the template — opening it now to fill in."
  echo "    (Set SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, and Razorpay keys.)"
  nano .env
fi

# ---------- Run ----------
echo "==> Starting the store on http://localhost:5000"
pkill -f "python app.py" 2>/dev/null || true
nohup python app.py > server.log 2>&1 &
sleep 2

echo "==> Opening a public link via Cloudflare Tunnel (no signup needed)"
echo "==> Your live URL will appear below in a few seconds — share that link."
cloudflared tunnel --url http://localhost:5000
