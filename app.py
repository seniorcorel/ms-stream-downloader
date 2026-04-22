"""
Microsoft Stream Video Downloader - Web Version
Descarga directa con requests (auth via cookies) + merge con ffmpeg local.
"""

import subprocess
import os
import json
import uuid
import threading
import re
import time
import xml.etree.ElementTree as ET
from http.cookiejar import MozillaCookieJar
from urllib.parse import urljoin, urlparse
from html import unescape as html_unescape

import requests
from flask import Flask, render_template, request, Response, jsonify, send_from_directory

app = Flask(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_FFMPEG_DIR = os.path.join(
    SCRIPT_DIR, "ffmpeg_extracted", "ffmpeg-8.1-essentials_build", "bin"
)
DOWNLOADS_DIR = os.path.join(SCRIPT_DIR, "downloads")
COOKIES_DIR = os.path.join(SCRIPT_DIR, "cookies")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)

downloads = {}


def get_ffmpeg_exe(name="ffmpeg"):
    # En Docker/Linux, ffmpeg está en PATH
    local = os.path.join(LOCAL_FFMPEG_DIR, f"{name}.exe")
    if os.path.isfile(local):
        return local
    # Linux binary without .exe
    local_linux = os.path.join(LOCAL_FFMPEG_DIR, name)
    if os.path.isfile(local_linux):
        return local_linux
    import shutil
    return shutil.which(name) or name


def clean_manifest_url(url):
    """Trunca la URL del manifest después de format=dash."""
    for marker in ["&format=dash", "?format=dash"]:
        idx = url.find(marker)
        if idx != -1:
            return url[:idx + len(marker)]
    return url


class DynamicOriginSession(requests.Session):
    """Session que ajusta Origin/Referer automáticamente al dominio de cada request,
    o usa un origin fijo si se configuró."""

    def __init__(self, fixed_origin=None):
        super().__init__()
        self.fixed_origin = fixed_origin

    def request(self, method, url, **kwargs):
        if self.fixed_origin:
            origin = self.fixed_origin.rstrip("/")
        else:
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.hostname}"
        self.headers["Origin"] = origin
        self.headers["Referer"] = origin + "/"
        return super().request(method, url, **kwargs)


def parse_raw_cookies(cookie_string, domain):
    """Parsea un string de cookies raw (Cookie: header del navegador) y devuelve un dict."""
    cookies = {}
    if not cookie_string:
        return cookies
    # Limpiar prefijo "Cookie: " si lo pegaron
    cookie_string = cookie_string.strip()
    if cookie_string.lower().startswith("cookie:"):
        cookie_string = cookie_string[len("cookie:"):].strip()
    for pair in cookie_string.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def create_session(token=None, cookies_path=None, origin_override=None, raw_cookies=None, referer=None):
    """Crea una session agnóstica.
    - token: solo se usa para el manifest (x-spopactoken)
    - cookies: se usan para todo (especialmente segmentos)
    - raw_cookies: string de cookies pegado del navegador
    """
    session = DynamicOriginSession(fixed_origin=origin_override or None)
    session.verify = False

    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    if referer:
        session.headers["Referer"] = referer
    if token:
        token = token.strip()
        if token.lower().startswith("bearer "):
            session.headers["Authorization"] = token
            session.headers["x-spopactoken"] = token[len("Bearer "):].strip()
        else:
            session.headers["x-spopactoken"] = token

    # Recopilar cookies de todas las fuentes
    all_cookies = {}

    # Detectar dominio del sharepoint para filtrar cookies
    sp_domains = set()
    if origin_override:
        sp_domains.add(urlparse(origin_override).hostname)

    # 1. Desde archivo cookies.txt (MozillaCookieJar) — solo cookies de SharePoint
    if cookies_path and os.path.isfile(cookies_path):
        try:
            jar = MozillaCookieJar(cookies_path)
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                domain = cookie.domain.lstrip(".")
                # Solo cookies de sharepoint o del dominio target
                if "sharepoint.com" in domain or "office.com" in domain or "microsoftonline.com" in domain or \
                   any(domain.endswith(sp) or sp.endswith(domain) for sp in sp_domains):
                    all_cookies[cookie.name] = cookie.value
        except Exception:
            pass

    # 2. Desde raw cookies (prioridad sobre archivo — se asume que son del dominio correcto)
    if raw_cookies:
        parsed = parse_raw_cookies(raw_cookies, "")
        all_cookies.update(parsed)

    # Setear cookies sin restricciones de dominio/path
    for name, value in all_cookies.items():
        session.cookies.set(name, value)

    return session


def parse_dash_manifest(xml_text):
    """Parsea DASH MPD y devuelve listas de representaciones de video y audio."""
    # Limpiar namespace para simplificar XPath
    xml_text = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
    root = ET.fromstring(xml_text)

    # BaseURL global del MPD
    mpd_base = ""
    base_el = root.find("BaseURL")
    if base_el is not None and base_el.text:
        mpd_base = base_el.text.strip()

    videos = []
    audios = []

    for adapt_set in root.findall(".//AdaptationSet"):
        mime = adapt_set.get("mimeType", "")
        content_type = adapt_set.get("contentType", "")
        is_video = "video" in mime or "video" in content_type
        is_audio = "audio" in mime or "audio" in content_type

        for rep in adapt_set.findall("Representation"):
            info = {
                "id": rep.get("id", ""),
                "bandwidth": int(rep.get("bandwidth", 0)),
                "width": int(rep.get("width", 0) or 0),
                "height": int(rep.get("height", 0) or 0),
                "codecs": rep.get("codecs", ""),
                "mime": rep.get("mimeType", mime),
                "base_url": None,
                "segments": [],
                "mpd_base": mpd_base,
            }

            # BaseURL (rep level > adaptset level > mpd level)
            for el in [rep, adapt_set]:
                b = el.find("BaseURL")
                if b is not None and b.text:
                    info["base_url"] = b.text.strip()
                    break

            # SegmentTemplate
            seg_tpl = rep.find("SegmentTemplate") or adapt_set.find("SegmentTemplate")

            if seg_tpl is not None:
                init_tpl = seg_tpl.get("initialization", "")
                media_tpl = seg_tpl.get("media", "")

                # Fix corrupted template vars: $RepresentationID48999e13...amp; -> $RepresentationID$&
                def fix_template(tpl, rep_id):
                    # Replace corrupted $Xxx<guid>amp; patterns
                    tpl = re.sub(r'\$RepresentationID[0-9a-f-]+amp;', rep_id + '&', tpl)
                    tpl = re.sub(r'\$Time[0-9a-f-]+amp;', '$Time$&', tpl)
                    tpl = re.sub(r'\$Bandwidth[0-9a-f-]+amp;', str(info["bandwidth"]) + '&', tpl)
                    tpl = re.sub(r'\$Number[0-9a-f-]+amp;', '$Number$&', tpl)
                    # Normal replacements
                    tpl = tpl.replace("$RepresentationID$", rep_id)
                    tpl = tpl.replace("$Bandwidth$", str(info["bandwidth"]))
                    return tpl

                init_url = fix_template(init_tpl, info["id"])
                # Intentar desactivar encriptación en las URLs
                init_url = init_url.replace("enableEncryption=1", "enableEncryption=0")
                info["init_url"] = init_url

                media_tpl_fixed = fix_template(media_tpl, info["id"])
                media_tpl_fixed = media_tpl_fixed.replace("enableEncryption=1", "enableEncryption=0")

                # SegmentTimeline
                timeline = seg_tpl.find("SegmentTimeline")
                if timeline is not None:
                    t = 0
                    for s_el in list(timeline):
                        if s_el.tag != "S" and not s_el.tag.endswith("}S"):
                            continue
                        t = int(s_el.get("t", t))
                        d = int(s_el.get("d", 0))
                        r_count = int(s_el.get("r", 0))
                        for _ in range(r_count + 1):
                            seg_url = media_tpl_fixed.replace("$Time$", str(t))
                            seg_url = seg_url.replace("$Number$", str(len(info["segments"])))
                            info["segments"].append(seg_url)
                            t += d

            rep_mime = info.get("mime", "")
            if is_video or "video" in rep_mime:
                videos.append(info)
            elif is_audio or "audio" in rep_mime:
                audios.append(info)

    videos.sort(key=lambda x: x.get("height", 0) or x.get("bandwidth", 0), reverse=True)
    audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)

    # Extract encryption info from ContentProtection
    encryption = None
    for cp in root.findall(".//{urn:mpeg:dash:schema:sea:2012}CryptoPeriod"):
        key_url = cp.get("keyUriTemplate", "")
        iv_hex = cp.get("IV", "")
        if key_url and iv_hex:
            # Unescape XML entities in the URL
            key_url = html_unescape(key_url)
            encryption = {"key_url": key_url, "iv": iv_hex}
            break
    # Also try without namespace
    if not encryption:
        for cp in root.findall(".//*[@keyUriTemplate]"):
            key_url = cp.get("keyUriTemplate", "")
            iv_hex = cp.get("IV", "")
            if key_url and iv_hex:
                key_url = html_unescape(key_url)
                encryption = {"key_url": key_url, "iv": iv_hex}
                break

    return videos, audios, encryption


def select_rep(reps, quality):
    if not reps:
        return None
    if quality == "worst":
        return reps[-1]
    if quality == "best":
        return reps[0]
    target = int(quality.replace("p", ""))
    for r in reps:
        if r.get("height", 0) <= target:
            return r
    return reps[-1]


def download_representation(session, rep, base_url, dest_path, info, label, decrypt_key=None, decrypt_iv=None):
    """Descarga un stream completo (video o audio) usando requests. Descifra al vuelo si hay clave."""
    effective_base = rep.get("mpd_base") or base_url
    info["log"].append(f"{label} base: {effective_base[:80]}...")

    def decrypt_segment(data):
        """Descifra un segmento AES-128-CBC."""
        if not decrypt_key or not decrypt_iv:
            return data
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(decrypt_key), modes.CBC(decrypt_iv))
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(data) + decryptor.finalize()
            # Remove PKCS7 padding
            if decrypted:
                pad_len = decrypted[-1]
                if 0 < pad_len <= 16 and all(b == pad_len for b in decrypted[-pad_len:]):
                    decrypted = decrypted[:-pad_len]
            return decrypted
        except Exception as e:
            # If decryption fails, return raw data (might not be encrypted)
            return data

    if rep.get("base_url"):
        url = urljoin(effective_base, rep["base_url"])
        info["log"].append(f"{label}: descarga directa...")
        r = session.get(url, stream=True, timeout=120)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0 and downloaded % (1024 * 1024) < 256 * 1024:
                    info["log"].append(f"{label}: {downloaded // (1024*1024)}MB / {total // (1024*1024)}MB")
        return True

    if rep.get("segments"):
        segments = rep["segments"]
        info["log"].append(f"{label}: {len(segments)} segmentos")

        with open(dest_path, "wb") as f:
            # Init segment (also encrypted in MS Stream)
            if rep.get("init_url"):
                init_raw = rep["init_url"]
                init_url = urljoin(effective_base, init_raw)
                info["log"].append(f"  Init: {init_url[:100]}...")
                r = session.get(init_url, timeout=60)
                info["log"].append(f"  Init: HTTP {r.status_code} ({len(r.content)} bytes)")
                r.raise_for_status()
                decrypted_init = decrypt_segment(r.content)
                # Verify decryption worked
                if len(decrypted_init) >= 8:
                    box = decrypted_init[4:8]
                    info["log"].append(f"  Init box: {box}")
                f.write(decrypted_init)

            retries = 0
            for i, seg_url in enumerate(segments):
                full_url = urljoin(effective_base, seg_url)

                # Log detallado del primer segmento
                if i == 0:
                    seg_domain = urlparse(full_url).hostname
                    info["log"].append(f"  Dominio segmentos: {seg_domain}")
                    # Verificar qué cookies se mandan realmente
                    from requests.utils import dict_from_cookiejar
                    prep = requests.Request("GET", full_url, headers=session.headers, cookies=session.cookies).prepare()
                    info["log"].append(f"  Cookie header enviado: {prep.headers.get('Cookie', 'NINGUNA')[:200]}...")

                r = session.get(full_url, timeout=30)

                # On error, log details for first few segments
                if r.status_code != 200 and i < 3:
                    error_body = ""
                    try:
                        error_body = r.json().get("error", {}).get("message", r.text[:200])
                    except Exception:
                        error_body = r.text[:200]
                    info["log"].append(f"  Segmento {i+1}: HTTP {r.status_code} - {error_body}")
                
                # On 401, try without token headers (URL might have its own auth params)
                if r.status_code == 401:
                    if "x-spopactoken" in session.headers or "Authorization" in session.headers:
                        info["log"].append(f"  Auth rechazada en segmento {i+1}, reintentando sin token...")
                        session.headers.pop("x-spopactoken", None)
                        session.headers.pop("Authorization", None)
                        r = session.get(full_url, timeout=60)
                    
                    if r.status_code == 401 and retries < 2:
                        retries += 1
                        info["log"].append(f"  Reintentando segmento {i+1} ({retries}/2)...")
                        time.sleep(1)
                        r = session.get(full_url, timeout=60)
                        if r.status_code != 200:
                            info["log"].append(f"  Reintento {retries}: HTTP {r.status_code}")

                # On 409 Conflict or 429 Too Many Requests, wait and retry
                if r.status_code in (409, 429, 503):
                    for attempt in range(3):
                        wait = (attempt + 1) * 3
                        info["log"].append(f"  HTTP {r.status_code} en segmento {i+1}, esperando {wait}s...")
                        time.sleep(wait)
                        r = session.get(full_url, timeout=60)
                        if r.status_code == 200:
                            break
                r.raise_for_status()
                f.write(decrypt_segment(r.content))

                pct = ((i + 1) / len(segments)) * 100
                if (i + 1) % max(1, len(segments) // 20) == 0 or i == len(segments) - 1:
                    info["log"].append(f"{label}: {i+1}/{len(segments)} ({pct:.0f}%)")
                # Actualizar progreso global
                if "video" in label.lower():
                    info["progress"] = pct * 0.5
                else:
                    info["progress"] = 50 + pct * 0.4
        return True

    info["log"].append(f"{label}: no se encontró URL ni segmentos")
    return False


def decrypt_file(encrypted_path, decrypted_path, key_bytes, iv_bytes, info, label):
    """Descifra un archivo AES-128-CBC usando ffmpeg."""
    ffmpeg = get_ffmpeg_exe("ffmpeg")
    key_hex = key_bytes.hex()
    iv_hex = iv_bytes.hex()
    cmd = [
        ffmpeg, "-y",
        "-decryption_key", key_hex,
        "-decryption_iv", iv_hex,
        "-i", encrypted_path,
        "-c", "copy",
        decrypted_path,
    ]
    info["log"].append(f"Descifrando {label}...")
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=creation_flags, timeout=300)
    if result.returncode != 0:
        info["log"].append(f"Descifrado {label} falló, intentando con openssl...")
        # Fallback: descifrar con Python
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv_bytes), backend=default_backend())
            decryptor = cipher.decryptor()
            with open(encrypted_path, "rb") as fin, open(decrypted_path, "wb") as fout:
                while True:
                    chunk = fin.read(64 * 1024)
                    if not chunk:
                        break
                    fout.write(decryptor.update(chunk))
                fout.write(decryptor.finalize())
            info["log"].append(f"Descifrado {label} con Python OK")
            return True
        except ImportError:
            info["log"].append("Necesitas: pip install cryptography")
            return False
        except Exception as e:
            info["log"].append(f"Error descifrado: {e}")
            return False
    info["log"].append(f"Descifrado {label} OK")
    return True


def merge_av(video_path, audio_path, output_path, info):
    """Merge video + audio con ffmpeg (archivos locales, sin auth)."""
    ffmpeg = get_ffmpeg_exe("ffmpeg")
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    info["log"].append("Mezclando video + audio...")
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=creation_flags, timeout=300)
    if result.returncode != 0:
        info["log"].append(f"ffmpeg error: {result.stderr[-500:]}")
        return False
    info["log"].append("Merge completado.")
    return True


def run_download(download_id, manifest_url, quality, token=None, cookies_path=None, origin_override=None, raw_cookies=None, referer=None):
    """Descarga: fetch manifest → parse DASH → download streams → merge."""
    info = downloads[download_id]
    filename = f"{download_id}.mp4"
    dest = os.path.join(DOWNLOADS_DIR, filename)
    info["filename"] = filename

    try:
        info["status"] = "downloading"
        info["log"].append("Iniciando descarga...")
        info["log"].append(f"URL: {len(manifest_url)} chars")
        info["log"].append(f"Token: {'sí' if token else 'no'}")
        info["log"].append(f"Cookies: {'sí' if cookies_path else 'no'}")

        session = create_session(token, cookies_path, origin_override, raw_cookies, referer)
        info["log"].append(f"Cookies en sesión: {len(session.cookies)}")
        if origin_override:
            info["log"].append(f"Origin override: {origin_override}")
        if raw_cookies:
            info["log"].append(f"Raw cookies: sí ({len(parse_raw_cookies(raw_cookies, ''))} cookies)")

        # Detectar dominio del manifest
        manifest_domain = urlparse(manifest_url).hostname
        info["log"].append(f"Dominio manifest: {manifest_domain}")

        # 1. Descargar manifest — usar URL completa tal cual
        info["log"].append("Descargando manifest DASH...")
        r = session.get(manifest_url, timeout=30)
        info["log"].append(f"Manifest: HTTP {r.status_code}")

        if r.status_code != 200:
            info["status"] = "error"
            if r.status_code == 401:
                info["log"].append("ERROR 401: Falta token de autenticación.")
                info["log"].append("")
                info["log"].append("Pasos para obtener el token:")
                info["log"].append("1. F12 → Network → filtrar 'videomanifest'")
                info["log"].append("2. Reproduce el video para que aparezca la request")
                info["log"].append("3. Click en la request → pestaña Headers")
                info["log"].append("4. En 'Request Headers' busca 'Authorization'")
                info["log"].append("5. Copia el valor (empieza con 'Bearer eyJ...')")
                info["log"].append("6. Pégalo en el campo 'Authorization Header'")
                info["log"].append("")
                info["log"].append("Tanto la URL como el token expiran rápido.")
                info["log"].append("Copia ambos y pégalos rápido.")
            else:
                info["log"].append(f"Error HTTP {r.status_code}")
            info["log"].append(f"Response: {r.text[:300]}")
            return
        
        manifest_text = r.text

        manifest_text = r.text
        info["log"].append(f"Manifest: {len(manifest_text)} bytes")

        # Guardar manifest para debug
        manifest_debug = os.path.join(DOWNLOADS_DIR, f"{download_id}_manifest.xml")
        with open(manifest_debug, "w", encoding="utf-8") as mf:
            mf.write(manifest_text)

        # 2. Parsear DASH
        videos, audios, encryption = parse_dash_manifest(manifest_text)
        info["log"].append(f"Streams: {len(videos)} video, {len(audios)} audio")
        if encryption:
            info["log"].append(f"Encriptación: AES-128-CBC (IV: {encryption['iv'][:20]}...)")

        for v in videos:
            info["log"].append(f"  Video: {v['width']}x{v['height']} @ {v['bandwidth']//1000}kbps")
        for a in audios:
            info["log"].append(f"  Audio: {a['codecs']} @ {a['bandwidth']//1000}kbps")

        if not videos:
            info["status"] = "error"
            info["log"].append("No se encontraron streams de video.")
            info["log"].append(f"Manifest preview: {manifest_text[:500]}")
            return

        # 3. Seleccionar calidad
        video_rep = select_rep(videos, quality)
        audio_rep = select_rep(audios, "best") if audios else None
        info["log"].append(f"→ Video: {video_rep['width']}x{video_rep['height']}")
        if audio_rep:
            info["log"].append(f"→ Audio: {audio_rep['codecs']} @ {audio_rep['bandwidth']//1000}kbps")

        # Base URL para segmentos relativos
        base_url = manifest_url.rsplit("/", 1)[0] + "/" if "/" in manifest_url else manifest_url

        # 4. Descargar (con descifrado al vuelo si hay encriptación)
        decrypt_key = None
        decrypt_iv = None
        if encryption:
            info["log"].append("Descargando clave de descifrado...")
            key_r = session.get(encryption["key_url"], timeout=30)
            if key_r.status_code != 200:
                session.headers.pop("x-spopactoken", None)
                session.headers.pop("Authorization", None)
                key_r = session.get(encryption["key_url"], timeout=30)
            if key_r.status_code == 200:
                decrypt_key = key_r.content
                iv_hex = encryption["iv"].replace("0x", "").replace("0X", "")
                decrypt_iv = bytes.fromhex(iv_hex)
                info["log"].append(f"Clave: {len(decrypt_key)} bytes, IV: {len(decrypt_iv)} bytes")
            else:
                info["log"].append(f"Clave no disponible: HTTP {key_r.status_code}, descargando sin descifrar")

        # Quitar tokens para los segmentos — solo se autentican con cookies
        session.headers.pop("x-spopactoken", None)
        session.headers.pop("Authorization", None)
        # Verificar que las cookies de auth están presentes
        cookie_names = [c.name for c in session.cookies]
        has_fedauth = "FedAuth" in cookie_names
        has_rtfa = "rtFa" in cookie_names
        info["log"].append(f"Cookies auth: FedAuth={'sí' if has_fedauth else 'NO'}, rtFa={'sí' if has_rtfa else 'NO'}")
        if not has_fedauth:
            info["log"].append("⚠ Falta FedAuth — probá pegando las cookies raw del navegador")
        info["log"].append("Auth segmentos: solo cookies (sin token)")

        if audio_rep:
            video_tmp = dest + ".v.tmp"
            audio_tmp = dest + ".a.tmp"

            if not download_representation(session, video_rep, base_url, video_tmp, info, "Video", decrypt_key, decrypt_iv):
                info["status"] = "error"
                return
            if not download_representation(session, audio_rep, base_url, audio_tmp, info, "Audio", decrypt_key, decrypt_iv):
                info["status"] = "error"
                return

            info["progress"] = 90
            if not merge_av(video_tmp, audio_tmp, dest, info):
                info["status"] = "error"
                return

            for tmp in [video_tmp, audio_tmp]:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        else:
            if not download_representation(session, video_rep, base_url, dest, info, "Video", decrypt_key, decrypt_iv):
                info["status"] = "error"
                return

        info["status"] = "done"
        info["progress"] = 100
        size_mb = os.path.getsize(dest) / (1024 * 1024)
        info["log"].append(f"Descarga completada: {size_mb:.1f} MB")

    except requests.exceptions.HTTPError as e:
        info["status"] = "error"
        info["log"].append(f"HTTP Error: {e}")
    except Exception as e:
        info["status"] = "error"
        info["log"].append(f"Error: {e}")


# --- Flask routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload-cookies", methods=["POST"])
def upload_cookies():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    cookies_id = str(uuid.uuid4())[:8]
    path = os.path.join(COOKIES_DIR, f"{cookies_id}.txt")
    f.save(path)
    return jsonify({"cookies_id": cookies_id})


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    quality = data.get("quality", "best")
    token = data.get("token", "").strip()
    cookies_id = data.get("cookies_id", "").strip()
    origin_override = data.get("origin", "").strip()
    raw_cookies = data.get("raw_cookies", "").strip()
    referer = data.get("referer", "").strip()

    if not url:
        return jsonify({"error": "URL requerida"}), 400

    cookies_path = None
    if cookies_id:
        cookies_path = os.path.join(COOKIES_DIR, f"{cookies_id}.txt")
        if not os.path.isfile(cookies_path):
            cookies_path = None

    download_id = str(uuid.uuid4())[:8]
    downloads[download_id] = {
        "status": "starting",
        "progress": 0,
        "filename": None,
        "log": [],
    }

    thread = threading.Thread(
        target=run_download,
        args=(download_id, url, quality, token or None, cookies_path, origin_override or None, raw_cookies or None, referer or None),
        daemon=True,
    )
    thread.start()
    return jsonify({"id": download_id})


@app.route("/api/progress/<download_id>")
def progress(download_id):
    def generate():
        last_log_idx = 0
        while True:
            info = downloads.get(download_id)
            if not info:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                break
            new_logs = info["log"][last_log_idx:]
            last_log_idx = len(info["log"])
            yield f"data: {json.dumps({'status': info['status'], 'progress': info['progress'], 'logs': new_logs, 'filename': info['filename']})}\n\n"
            if info["status"] in ("done", "error"):
                break
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/file/<download_id>")
def download_file(download_id):
    info = downloads.get(download_id)
    if not info or info["status"] != "done":
        return jsonify({"error": "Archivo no disponible"}), 404
    filepath = os.path.join(DOWNLOADS_DIR, info["filename"])
    if not os.path.isfile(filepath):
        return jsonify({"error": "Archivo no encontrado en disco"}), 404
    return send_from_directory(DOWNLOADS_DIR, info["filename"], as_attachment=True, download_name="video.mp4")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print(f"ffmpeg: {get_ffmpeg_exe()}")
    print(f"Descargas en: {DOWNLOADS_DIR}")
    app.run(debug=True, port=5000)
