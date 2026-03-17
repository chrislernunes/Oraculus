# ORACULUS — Prediction Market Arbitrage Engine

## Deploy on Render (one service, no Redis)

### 1. Upload to GitHub
Push all files to a GitHub repo.

### 2. Create Web Service on Render
- render.com → New → **Web Service**
- Connect your GitHub repo
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
- Click **Deploy**

That's it. The engine runs as a background thread inside the same process.

### 3. Visit your URL
`https://your-service-name.onrender.com`

---

## Run Locally
```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```
