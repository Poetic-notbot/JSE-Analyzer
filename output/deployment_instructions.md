# Deployment Instructions — Jamaica Crop Intelligence

## What is deployable

- **Entry file:** `streamlit_app.py` (repository root)
- **Runtime deps:** `requirements.txt` (repository root — covers both apps)
- **Python:** 3.11 (works on 3.10–3.12)

The app is fully self-contained: SQLite is created and seeded automatically on
first run, so there is no external database or secret to configure.

## Local run

```bash
git clone https://github.com/Poetic-notbot/JSE-Analyzer
cd JSE-Analyzer
pip install -r requirements.txt
streamlit run streamlit_app.py
# open http://localhost:8501
```

Run the tests:

```bash
pytest jamaica_crop_intelligence/tests -q     # 23 tests
```

## Deploy to Streamlit Community Cloud (one-time, ~2 minutes)

Streamlit Community Cloud deployment is tied to *your* Streamlit account signed
in with your GitHub identity, so it must be done from your login. Steps:

1. Go to **https://share.streamlit.io** and sign in with the **Poetic-notbot**
   GitHub account.
2. Click **Create app → Deploy a public app from GitHub**.
3. Fill in:
   - **Repository:** `Poetic-notbot/JSE-Analyzer`
   - **Branch:** `claude/jamaica-crop-intelligence-yqr45e` (or `main` after you
     merge the PR)
   - **Main file path:** `streamlit_app.py`
4. Click **Deploy**. First build installs `requirements.txt` (~1–2 min).
5. Streamlit assigns a public URL, typically:
   `https://<app-name>-poetic-notbot.streamlit.app`
   Rename it under **App settings → General → App URL** if you want a cleaner
   slug such as `jamaica-crop-intelligence`.

### Notes

- **Ephemeral storage:** Community Cloud resets the filesystem on each cold
  start, so ingested official data is not retained between restarts. The app
  re-seeds automatically. To persist ingested data, set an env var
  `JCI_DB_PATH` pointing at a mounted persistent volume (not available on the
  free Community tier — use a small managed host or Streamlit's connections for
  that).
- **pdfplumber:** included for Ministry PDF ingestion. It is imported lazily, so
  the app still runs if the wheel is unavailable on a given platform.
- **Legacy app:** the original JSE stock analyzer remains at `app.py`. To deploy
  it instead/as a second app, set the main file path to `app.py`.

## Deploy elsewhere (Docker sketch)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", \
     "--server.port=8501", "--server.address=0.0.0.0"]
```

```bash
docker build -t jamaica-crop-intelligence .
docker run -p 8501:8501 jamaica-crop-intelligence
```
