# MS Stream Video Downloader

Descarga videos de Microsoft Stream/Teams/SharePoint usando la URL del videomanifest.

## Requisitos

- Python 3.8+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp): `pip install yt-dlp`
- [ffmpeg](https://ffmpeg.org/download.html): debe estar en el PATH

## Uso

```bash
pip install yt-dlp
python stream_downloader.py
```

1. Pega la URL del videomanifest (la que empieza con `https://...mediap.svc.ms/transform/videomanifest?...`)
2. (Opcional) Selecciona un archivo `cookies.txt` si el video requiere autenticación
3. Elige dónde guardar el archivo
4. Click en "Descargar Video"

## Cómo obtener la URL del videomanifest

1. Abre el video en Microsoft Stream / Teams en el navegador
2. Abre DevTools (F12) → pestaña Network
3. Filtra por "videomanifest"
4. Copia la URL completa de la petición

## Cómo exportar cookies (si es necesario)

1. Instala la extensión [Get cookies.txt (LOCAL)](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) en Chrome
2. Navega a la página del video en Stream/SharePoint
3. Exporta las cookies como archivo `.txt`
4. Selecciona ese archivo en la app
