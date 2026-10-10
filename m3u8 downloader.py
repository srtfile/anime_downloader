"""
=============================================================================
                      M3U8 ULTRA DOWNLOADER
=============================================================================
Advanced HLS (m3u8) video stream downloader featuring:
- Cloudflare & TLS Fingerprint bypass (via curl_cffi Chrome impersonation)
- Multi-threaded concurrent segment downloading with auto-retry
- Full AES-128 HLS decryption support
- Master playlist quality & resolution selector
- Lossless FFmpeg remuxing to MP4 (using system or bundled FFmpeg)
- Interactive CLI + Command-line flag modes
=============================================================================
"""

import os
import sys
import re
import time
import shutil
import argparse
import threading
import atexit
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Tuple, Optional

# Rich UI Library
from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
    SpinnerColumn,
    TaskID
)
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

# AES-128 Decryption
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    HAS_CRYPTO = True
except ImportError:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        HAS_CRYPTO = True
    except ImportError:
        HAS_CRYPTO = False

# HTTP Client (curl_cffi for browser TLS fingerprint impersonation, fallback to requests)
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    import requests as cffi_requests
import requests

# Detect headless / CI / GitHub Actions environment
IS_CI = (
    os.environ.get("GITHUB_ACTIONS") == "true"
    or os.environ.get("CI") == "true"
    or not sys.stdout.isatty()
)

console = Console(force_terminal=True if IS_CI else None)


def get_ffmpeg_executable() -> Optional[str]:
    """Finds FFmpeg executable in system PATH or via bundled imageio_ffmpeg."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


class HLSDownloader:
    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        referer: Optional[str] = None,
        origin: Optional[str] = None,
        cookie: Optional[str] = None,
        threads: int = 32,
        timeout: int = 30,
        max_retries: int = 6,
        impersonate: str = "chrome120",
    ):
        self.threads = max(1, threads)
        self.timeout = max(5, timeout)
        self.max_retries = max(1, max_retries)
        self.impersonate = impersonate

        # Default browser headers
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }

        if referer:
            self.headers["Referer"] = referer
        if origin:
            self.headers["Origin"] = origin
        if cookie:
            self.headers["Cookie"] = cookie
        if headers:
            self.headers.update(headers)

        # Thread-local storage for persistent HTTP/2 and TCP session reuse
        self._local = threading.local()
        # Thread lock for thread-safe key cache
        self._key_lock = threading.Lock()
        # Key cache: URI -> raw key bytes
        self.key_cache: Dict[str, bytes] = {}

    def _get_thread_session(self):
        """Retrieves or creates a thread-local persistent HTTP session to reuse TCP/TLS connections."""
        session = getattr(self._local, "session", None)
        if session is None:
            if HAS_CURL_CFFI:
                session = cffi_requests.Session(impersonate=self.impersonate)
            else:
                session = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=16,
                    pool_maxsize=16,
                    max_retries=0
                )
                session.mount("http://", adapter)
                session.mount("https://", adapter)
            self._local.session = session
        return session

    def _reset_thread_session(self):
        """Closes and discards the thread-local session if an unrecoverable connection drop occurs."""
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            self._local.session = None

    def fetch_text(self, url: str) -> Tuple[int, str]:
        """Fetches text content with retry support."""
        for attempt in range(1, self.max_retries + 1):
            try:
                session = self._get_thread_session()
                resp = session.get(url, headers=self.headers, timeout=self.timeout)
                return resp.status_code, resp.text
            except Exception as e:
                self._reset_thread_session()
                if attempt == self.max_retries:
                    raise RuntimeError(f"Failed to fetch {url} after {self.max_retries} attempts: {e}")
                time.sleep(min(0.4 * attempt, 2.0))
        return 0, ""

    def fetch_bytes(self, url: str, byte_range: Optional[Tuple[int, int]] = None) -> bytes:
        """Fetches binary content with persistent session reuse and adaptive retry."""
        req_headers = dict(self.headers)
        if byte_range:
            start, length = byte_range
            req_headers["Range"] = f"bytes={start}-{start + length - 1}"

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            current_timeout = self.timeout + (attempt - 1) * 5
            try:
                session = self._get_thread_session()
                resp = session.get(url, headers=req_headers, timeout=current_timeout)
                if resp.status_code in (200, 206):
                    if resp.content:
                        return resp.content
                    raise ValueError(f"Empty content returned from {url}")
                elif resp.status_code == 403:
                    raise PermissionError(f"HTTP 403 Forbidden for {url}")
                else:
                    raise RuntimeError(f"HTTP {resp.status_code} for {url}")
            except Exception as e:
                last_error = e
                self._reset_thread_session()
                if attempt == self.max_retries:
                    raise RuntimeError(f"Failed to fetch segment after {self.max_retries} attempts: {last_error}")
                time.sleep(min(0.3 * attempt, 2.0))
        return b""

    def parse_master_playlist(self, master_text: str, master_url: str) -> List[Dict]:
        """Parses master playlist streams (#EXT-X-STREAM-INF)."""
        lines = master_text.strip().splitlines()
        streams = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXT-X-STREAM-INF:"):
                attr_line = line[len("#EXT-X-STREAM-INF:"):]
                attrs = {}
                # Extract comma-separated key=value or key="value"
                pattern = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)')
                for match in pattern.finditer(attr_line):
                    k, v = match.group(1), match.group(2).strip('"')
                    attrs[k] = v

                # The next non-empty line is the stream URI
                i += 1
                while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("#")):
                    i += 1
                if i < len(lines):
                    stream_uri = lines[i].strip()
                    full_uri = urljoin(master_url, stream_uri)
                    resolution = attrs.get("RESOLUTION", "Unknown")
                    bandwidth = int(attrs.get("BANDWIDTH", 0))
                    codecs = attrs.get("CODECS", "Unknown")
                    name = attrs.get("NAME", "")
                    frame_rate = attrs.get("FRAME-RATE", "")

                    streams.append({
                        "resolution": resolution,
                        "bandwidth": bandwidth,
                        "codecs": codecs,
                        "name": name,
                        "frame_rate": frame_rate,
                        "url": full_uri
                    })
            i += 1

        # Sort streams by bandwidth descending
        streams.sort(key=lambda s: s["bandwidth"], reverse=True)
        return streams

    def parse_media_playlist(self, playlist_text: str, playlist_url: str) -> Dict:
        """Parses media playlist segments, encryption keys, and init maps."""
        lines = playlist_text.strip().splitlines()
        segments = []
        current_key_info = None
        current_map = None
        seq_num = 0

        # Check media sequence start
        for line in lines:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    seq_num = int(line.split(":")[1].strip())
                except ValueError:
                    pass
                break

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            if line.startswith("#EXT-X-KEY:"):
                # Format: #EXT-X-KEY:METHOD=AES-128,URI="...",IV=0x...
                attr_line = line[len("#EXT-X-KEY:"):]
                pattern = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)')
                key_attrs = {m.group(1): m.group(2).strip('"') for m in pattern.finditer(attr_line)}
                method = key_attrs.get("METHOD", "NONE")
                if method == "NONE":
                    current_key_info = None
                else:
                    key_uri = key_attrs.get("URI")
                    if key_uri:
                        full_key_url = urljoin(playlist_url, key_uri)
                        iv_hex = key_attrs.get("IV")
                        current_key_info = {
                            "method": method,
                            "url": full_key_url,
                            "iv_hex": iv_hex,
                        }

            elif line.startswith("#EXT-X-MAP:"):
                # Fragmentation header (fMP4)
                attr_line = line[len("#EXT-X-MAP:"):]
                pattern = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)')
                map_attrs = {m.group(1): m.group(2).strip('"') for m in pattern.finditer(attr_line)}
                map_uri = map_attrs.get("URI")
                if map_uri:
                    current_map = urljoin(playlist_url, map_uri)

            elif line.startswith("#EXTINF:"):
                duration_str = line[len("#EXTINF:"):].split(",")[0].strip()
                try:
                    duration = float(duration_str)
                except ValueError:
                    duration = 0.0

                # Next line is the segment URI
                i += 1
                while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("#")):
                    # Check for byte range
                    if lines[i].strip().startswith("#EXT-X-BYTERANGE:"):
                        pass
                    i += 1

                if i < len(lines):
                    seg_uri = lines[i].strip()
                    full_seg_url = urljoin(playlist_url, seg_uri)

                    segments.append({
                        "index": len(segments),
                        "seq": seq_num + len(segments),
                        "url": full_seg_url,
                        "duration": duration,
                        "key_info": current_key_info,
                    })

            i += 1

        return {
            "map_url": current_map,
            "segments": segments,
            "is_encrypted": any(s.get("key_info") is not None for s in segments)
        }

    def get_encryption_key(self, key_url: str) -> bytes:
        """Fetches and caches AES decryption key thread-safely."""
        with self._key_lock:
            if key_url in self.key_cache:
                return self.key_cache[key_url]
        key_bytes = self.fetch_bytes(key_url)
        with self._key_lock:
            self.key_cache[key_url] = key_bytes
        return key_bytes

    def decrypt_aes128(self, encrypted_data: bytes, key: bytes, iv: bytes) -> bytes:
        """Decrypts AES-128 CBC ciphertext."""
        if not HAS_CRYPTO:
            raise RuntimeError("pycryptodome or cryptography is required for AES-128 decryption.")

        try:
            cipher = AES.new(key, AES.MODE_CBC, iv=iv)
            decrypted = cipher.decrypt(encrypted_data)
            try:
                return unpad(decrypted, AES.block_size)
            except Exception:
                # If padding removal fails (some TS files are unpadded or zero-padded), return raw
                return decrypted
        except NameError:
            # Fallback to cryptography package if pycryptodome not installed
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            decryptor = cipher.decryptor()
            return decryptor.update(encrypted_data) + decryptor.finalize()

    def sanitize_segment_data(self, data: bytes) -> bytes:
        """Strips fake image headers (PNG/JPEG/GIF) added by streaming hosts for obfuscation."""
        if not data:
            return data

        # Check for fake image headers commonly prepended by video hosts:
        # PNG: \x89PNG\r\n\x1a\n, JPEG: \xff\xd8\xff, GIF: GIF8, WebP/RIFF: RIFF....WEBP, BMP: BM
        is_obfuscated = (
            data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"GIF8")
            or data.startswith(b"BM")
            or (data.startswith(b"RIFF") and b"WEBP" in data[:16])
        )

        if is_obfuscated:
            # 1. Look for standard MPEG-TS sync byte 0x47 (188 bytes interval)
            max_scan = min(len(data) - 188 * 3, 16384)
            for offset in range(max_scan):
                if data[offset] == 0x47 and all(data[offset + k * 188] == 0x47 for k in range(1, 4)):
                    return data[offset:]

            # 2. Look for fMP4 boxes (styp, moof, ftyp, free)
            for box in (b"styp", b"moof", b"ftyp", b"free"):
                pos = data.find(box, 0, 16384)
                if pos >= 4:
                    box_offset = pos - 4
                    return data[box_offset:]

        return data

    def download_segment(self, segment: Dict, temp_dir: str) -> Tuple[int, int, str]:
        """Downloads and optionally decrypts a single video segment."""
        seq = segment["seq"]
        idx = segment["index"]
        seg_url = segment["url"]
        key_info = segment["key_info"]
        out_filename = os.path.join(temp_dir, f"seg_{idx:06d}.ts")

        # Fast resume: If segment already downloaded and non-empty, skip re-fetching
        if os.path.exists(out_filename) and os.path.getsize(out_filename) > 0:
            return idx, os.path.getsize(out_filename), out_filename

        # Download raw segment
        data = self.fetch_bytes(seg_url)

        # Decrypt if encrypted
        if key_info and key_info.get("method") == "AES-128":
            key_url = key_info["url"]
            key = self.get_encryption_key(key_url)

            # Determine IV
            if key_info.get("iv_hex"):
                iv_str = key_info["iv_hex"]
                if iv_str.lower().startswith("0x"):
                    iv_str = iv_str[2:]
                iv = bytes.fromhex(iv_str.zfill(32))
            else:
                iv = seq.to_bytes(16, byteorder="big")

            data = self.decrypt_aes128(data, key, iv)

        # Strip fake image headers or stream wrappers
        data = self.sanitize_segment_data(data)

        with open(out_filename, "wb") as f:
            f.write(data)

        return idx, len(data), out_filename


def prompt_quality_selection(streams: List[Dict]) -> Dict:
    """Displays a rich table and asks user to choose a quality."""
    table = Table(title="Available Streams in Master Playlist", show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Resolution", style="bold green", width=14)
    table.add_column("Bitrate", style="yellow", width=14)
    table.add_column("Codecs", style="magenta", width=22)
    table.add_column("FPS", style="blue", width=8)

    for idx, s in enumerate(streams):
        res = s["resolution"]
        bw = f"{s['bandwidth'] / 1000:,.0f} kbps" if s["bandwidth"] else "N/A"
        fps = s["frame_rate"] or "Auto"
        table.add_row(str(idx + 1), res, bw, s["codecs"], fps)

    console.print(table)
    console.print("[bold]Tip:[/bold] Press [cyan]Enter[/cyan] for the best quality [green](1)[/green].")

    while True:
        choice = Prompt.ask("Select stream number", default="1").strip()
        if choice.isdigit():
            c_int = int(choice)
            if 1 <= c_int <= len(streams):
                return streams[c_int - 1]
        console.print(f"[red]Invalid selection: {choice}. Choose 1 to {len(streams)}.[/red]")


def merge_segments_with_ffmpeg(
    ffmpeg_exe: str,
    temp_dir: str,
    output_path: str,
    segment_files: List[str],
    init_map_file: Optional[str] = None
) -> bool:
    """Concatenates TS/fMP4 files losslessly into final playable MP4."""
    concat_txt_path = os.path.join(temp_dir, "concat.txt")

    with open(concat_txt_path, "w", encoding="utf-8") as f:
        if init_map_file and os.path.exists(init_map_file):
            normalized = init_map_file.replace("\\", "/")
            f.write(f"file '{normalized}'\n")
        for seg_file in segment_files:
            normalized = seg_file.replace("\\", "/")
            f.write(f"file '{normalized}'\n")

    # 1. Primary attempt: Fast lossless stream-copy with bitstream filter & faststart
    cmd = [
        ffmpeg_exe,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_txt_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output_path
    ]

    import subprocess
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        return True

    # 2. Secondary attempt: Stream copy without aac bitstream filter (in case non-ADTS AAC)
    fallback_cmd = [
        ffmpeg_exe,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_txt_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path
    ]
    result_fallback = subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result_fallback.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        return True

    # 3. Tertiary attempt: Re-encode audio to AAC if stream copy failed due to timestamp / audio sync errors
    transcode_cmd = [
        ffmpeg_exe,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_txt_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-movflags", "+faststart",
        output_path
    ]
    result_transcode = subprocess.run(transcode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result_transcode.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024


def direct_binary_merge(segment_files: List[str], output_path: str, init_map_file: Optional[str] = None):
    """Fallback binary concatenation when FFmpeg is not present."""
    with open(output_path, "wb") as outfile:
        if init_map_file and os.path.exists(init_map_file):
            with open(init_map_file, "rb") as infile:
                shutil.copyfileobj(infile, outfile)
        for seg in segment_files:
            with open(seg, "rb") as infile:
                shutil.copyfileobj(infile, outfile)


def cleanup_stale_temp_dirs():
    """Removes leftover .temp_* folders from aborted previous runs."""
    try:
        cwd = os.getcwd()
        for item in os.listdir(cwd):
            if item.startswith(".temp_") and os.path.isdir(os.path.join(cwd, item)):
                try:
                    shutil.rmtree(os.path.join(cwd, item), ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass


atexit.register(cleanup_stale_temp_dirs)


def run_downloader(
    url: str,
    output: Optional[str] = None,
    referer: Optional[str] = None,
    origin: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    threads: int = 32,
    quality_pref: str = "best",
    keep_temp: bool = False,
    no_merge: bool = False,
):
    console.print(Panel.fit(
        "[bold cyan]M3U8 ULTRA DOWNLOADER[/bold cyan]\n"
        "[dim]High-Speed Multi-Threaded HLS Stream Downloader with Cloudflare & AES-128 Support[/dim]",
        border_style="cyan"
    ))

    # Initialize Downloader
    downloader = HLSDownloader(
        headers=headers,
        referer=referer,
        origin=origin,
        threads=threads
    )

    console.print(f"[bold blue]Target URL:[/bold blue] {url}")
    if referer:
        console.print(f"[dim]Referer:[/dim] {referer}")

    # Fetch initial M3U8
    with console.status("[bold yellow]Connecting to host and fetching M3U8 playlist...[/bold yellow]", spinner="dots"):
        try:
            status_code, m3u8_content = downloader.fetch_text(url)
        except Exception as e:
            console.print(f"[bold red]Connection Error:[/bold red] {e}")
            return False

    if status_code == 403:
        console.print(Panel(
            "[bold red]HTTP 403 Forbidden Error[/bold red]\n\n"
            "The server blocked the request. This typically happens because:\n"
            "1. [yellow]Temporary Token Expired:[/yellow] Video tokens in streaming URLs (like session hashes) expire quickly.\n"
            "2. [yellow]Missing Referer Header:[/yellow] Many video hosts only serve media if the [cyan]Referer[/cyan] header matches the web page where you found the video.\n\n"
            "[bold green]How to solve this:[/bold green]\n"
            "- Pass the webpage URL using [cyan]--referer <page_url>[/cyan]\n"
            "- Extract a fresh URL from the browser's Network tab (F12) right before downloading.",
            title="Troubleshooting Guide",
            border_style="red"
        ))
        return False
    elif status_code != 200 or not m3u8_content:
        console.print(f"[bold red]Failed to download M3U8:[/bold red] Server responded with HTTP {status_code}")
        return False

    # Check if Master Playlist or Media Playlist
    target_media_url = url
    target_media_content = m3u8_content

    if "#EXT-X-STREAM-INF" in m3u8_content:
        console.print("[green]Detected Master Playlist containing multiple stream variants.[/green]")
        streams = downloader.parse_master_playlist(m3u8_content, url)
        if not streams:
            console.print("[red]No playable streams found in master playlist.[/red]")
            return False

        # Select stream
        selected_stream = None
        if quality_pref.lower() == "best":
            selected_stream = streams[0]
            console.print(f"[green]Auto-selected Best Quality:[/green] [bold]{selected_stream['resolution']}[/bold] ({selected_stream['bandwidth'] // 1000} kbps)")
        elif quality_pref.lower() == "worst":
            selected_stream = streams[-1]
            console.print(f"[yellow]Selected Lowest Quality:[/yellow] [bold]{selected_stream['resolution']}[/bold]")
        elif quality_pref.isdigit():
            idx = int(quality_pref) - 1
            if 0 <= idx < len(streams):
                selected_stream = streams[idx]
            else:
                selected_stream = streams[0]
        else:
            # Interactive selection if possible
            if sys.stdin.isatty() and not IS_CI:
                selected_stream = prompt_quality_selection(streams)
            else:
                selected_stream = streams[0]

        target_media_url = selected_stream["url"]
        console.print(f"[cyan]Fetching media playlist for selected stream...[/cyan]")
        _, target_media_content = downloader.fetch_text(target_media_url)

    # Parse media playlist segments
    parsed = downloader.parse_media_playlist(target_media_content, target_media_url)
    segments = parsed["segments"]
    init_map_url = parsed["map_url"]
    is_encrypted = parsed["is_encrypted"]

    if not segments:
        console.print("[bold red]No video segments found in the playlist.[/bold red]")
        return False

    total_duration = sum(s["duration"] for s in segments)
    console.print(f"[green]Found [bold]{len(segments)}[/bold] video segments "
                  f"(~{total_duration / 60:.1f} minutes)[/green]")
    if is_encrypted:
        console.print("[yellow]Stream uses AES-128 encryption. Pre-fetching decryption keys...[/yellow]")
        # Pre-cache key upfront to avoid concurrency locks
        for s in segments:
            k_info = s.get("key_info")
            if k_info and k_info.get("url"):
                try:
                    downloader.get_encryption_key(k_info["url"])
                except Exception as e:
                    console.print(f"[yellow]Key pre-fetch warning: {e}[/yellow]")
                break

    # Determine output filename
    if not output:
        # Default filename based on URL or timestamp
        path_name = os.path.basename(urlparse(url).path).replace(".m3u8", "")
        if not path_name or path_name == "master":
            path_name = f"downloaded_video_{int(time.time())}"
        output = f"{path_name}.mp4"

    if not output.endswith(".mp4") and not output.endswith(".ts"):
        output += ".mp4"

    # Prepare temp directory with guaranteed cleanup
    temp_dir = os.path.abspath(f".temp_{abs(hash(url))}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        # Download init map segment if fMP4
        init_map_path = None
        if init_map_url:
            console.print("[dim]Downloading initialization map segment (fMP4)...[/dim]")
            map_bytes = downloader.fetch_bytes(init_map_url)
            init_map_path = os.path.join(temp_dir, "init.mp4")
            with open(init_map_path, "wb") as f:
                f.write(map_bytes)

        # Multi-threaded segment download with Rich progress bar
        ordered_segment_files = [None] * len(segments)
        downloaded_bytes = 0
        start_time = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TextColumn("[bold yellow]{task.completed}/{task.total} segs[/bold yellow]"),
            TextColumn("•"),
            TransferSpeedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
            refresh_per_second=1 if IS_CI else 10
        ) as progress:
            task = progress.add_task("Downloading Video", total=len(segments))

            with ThreadPoolExecutor(max_workers=downloader.threads) as executor:
                future_to_seg = {
                    executor.submit(downloader.download_segment, seg, temp_dir): seg
                    for seg in segments
                }

                for future in as_completed(future_to_seg):
                    try:
                        idx, seg_size, file_path = future.result()
                        ordered_segment_files[idx] = file_path
                        downloaded_bytes += seg_size
                        progress.update(task, advance=1)
                    except Exception as e:
                        console.print(f"\n[red]Error downloading segment: {e}[/red]")

        # Check for any segments that failed and retry them
        missing_indices = [i for i, f in enumerate(ordered_segment_files) if f is None or not os.path.exists(f)]
        if missing_indices:
            console.print(f"[yellow]Retrying {len(missing_indices)} failed segment(s)...[/yellow]")
            for idx in missing_indices:
                seg = segments[idx]
                try:
                    _, seg_size, file_path = downloader.download_segment(seg, temp_dir)
                    ordered_segment_files[idx] = file_path
                    downloaded_bytes += seg_size
                except Exception as e:
                    console.print(f"[red]Retry failed for segment {idx}: {e}[/red]")

        # Filter only successfully downloaded segment files
        valid_segment_files = [f for f in ordered_segment_files if f and os.path.exists(f)]
        if not valid_segment_files:
            console.print("[bold red]Failed to download any video segments.[/bold red]")
            return False

        if len(valid_segment_files) < len(segments):
            console.print(f"[yellow]Warning: {len(segments) - len(valid_segment_files)} segments could not be fetched. Merging {len(valid_segment_files)} available segments.[/yellow]")

        elapsed = max(0.1, time.time() - start_time)
        avg_speed = (downloaded_bytes / (1024 * 1024)) / elapsed
        console.print(f"[bold green]Downloaded {len(valid_segment_files)}/{len(segments)} segments ({downloaded_bytes / (1024 * 1024):.1f} MB) in {elapsed:.1f}s (~{avg_speed:.2f} MB/s)[/bold green]")

        # Remux or concatenate
        ffmpeg_exe = get_ffmpeg_executable()
        out_dir = os.path.dirname(os.path.abspath(output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if ffmpeg_exe and not no_merge:
            with console.status("[bold cyan]Merging and remuxing segments to MP4 via FFmpeg (lossless copy)...[/bold cyan]"):
                success = merge_segments_with_ffmpeg(
                    ffmpeg_exe=ffmpeg_exe,
                    temp_dir=temp_dir,
                    output_path=output,
                    segment_files=valid_segment_files,
                    init_map_file=init_map_path
                )
            if success and os.path.exists(output) and os.path.getsize(output) > 0:
                console.print(f"[bold green]Video successfully saved to:[/bold green] [underline]{os.path.abspath(output)}[/underline]")
            else:
                console.print("[yellow]FFmpeg remux encountered an issue. Falling back to direct binary merge (.ts)...[/yellow]")
                ts_output = output.rsplit(".", 1)[0] + ".ts"
                direct_binary_merge(valid_segment_files, ts_output, init_map_path)
                console.print(f"[bold green]Saved as TS container:[/bold green] {ts_output}")
        else:
            if not ffmpeg_exe:
                console.print("[dim]FFmpeg not found. Performing direct binary merge to TS...[/dim]")
            ts_output = output.rsplit(".", 1)[0] + ".ts"
            direct_binary_merge(valid_segment_files, ts_output, init_map_path)
            console.print(f"[bold green]Saved as:[/bold green] {ts_output}")

        return True
    finally:
        # Clean up temporary segments
        if not keep_temp and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


MEGAPLAY_JSON_URL = "https://raw.githubusercontent.com/donkarboy/megaplay-extractor-updated/refs/heads/main/streams/megaplay_stream.json"
DEFAULT_MEGAPLAY_REFERER = "https://megaplay.buzz/"
_MEGAPLAY_DB_CACHE = None


def fetch_megaplay_database() -> Dict:
    """Fetches and caches the megaplay stream JSON database."""
    global _MEGAPLAY_DB_CACHE
    if _MEGAPLAY_DB_CACHE is not None:
        return _MEGAPLAY_DB_CACHE

    import json
    try:
        import urllib.request
        with console.status("[bold cyan]Fetching Megaplay streams database from GitHub...[/bold cyan]", spinner="dots"):
            req = urllib.request.Request(
                MEGAPLAY_JSON_URL,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        _MEGAPLAY_DB_CACHE = data.get("entries", data)
        return _MEGAPLAY_DB_CACHE
    except Exception as e:
        console.print(f"[red]Error fetching stream database: {e}[/red]")
        return {}


def parse_episode_input(ep_str: str, anime_entry: Optional[Dict] = None) -> List[int]:
    """
    Parses episode inputs like '2', '1 to 30', '1-30', '1, 2, 5', or 'all'.
    When 'all' is passed and anime_entry is given, extracts all available dub episodes.
    """
    ep_str = ep_str.strip().lower()

    if ep_str == "all":
        if anime_entry:
            dub_eps = set()
            for k in anime_entry.keys():
                m = re.match(r"^ep-(\d+)-dub-", k)
                if m:
                    dub_eps.add(int(m.group(1)))
            if dub_eps:
                return sorted(list(dub_eps))

            # Fallback to total_episodes if dub prefix not directly matched
            tot = anime_entry.get("total_episodes")
            if tot and str(tot).isdigit():
                return list(range(1, int(tot) + 1))
        return []

    episodes = set()
    parts = re.split(r"[,;]+", ep_str)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part == "all":
            if anime_entry:
                return parse_episode_input("all", anime_entry)
        range_match = re.match(r"^(\d+)\s*(?:to|-)\s*(\d+)$", part)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start <= end:
                episodes.update(range(start, end + 1))
            else:
                episodes.update(range(end, start + 1))
        elif part.isdigit():
            episodes.add(int(part))

    return sorted(list(episodes))


def get_dub_stream_for_episode(entry: Dict, episode_num: int) -> Optional[Tuple[str, str]]:
    """
    Finds the dub stream for a given episode number.
    Prioritizes 'master.m3u8' URLs.
    Returns (stream_label, stream_url) or None if not found.
    """
    # Pattern: ep-{episode_num}-dub-{server_num}
    prefix = f"ep-{episode_num}-dub-"
    dub_streams = []
    for k, v in entry.items():
        if k.startswith(prefix) and isinstance(v, str) and v.startswith("http"):
            dub_streams.append((k, v))

    if not dub_streams:
        return None

    # Prioritize master.m3u8 as default
    for k, v in dub_streams:
        if "master.m3u8" in v.lower():
            return (k, v)

    # Otherwise return the first available dub stream
    return dub_streams[0]


def run_mal_download_flow(
    mal_id: str,
    episode_input: str,
    threads: int = 32,
    quality: str = "best",
    overwrite: bool = False
) -> bool:
    """Orchestrates downloading dub episodes for a specific MAL ID."""
    db = fetch_megaplay_database()
    if not db:
        console.print("[red]Could not load stream database. Please check your connection.[/red]")
        return False

    mal_id = str(mal_id).strip()
    if mal_id not in db:
        console.print(f"[bold red]MAL ID '{mal_id}' was not found in the database.[/bold red]")
        console.print(f"[dim]Available MAL IDs: {', '.join(list(db.keys())[:15])}...[/dim]")
        return False

    anime_entry = db[mal_id]
    total_eps = anime_entry.get("total_episodes", "Unknown")
    console.print(f"[green]Found anime entry for MAL ID [bold]{mal_id}[/bold] (Total episodes in DB: {total_eps})[/green]")

    target_episodes = parse_episode_input(episode_input, anime_entry=anime_entry)
    if not target_episodes:
        console.print(f"[bold red]Invalid episode specification:[/bold red] '{episode_input}'")
        return False

    if len(target_episodes) <= 20:
        ep_display = str(target_episodes)
    else:
        ep_display = f"[{target_episodes[0]}, {target_episodes[1]}, ... {target_episodes[-1]}] (Total: {len(target_episodes)} episodes)"

    console.print(f"[cyan]Target Dub Episodes ({len(target_episodes)}):[/cyan] {ep_display}")

    # Prepare downloads
    queued_downloads = []
    for ep in target_episodes:
        dub_info = get_dub_stream_for_episode(anime_entry, ep)
        if dub_info:
            label, stream_url = dub_info
            out_filename = f"mal_{mal_id}_ep_{ep:02d}_dub.mp4"
            queued_downloads.append((ep, label, stream_url, out_filename))
        else:
            console.print(f"[yellow]Episode {ep}: No DUB stream available in database. Skipping.[/yellow]")

    if not queued_downloads:
        console.print("[bold red]No valid dub streams found for the specified episode(s).[/bold red]")
        return False

    console.print(f"\n[bold green]Ready to download {len(queued_downloads)} dub episode(s):[/bold green]")
    for ep, label, stream_url, out_name in queued_downloads[:8]:
        console.print(f"  • Episode {ep:02d} ({label}): [cyan]{out_name}[/cyan]")
    if len(queued_downloads) > 8:
        console.print(f"  [dim]... and {len(queued_downloads) - 8} more episode(s).[/dim]")
    console.print()

    # Execute downloads
    success_count = 0
    for current_idx, (ep, label, stream_url, out_filename) in enumerate(queued_downloads, 1):
        if not overwrite and os.path.exists(out_filename) and os.path.getsize(out_filename) > 1024 * 1024:
            size_mb = os.path.getsize(out_filename) / (1024 * 1024)
            console.print(f"[yellow]Episode {ep:02d}: '{out_filename}' already exists ({size_mb:.1f} MB). Skipping.[/yellow]")
            success_count += 1
            continue

        console.print(Panel(
            f"[bold cyan]Processing Episode {ep:02d} ({current_idx}/{len(queued_downloads)})[/bold cyan]\n"
            f"[dim]Source: {label}[/dim]\n"
            f"[bold green]Output: {out_filename}[/bold green]",
            border_style="green"
        ))

        ok = run_downloader(
            url=stream_url,
            output=out_filename,
            referer=DEFAULT_MEGAPLAY_REFERER,
            threads=threads,
            quality_pref=quality
        )
        if ok:
            success_count += 1

    console.print(f"\n[bold green]Finished processing MAL ID {mal_id}! ({success_count}/{len(queued_downloads)} episodes completed)[/bold green]")
    return success_count > 0


def parse_batch_line(line: str) -> Optional[Tuple[str, str]]:
    """
    Parses a single line from input_data.txt or batch file.
    Supports:
      - mal id = 1735 ep = 1 to 35
      - mal id = 1735 ep = 25
      - mal id = 1735 all
      - mal id = 1735 ep = all
      - mal id = 1735
      - 1735 ep = 1 to 35
    Returns (mal_id, episode_spec) or None.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    clean_line = line.replace(",", " ")

    # Pattern 1: mal id = <id> ep = <spec>
    m1 = re.search(r"mal(?:_|\s+)?id\s*[:=]?\s*(\d+)\s*(?:ep(?:isode)?\s*[:=]?\s*(.+))", clean_line, re.IGNORECASE)
    if m1:
        return m1.group(1).strip(), m1.group(2).strip()

    # Pattern 2: mal id = <id> all   or   mal id = <id>
    m2 = re.search(r"mal(?:_|\s+)?id\s*[:=]?\s*(\d+)(?:\s+(all))?", clean_line, re.IGNORECASE)
    if m2:
        return m2.group(1).strip(), (m2.group(2) or "all").strip()

    # Pattern 3: direct digits: "<id> ep <spec>" or "<id> all"
    m3 = re.search(r"^(\d+)\s*(?:ep(?:isode)?\s*[:=]?\s*(.+)|(all))", clean_line, re.IGNORECASE)
    if m3:
        return m3.group(1).strip(), (m3.group(2) or m3.group(3) or "all").strip()

    return None


def parse_batch_file(filepath: str) -> List[Dict]:
    """Reads tasks from input_data.txt or any batch file."""
    if not os.path.exists(filepath):
        return []

    tasks = []
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line_num, line in enumerate(f, 1):
            parsed = parse_batch_line(line)
            if parsed:
                mal_id, ep_spec = parsed
                tasks.append({
                    "line_num": line_num,
                    "mal_id": mal_id,
                    "ep_spec": ep_spec,
                    "raw_line": line.strip()
                })
    return tasks


def run_batch_download_flow(
    filepath: str = "input_data.txt",
    threads: int = 32,
    quality: str = "best",
    overwrite: bool = False
) -> bool:
    """Executes all download instructions specified in the batch input file."""
    if not os.path.exists(filepath):
        console.print(f"[bold red]Input file not found:[/bold red] {filepath}")
        return False

    tasks = parse_batch_file(filepath)
    if not tasks:
        console.print(f"[bold yellow]No valid download tasks found in '{filepath}'.[/bold yellow]")
        console.print("[dim]Ensure your file contains lines like:\n  mal id = 1735 ep = 1 to 35\n  mal id = 1735 ep = 25\n  mal id = 1735 all[/dim]")
        return False

    console.print(Panel.fit(
        f"[bold cyan]BATCH DOWNLOAD MODE[/bold cyan] - Loaded from [green]{os.path.basename(filepath)}[/green]\n"
        f"[bold green]{len(tasks)}[/bold green] task line(s) queued for download",
        border_style="cyan"
    ))

    # Display task table
    table = Table(title=f"Queued Tasks from {os.path.basename(filepath)}", show_header=True, header_style="bold magenta")
    table.add_column("Task #", style="dim", width=8)
    table.add_column("Line", style="dim", width=6)
    table.add_column("MAL ID", style="bold green", width=12)
    table.add_column("Episode Target", style="cyan", width=20)
    table.add_column("Original Line", style="dim")

    for idx, t in enumerate(tasks, 1):
        table.add_row(str(idx), str(t["line_num"]), t["mal_id"], t["ep_spec"], t["raw_line"])

    console.print(table)
    console.print()

    total_tasks = len(tasks)
    successful_tasks = 0

    for idx, task in enumerate(tasks, 1):
        console.print(Panel(
            f"[bold yellow]=== EXECUTING TASK {idx}/{total_tasks}: MAL ID {task['mal_id']} (Episodes: {task['ep_spec']}) ===[/bold yellow]",
            border_style="yellow"
        ))
        ok = run_mal_download_flow(
            mal_id=task["mal_id"],
            episode_input=task["ep_spec"],
            threads=threads,
            quality=quality,
            overwrite=overwrite
        )
        if ok:
            successful_tasks += 1

    console.print(Panel.fit(
        f"[bold green]BATCH PROCESSING COMPLETED[/bold green]\n"
        f"Processed: [bold green]{successful_tasks}/{total_tasks}[/bold green] tasks successfully.",
        border_style="green"
    ))
    return successful_tasks > 0


def interactive_mode():
    """Interactive guided mode when no CLI arguments are supplied."""
    default_input_file = "input_data.txt"

    # Auto-fallback in non-interactive CI / GitHub Actions environment
    if IS_CI:
        if os.path.exists(default_input_file):
            console.print(f"[bold cyan]Headless / CI environment detected. Automatically processing '{default_input_file}' with 32 threads...[/bold cyan]")
            run_batch_download_flow(filepath=default_input_file, threads=32, quality="best")
            return
        else:
            console.print(f"[bold red]Headless environment error: '{default_input_file}' not found and no CLI parameters provided.[/bold red]")
            sys.exit(1)

    console.print(Panel.fit(
        "[bold cyan]M3U8 ULTRA DOWNLOADER[/bold cyan] - [dim]Interactive Setup[/dim]\n"
        "[dim]Supports input_data.txt Batch File, MAL Anime ID lookup, & Direct M3U8 URLs[/dim]",
        border_style="cyan"
    ))

    # Auto-detect input_data.txt
    batch_tasks = parse_batch_file(default_input_file) if os.path.exists(default_input_file) else []

    if batch_tasks:
        console.print(f"[bold green]Detected '{default_input_file}' with {len(batch_tasks)} queued task(s):[/bold green]")
        for t in batch_tasks[:5]:
            console.print(f"  • Line {t['line_num']}: [bold]MAL ID {t['mal_id']}[/bold] -> Episodes: [cyan]{t['ep_spec']}[/cyan]")
        if len(batch_tasks) > 5:
            console.print(f"  [dim]... and {len(batch_tasks) - 5} more task(s).[/dim]")

        use_file = Confirm.ask(f"\nDo you want to run tasks from [cyan]{default_input_file}[/cyan]?", default=True)
        if use_file:
            threads_str = Prompt.ask("[bold cyan]Download threads[/bold cyan]", default="32").strip()
            threads = int(threads_str) if threads_str.isdigit() else 32
            quality = Prompt.ask("[bold cyan]Quality preference[/bold cyan] ([green]best[/green]/[yellow]worst[/yellow]/ask)", default="best").strip()
            run_batch_download_flow(filepath=default_input_file, threads=threads, quality=quality)
            return

    console.print("\n[bold yellow]Choose Download Mode:[/bold yellow]")
    console.print("  [1] [bold green]Anime by MAL ID[/bold green] (Single Anime Lookup)")
    console.print("  [2] [bold cyan]Batch File[/bold cyan] (Process input_data.txt or custom file)")
    console.print("  [3] [bold magenta]Direct M3U8 URL[/bold magenta]")
    mode = Prompt.ask("Select mode", choices=["1", "2", "3"], default="1").strip()

    if mode == "1":
        mal_id = Prompt.ask("[bold green]Enter Anime MAL ID[/bold green] [dim](e.g. 1735)[/dim]").strip()
        while not mal_id:
            mal_id = Prompt.ask("[bold green]Enter Anime MAL ID[/bold green]").strip()

        ep_input = Prompt.ask("[bold cyan]Enter Episode or Range[/bold cyan] [dim](e.g. '2' or '1 to 35' or 'all')[/dim]", default="all").strip()
        while not ep_input:
            ep_input = Prompt.ask("[bold cyan]Enter Episode or Range[/bold cyan]").strip()

        threads_str = Prompt.ask("[bold cyan]Download threads[/bold cyan]", default="32").strip()
        threads = int(threads_str) if threads_str.isdigit() else 32

        quality = Prompt.ask("[bold cyan]Quality preference[/bold cyan] ([green]best[/green]/[yellow]worst[/yellow]/ask)", default="best").strip()

        run_mal_download_flow(mal_id=mal_id, episode_input=ep_input, threads=threads, quality=quality)
        return

    elif mode == "2":
        filepath = Prompt.ask("[bold cyan]Enter batch file path[/bold cyan]", default="input_data.txt").strip()
        threads_str = Prompt.ask("[bold cyan]Download threads[/bold cyan]", default="32").strip()
        threads = int(threads_str) if threads_str.isdigit() else 32
        quality = Prompt.ask("[bold cyan]Quality preference[/bold cyan] ([green]best[/green]/[yellow]worst[/yellow]/ask)", default="best").strip()
        run_batch_download_flow(filepath=filepath, threads=threads, quality=quality)
        return

    # Direct URL mode
    url = Prompt.ask("[bold green]Enter M3U8 URL[/bold green]").strip()
    while not url.startswith("http://") and not url.startswith("https://"):
        console.print("[red]Please enter a valid HTTP/HTTPS URL.[/red]")
        url = Prompt.ask("[bold green]Enter M3U8 URL[/bold green]").strip()

    referer = Prompt.ask("[bold cyan]Enter Referer header[/bold cyan] [dim](press Enter to skip)[/dim]", default="").strip()
    if not referer:
        referer = None

    default_name = "downloaded_video.mp4"
    output = Prompt.ask("[bold cyan]Enter output filename[/bold cyan]", default=default_name).strip()

    threads_str = Prompt.ask("[bold cyan]Download threads[/bold cyan]", default="32").strip()
    threads = int(threads_str) if threads_str.isdigit() else 32

    quality = Prompt.ask("[bold cyan]Quality preference[/bold cyan] ([green]best[/green]/[yellow]worst[/yellow]/ask)", default="best").strip()

    run_downloader(
        url=url,
        output=output,
        referer=referer,
        threads=threads,
        quality_pref=quality
    )


def main():
    parser = argparse.ArgumentParser(
        description="M3U8 Ultra Downloader - Fast multi-threaded HLS downloader with batch file input, MAL database, AES-128 & FFmpeg support.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python "m3u8 downloader.py"
  python "m3u8 downloader.py" --file input_data.txt
  python "m3u8 downloader.py" --mal 1735 --ep "1 to 35"
  python "m3u8 downloader.py" --mal 1735 --ep 25
  python "m3u8 downloader.py" --mal 1735 --ep all
  python "m3u8 downloader.py" -u "https://example.com/master.m3u8" -o "video.mp4"
        """
    )
    parser.add_argument("-f", "--file", "--input-file", type=str, help="Process download tasks from input_data.txt or custom file")
    parser.add_argument("--mal", type=str, help="Anime MyAnimeList (MAL) ID to download dub from database")
    parser.add_argument("--ep", "--episode", type=str, help="Episode or episode range (e.g. '25' or '1 to 35' or 'all')")
    parser.add_argument("-u", "--url", type=str, help="Direct M3U8 playlist URL (master or media)")
    parser.add_argument("-o", "--output", type=str, help="Output MP4 file path")
    parser.add_argument("-r", "--referer", type=str, help="Custom Referer URL (useful to bypass 403 Forbidden)")
    parser.add_argument("--origin", type=str, help="Custom Origin header")
    parser.add_argument("-t", "--threads", type=int, default=32, help="Concurrent download threads (default: 32)")
    parser.add_argument("-q", "--quality", type=str, default="best", help="Quality preference: 'best', 'worst', or stream number")
    parser.add_argument("-H", "--header", action="append", help="Custom headers in 'Key: Value' format (can be specified multiple times)")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary downloaded segment files")
    parser.add_argument("--no-merge", action="store_true", help="Do not merge segments with FFmpeg")
    parser.add_argument("--overwrite", action="store_true", help="Force overwrite existing downloaded video files")
    parser.add_argument("--ci", action="store_true", help="Force CI / Non-interactive headless execution mode")

    args = parser.parse_args()

    # Parse custom headers list
    custom_headers = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                custom_headers[k.strip()] = v.strip()

    if args.file:
        run_batch_download_flow(
            filepath=args.file,
            threads=args.threads,
            quality=args.quality,
            overwrite=args.overwrite
        )
    elif args.mal:
        ep_arg = args.ep or "all"
        run_mal_download_flow(
            mal_id=args.mal,
            episode_input=ep_arg,
            threads=args.threads,
            quality=args.quality,
            overwrite=args.overwrite
        )
    elif args.url:
        run_downloader(
            url=args.url,
            output=args.output,
            referer=args.referer,
            origin=args.origin,
            headers=custom_headers,
            threads=args.threads,
            quality_pref=args.quality,
            keep_temp=args.keep_temp,
            no_merge=args.no_merge
        )
    else:
        if args.ci or IS_CI:
            default_file = "input_data.txt"
            if os.path.exists(default_file):
                console.print(f"[bold cyan]Auto-executing '{default_file}' in CI mode...[/bold cyan]")
                run_batch_download_flow(
                    filepath=default_file,
                    threads=args.threads,
                    quality=args.quality,
                    overwrite=args.overwrite
                )
            else:
                console.print("[bold red]CI mode: No input_data.txt found.[/bold red]")
                sys.exit(1)
        else:
            interactive_mode()


if __name__ == "__main__":
    main()

