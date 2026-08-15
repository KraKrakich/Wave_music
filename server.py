#!/usr/bin/env python3
"""
WAVE musicdl backend
=====================

Небольшой локальный сервер, который оборачивает библиотеку `musicdl`
(https://github.com/CharlesPikachu/musicdl) и отдаёт фронтенду WAVE
(WAVE_Music3.html) JSON с треками + возможность стримить/скачивать
полное аудио (а не 30-сек превью, как у iTunes/Deezer).

Установка:
    pip install flask flask-cors musicdl

Запуск:
    python server.py
    # сервер поднимется на http://127.0.0.1:5057

Во фронтенде (WAVE_Music3.html) в настройках нужно один раз задать
адрес бэкенда — по умолчанию уже стоит http://127.0.0.1:5057, менять
не нужно, если ты запускаешь всё локально.

Эндпоинты:
    GET  /api/search?q=<запрос>&limit=<n>
         -> [{id, title, artist, cover, audio, duration, source}, ...]
         Поле `audio` уже указывает на /api/stream/<id> этого же сервера.

    GET  /api/stream/<id>
         -> стримит (и скачивает по требованию) реальный аудиофайл,
            резолвя его через musicdl "на лету" при первом обращении.

    GET  /api/health
         -> {"ok": true} — для проверки, что сервер жив.
"""

import io
import os
import tempfile
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_file, abort
from flask_cors import CORS

from musicdl import musicdl

app = Flask(__name__)
CORS(app)  # разрешаем запросы с file:// и с любого локального фронтенда

# ---------------------------------------------------------------------------
# Конфигурация источников musicdl.
# По умолчанию используем связку зарубежных/открытых источников, которые
# не требуют cookies/VIP-аккаунта. Можно дописать китайские источники
# (NeteaseMusicClient, QQMusicClient, KuwoMusicClient, ...), если нужно —
# им не требуется авторизация для обычного поиска/скачивания.
# ---------------------------------------------------------------------------
MUSIC_SOURCES = [
    "NeteaseMusicClient",
    "KuwoMusicClient",
    "QQMusicClient",
    "SoundCloudMusicClient",
    "AudiusMusicClient",
]

SEARCH_SIZE_PER_SOURCE = 8

# Кэш результатов поиска: id -> song_info (нужно для отложенного скачивания)
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60 * 30  # 30 минут

# Кэш уже скачанных файлов: id -> путь к временному файлу
_FILE_CACHE = {}
_TMP_DIR = tempfile.mkdtemp(prefix="wave_musicdl_")


def _make_client():
    return musicdl.MusicClient(
        music_sources=MUSIC_SOURCES,
        init_music_clients_cfg={
            src: {"search_size_per_source": SEARCH_SIZE_PER_SOURCE}
            for src in MUSIC_SOURCES
        },
    )


def _cleanup_cache():
    now = time.time()
    with _CACHE_LOCK:
        expired = [k for k, v in _CACHE.items() if now - v["ts"] > _CACHE_TTL]
        for k in expired:
            _CACHE.pop(k, None)
            fp = _FILE_CACHE.pop(k, None)
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass


def _song_to_track(song_info, source_name):
    """Приводим song_info musicdl к формату треков WAVE."""
    track_id = "md_" + uuid.uuid4().hex[:12]
    with _CACHE_LOCK:
        _CACHE[track_id] = {"song_info": song_info, "source": source_name, "ts": time.time()}

    title = song_info.get("songname") or song_info.get("name") or song_info.get("title") or "Unknown"
    artist = song_info.get("singername") or song_info.get("artist") or song_info.get("author") or "Unknown"
    cover = song_info.get("albumpic") or song_info.get("cover") or song_info.get("pic") or ""
    duration_ms = song_info.get("duration") or song_info.get("interval") or 0
    try:
        duration_ms = int(duration_ms)
    except (TypeError, ValueError):
        duration_ms = 0

    return {
        "id": track_id,
        "title": str(title)[:120],
        "artist": str(artist)[:80],
        "cover": cover or "https://picsum.photos/seed/md/500/500",
        "audio": f"/api/stream/{track_id}",
        "duration": _format_ms(duration_ms),
        "source": "musicdl:" + source_name,
    }


def _format_ms(ms):
    if not ms:
        return ""
    try:
        s = round(int(ms) / 1000) if ms > 100000 else int(ms)  # эвристика: мс vs сек
        m, r = divmod(s, 60)
        return f"{m}:{r:02d}"
    except Exception:
        return ""


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/search")
def search():
    query = (request.args.get("q") or "").strip()
    limit = int(request.args.get("limit") or 20)
    if not query:
        return jsonify([])

    _cleanup_cache()

    client = _make_client()
    try:
        results = client.search(keyword=query)
    except Exception as e:
        app.logger.exception("musicdl search failed")
        return jsonify({"error": str(e)}), 502

    tracks = []
    for source_name, song_infos in (results or {}).items():
        for song_info in song_infos:
            tracks.append(_song_to_track(song_info, source_name))
            if len(tracks) >= limit:
                break
        if len(tracks) >= limit:
            break

    return jsonify(tracks)


@app.route("/api/stream/<track_id>")
def stream(track_id):
    with _CACHE_LOCK:
        entry = _CACHE.get(track_id)

    if not entry:
        abort(404, "Unknown or expired track id — search again")

    # Уже скачивали раньше в этой сессии сервера?
    cached_path = _FILE_CACHE.get(track_id)
    if cached_path and os.path.exists(cached_path):
        return send_file(cached_path, mimetype="audio/mpeg", conditional=True)

    client = _make_client()
    work_dir = os.path.join(_TMP_DIR, track_id)
    os.makedirs(work_dir, exist_ok=True)

    try:
        client.download(song_infos=[entry["song_info"]])
    except Exception as e:
        app.logger.exception("musicdl download failed")
        abort(502, f"Download failed: {e}")

    # musicdl сохраняет файл в рабочую директорию источника — ищем
    # только что созданный аудиофайл.
    found = _find_downloaded_file(entry["source"])
    if not found:
        abort(502, "Downloaded file not found")

    _FILE_CACHE[track_id] = found
    return send_file(found, mimetype="audio/mpeg", conditional=True)


def _find_downloaded_file(source_name):
    """Ищем самый свежий аудиофайл в рабочей директории musicdl для
    данного источника (musicdl по умолчанию сохраняет в ./<ClientName>/)."""
    candidates_dirs = [source_name, os.path.join(os.getcwd(), source_name), "."]
    audio_ext = (".mp3", ".flac", ".m4a", ".wav", ".ogg")
    newest_path, newest_mtime = None, -1
    for d in candidates_dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(audio_ext):
                    fp = os.path.join(root, f)
                    mtime = os.path.getmtime(fp)
                    if mtime > newest_mtime:
                        newest_mtime, newest_path = mtime, fp
    return newest_path


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"WAVE musicdl backend: http://{host}:{port}")
    print("Sources:", ", ".join(MUSIC_SOURCES))
    app.run(host=host, port=port, debug=False, threaded=True)
