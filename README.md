# MS Stream Video Downloader

Aplicación web para descargar videos de Microsoft Stream, Teams y SharePoint. Funciona con cualquier tenant de Microsoft 365.

Parsea el manifest DASH del video, descarga los segmentos de video y audio por separado, descifra si es necesario (AES-128-CBC), y los une con ffmpeg.

## Requisitos

- Python 3.8+
- [ffmpeg](https://ffmpeg.org/download.html) en el PATH
  - Windows: `winget install Gyan.FFmpeg`
  - Linux/Mac: `apt install ffmpeg` / `brew install ffmpeg`

## Instalación

```bash
pip install -r requirements.txt
python app.py
```

Abrir `http://localhost:5000` en el navegador.

## Uso

### 1. Obtener la URL del videomanifest

1. Abrí el video en Stream/Teams/SharePoint en el navegador
2. F12 → pestaña Network
3. Filtrá por `videomanifest`
4. Reproducí el video
5. Click derecho en la request → Copy URL

### 2. Obtener el token

En la misma request del videomanifest en DevTools → Headers → Request Headers:
- Copiar el valor de `x-spopactoken` (empieza con `v1.eyJ...`)
- O el valor de `Authorization` (empieza con `Bearer eyJ...`)

### 3. Obtener las cookies

Las cookies autentican la descarga de los segmentos de video. Hay dos opciones:

**Opción A — Cookies raw (recomendado):**
1. En DevTools → Network, filtrá por `transcode`
2. Click en una request de segmento con status 200
3. En Headers → Request Headers, copiá el valor completo del header `cookie`
4. Pegalo en el campo "Cookies" de la app

**Opción B — Archivo cookies.txt:**
1. Instalá la extensión [Get cookies.txt](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) en Chrome
2. Navegá a la página del video
3. Exportá las cookies como `.txt`
4. Subí el archivo en la app

### 4. Descargar

1. Pegá la URL, el token y las cookies
2. Elegí la calidad
3. Click en "Descargar Video"

## Notas importantes

- La URL del manifest y el token expiran rápido (minutos). Copialos y pegalos rápido.
- Las cookies duran más que el token.
- Asegurate de exportar las cookies desde la misma sesión del navegador donde se reproduce el video.
- El campo "Origin" es opcional — se detecta automáticamente. Solo completalo si la descarga falla.

## Deploy con Docker

```bash
docker build -t ms-stream-downloader .
docker run -p 5000:5000 ms-stream-downloader
```

La imagen incluye ffmpeg, no necesitás instalarlo aparte.

## Deploy en la nube

El proyecto incluye configuración para:
- **Render** (`render.yaml`)
- **Railway** (`railway.toml`)

Ambos usan el Dockerfile que ya incluye ffmpeg.

## Stack

- Flask (backend)
- requests + cryptography (descarga y descifrado)
- ffmpeg (merge video + audio)
- gunicorn (producción)
