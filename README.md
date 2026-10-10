# ⚡ Ultra-Fast M3U8 Downloader (with GitHub Actions Cloud Engine)

An ultra-high-speed HLS (M3U8) stream downloader optimized for extreme performance both locally and in **GitHub Actions cloud runners**.

---

## 🚀 Speed Optimizations Added

1. **Persistent HTTP/2 & TCP Connection Reuse**:
   - Reuses worker sessions with HTTP keep-alive instead of establishing a fresh TLS handshake per segment.
   - Eliminates 50ms–200ms latency on every segment (saving minutes per episode).
2. **Pre-Cached AES-128 Decryption**:
   - Decryption keys are pre-fetched and cached thread-safely before segment workers start, removing key contention.
3. **Elevated Cloud Concurrency**:
   - Default concurrency boosted to **32 threads** (configurable up to **64 threads**).
4. **Linux Network Kernel Buffer Tuning**:
   - Automated TCP socket buffer boost (`net.core.rmem_max`, `net.ipv4.tcp_rmem`) and file descriptor boost (`ulimit -n 65535`) inside the GitHub Actions runner.
5. **Headless & CI Auto-Detection**:
   - Progress reporting automatically adjusts to prevent log flooding in CI.
   - Automatically executes batch tasks from `input_data.txt` without hanging on interactive prompts.
6. **Guaranteed Temp Cleanup & Resume Support**:
   - Leftover segment caches are cleaned up automatically.
   - Already-downloaded segments are recognized instantly without re-fetching.

---

## ☁️ Running on GitHub Actions (Zero Local Bandwidth)

### Method 1: Manual Run via the GitHub UI (Workflow Dispatch)
1. Push this repository to GitHub.
2. In your repo, click **Actions** -> **Ultra M3U8 Downloader**.
3. Click **Run workflow**.
4. You can optionally fill in:
   - **MAL ID**: e.g., `1735` (leave blank to run tasks directly from `input_data.txt`)
   - **Episodes**: e.g., `250 to 255`, `25`, or `all`
   - **Direct M3U8 URL**: (optional)
   - **Threads**: `32` (or `48` / `64` for maximum cloud speed)
   - **Upload Options**: Artifacts and/or GitHub Releases
5. Click **Run workflow**.

### Method 2: Automatic Trigger on Git Push
- Simply edit `input_data.txt` with your desired anime MAL ID and episode ranges (e.g., `mal id = 1735 ep = 1 to 10`), commit, and push.
- The workflow will automatically start downloading immediately!

---

## 📦 How to Get Downloaded Videos from GitHub Actions

Once the workflow finishes, you have two convenient download options:

1. **GitHub Releases** (Direct, permanent download links):
   - Navigate to the **Releases** tab in your repository.
   - Each completed run attaches all `.mp4` video files directly to a release.
   - You can download them directly in your browser or with download managers (IDM, aria2, wget) at full gigabit speed.
2. **Workflow Artifacts**:
   - Click the workflow run in the **Actions** tab.
   - Scroll to the bottom to the **Artifacts** section and download `downloaded-videos-<run_number>.zip`.

---

## 💻 Running Locally

### Install Dependencies
```bash
pip install -r requirements.txt
```

### CLI Commands
```bash
# Process input_data.txt batch tasks
python "m3u8 downloader.py" --file input_data.txt -t 32

# Download specific MAL ID and episodes
python "m3u8 downloader.py" --mal 1735 --ep "250 to 255" -t 32

# Download direct M3U8 URL
python "m3u8 downloader.py" -u "https://example.com/playlist.m3u8" -o "video.mp4" -t 32
```
