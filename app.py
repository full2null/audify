import datetime
import io
import os
import tempfile

import streamlit as st
from mutagen.id3 import (
    APIC,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from PIL import Image
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

QUALITY_PRESETS = {
    "mp3": {"High": "0", "Medium": "2", "Low": "5"},
    "aac": {"High": "320", "Medium": "192", "Low": "128"},
}
CODEC_LABELS = {"mp3": "MP3", "aac": "AAC"}
CODEC_INFO = {"mp3": ("mp3", "mpeg"), "aac": ("m4a", "aac")}
_FILENAME_TABLE = str.maketrans(
    {
        "\\": "＼",
        "/": "／",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
)

META_KEYS = (
    "title",
    "artist",
    "album",
    "album_artist",
    "year",
    "track_number",
    "total_tracks",
    "disc_number",
    "total_discs",
    "genre",
)


def apply_metadata_mp3(filepath: str, fields: dict):
    audio = MP3(filepath)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    def _set(tag_cls, value):
        name = tag_cls.__name__
        tags.delall(name)
        if value:
            tags.add(tag_cls(encoding=3, text=value))

    _set(TIT2, fields["title"])

    artists = [a.strip() for a in fields["artist"].split(";") if a.strip()]
    tags.delall("TPE1")
    if artists:
        tags.add(TPE1(encoding=3, text=artists))
    tags.delall("TXXX:ARTISTS")
    if artists:
        tags.add(TXXX(encoding=3, desc="ARTISTS", text=artists))

    _set(TALB, fields["album"])
    _set(TPE2, fields["album_artist"])
    _set(TDRC, fields["year"])
    _set(TCON, fields["genre"])

    track = fields["track_number"]
    if fields["total_tracks"] and track:
        track = f"{track}/{fields['total_tracks']}"
    _set(TRCK, track)

    disc = fields["disc_number"]
    if fields["total_discs"] and disc:
        disc = f"{disc}/{fields['total_discs']}"
    _set(TPOS, disc)

    if fields["cover"]:
        tags.delall("APIC")
        mime_type, cover_data = fields["cover"]
        tags.add(APIC(encoding=3, mime=mime_type, type=3, data=cover_data))

    audio.save(v2_version=3)


def apply_metadata_m4a(filepath: str, fields: dict):
    audio = MP4(filepath)

    def _set(key: str, value: str):
        if value:
            audio[key] = [value]
        elif key in audio:
            del audio[key]

    _set("©nam", fields["title"])

    artists = [a.strip() for a in fields["artist"].split(";") if a.strip()]
    if artists:
        audio["©ART"] = ["; ".join(artists)]
    elif "©ART" in audio:
        del audio["©ART"]
    freeform_key = "----:com.apple.iTunes:ARTISTS"
    if artists:
        audio[freeform_key] = [MP4FreeForm(a.encode("utf-8")) for a in artists]
    elif freeform_key in audio:
        del audio[freeform_key]

    _set("©alb", fields["album"])
    _set("aART", fields["album_artist"])
    _set("©day", fields["year"])
    _set("©gen", fields["genre"])

    if fields["track_number"]:
        try:
            tn = int(fields["track_number"])
            tt = int(fields["total_tracks"]) if fields["total_tracks"] else 0
            audio["trkn"] = [(tn, tt)]
        except ValueError:
            pass
    elif "trkn" in audio:
        del audio["trkn"]

    if fields["disc_number"]:
        try:
            dn = int(fields["disc_number"])
            dt = int(fields["total_discs"]) if fields["total_discs"] else 0
            audio["disk"] = [(dn, dt)]
        except ValueError:
            pass
    elif "disk" in audio:
        del audio["disk"]

    if fields["cover"]:
        mime_type, cover_data = fields["cover"]
        fmt = MP4Cover.FORMAT_PNG if mime_type == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover_data, imageformat=fmt)]

    audio.save()


_APPLY_METADATA = {"mp3": apply_metadata_mp3, "aac": apply_metadata_m4a}


def apply_metadata(filepath: str, fields: dict, codec: str):
    _APPLY_METADATA[codec](filepath, fields)


def crop_to_square(image_data: bytes):
    with Image.open(io.BytesIO(image_data)) as img:
        img = img.convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
    return "image/jpeg", buf.getvalue()


def _parse_release_date(info: dict) -> datetime.date | None:
    date_str = (info.get("release_date") or "") or (info.get("upload_date") or "")
    if len(date_str) == 8:
        try:
            return datetime.date(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:])
            )
        except ValueError:
            pass
    release_year = info.get("release_year")
    if release_year:
        try:
            return datetime.date(int(release_year), 1, 1)
        except ValueError:
            pass
    return None


def on_download():
    try:
        os.remove(os.path.join(state["temp_dir"].name, state["current_filename"]))
    except FileNotFoundError:
        pass


def on_url_change():
    state["extracted_info"] = None
    state["meta_initialized"] = False


st.set_page_config(
    page_title="Audify",
    menu_items={
        "About": "Web app for extracting and tagging audio from URLs",
    },
)

state = st.session_state

_STATE_DEFAULTS = {
    "codec": None,
    "extracted_info": None,
    "quality": None,
    "meta_initialized": False,
    "current_filename": None,
}
for _k, _v in _STATE_DEFAULTS.items():
    if _k not in state:
        state[_k] = _v

if "temp_dir" not in state:
    state["temp_dir"] = tempfile.TemporaryDirectory()

if "ydl_options" not in state:
    state["ydl_options"] = {
        "format": "ba/b",
        "outtmpl": {"default": os.path.join(state["temp_dir"].name, "audio.%(ext)s")},
        "js_runtimes": {"deno": {"path": None}, "node": {"path": None}},
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": None,
                "preferredquality": None,
                "nopostoverwrites": False,
            },
        ],
        "quiet": False,
        "verbose": True,
        "writethumbnail": True,
    }

st.title("Audify", anchor=False)
st.markdown(
    """
    <style>
    [data-testid="stImage"] img {
        aspect-ratio: 1 / 1;
        object-fit: cover;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

url = st.text_input(
    "URL",
    on_change=on_url_change,
    placeholder="Enter URL",
    label_visibility="collapsed",
)

if url:
    ydl_options = state["ydl_options"]

    try:
        if state["extracted_info"] is None:
            with YoutubeDL(ydl_options) as ydl:
                state["extracted_info"] = ydl.extract_info(url, download=False)

        info = state["extracted_info"]
        thumbnail_url: str = info["thumbnail"]
        title: str = info["title"]
        uploader: str = info["uploader"]

        if not state["meta_initialized"]:
            artists_list = info.get("artists") or []
            if artists_list:
                artist = "; ".join(artists_list)
            else:
                artist = (
                    info.get("artist")
                    or info.get("creator")
                    or info.get("uploader", "")
                )
            tags = info.get("tags") or []
            state["meta_year"] = _parse_release_date(info)

            state["meta_title"] = info.get("track") or title
            state["meta_artist"] = artist
            state["meta_album"] = ""
            state["meta_album_artist"] = ""
            state["meta_track_number"] = ""
            state["meta_total_tracks"] = ""
            state["meta_disc_number"] = ""
            state["meta_total_discs"] = ""
            state["meta_genre"] = ""
            state["meta_initialized"] = True

        if state.get("uploaded_cover") is not None:
            st.image(state["uploaded_cover"], width="stretch")
        else:
            st.image(thumbnail_url, width="stretch")
        st.file_uploader(
            "Want to use a different album cover?",
            type=["jpg", "jpeg", "png"],
            key="uploaded_cover",
        )
        st.text_input("Title", key="meta_title")
        st.text_input(
            "Artist",
            key="meta_artist",
            help="Use ; to separate multiple artists",
        )

        col_left, col_right = st.columns(2)
        state["codec"] = col_left.radio(
            "Codec",
            ["mp3", "aac"],
            index=1,
            format_func=CODEC_LABELS.get,
            horizontal=True,
        )
        state["quality"] = col_right.radio(
            "Quality",
            ["High", "Medium", "Low"],
            index=1,
            horizontal=True,
        )

        with st.expander("Metadata"):
            col_a, col_b = st.columns(2)
            col_a.text_input("Album", key="meta_album")
            col_b.text_input("Album Artist", key="meta_album_artist")

            col_c, col_d = st.columns(2)
            col_c.date_input("Date", key="meta_year", format="YYYY-MM-DD")
            col_d.text_input("Genre", key="meta_genre")

            col_e, col_f, col_g, col_h = st.columns(4)
            col_e.text_input("Track #", key="meta_track_number")
            col_f.text_input("Total Tracks", key="meta_total_tracks")
            col_g.text_input("Disc #", key="meta_disc_number")
            col_h.text_input("Total Discs", key="meta_total_discs")

        placeholder = st.empty()

        if placeholder.button("Extract"):
            if not state["meta_title"].strip():
                st.error("Title is required.")
            else:
                with st.spinner("Extracting..."):
                    placeholder.button(
                        "Extract",
                        key="extracting",
                        disabled=True,
                    )

                    ydl_options["postprocessors"][0]["preferredcodec"] = state["codec"]
                    ydl_options["postprocessors"][0]["preferredquality"] = (
                        QUALITY_PRESETS[state["codec"]][state["quality"]]
                    )

                    with YoutubeDL(ydl_options) as ydl:
                        ydl.download([url])

                extension, mime = CODEC_INFO[state["codec"]]

                filename = f"{state['meta_title']}.{extension}"

                filename = filename.translate(_FILENAME_TABLE)

                state["current_filename"] = filename
                src = os.path.join(state["temp_dir"].name, f"audio.{extension}")
                filepath = os.path.join(state["temp_dir"].name, filename)

                thumb_path = None
                for fname in os.listdir(state["temp_dir"].name):
                    fpath = os.path.join(state["temp_dir"].name, fname)
                    if (
                        fname.startswith("audio.")
                        and not fname.endswith(f".{extension}")
                        and os.path.isfile(fpath)
                    ):
                        thumb_path = fpath
                        break

                if state.get("uploaded_cover") is not None:
                    cover = crop_to_square(state["uploaded_cover"].read())
                    if thumb_path:
                        os.remove(thumb_path)
                elif thumb_path is not None:
                    with open(thumb_path, "rb") as tf:
                        cover = crop_to_square(tf.read())
                    os.remove(thumb_path)
                else:
                    cover = None

                fields = {k: state.get(f"meta_{k}", "") for k in META_KEYS}
                year_val = fields.get("year")
                if isinstance(year_val, datetime.date):
                    fields["year"] = year_val.strftime("%Y-%m-%d")
                elif year_val is None:
                    fields["year"] = ""
                fields["cover"] = cover
                apply_metadata(src, fields, state["codec"])
                os.replace(src, filepath)

                with open(filepath, "rb") as f:
                    placeholder.download_button(
                        "Download",
                        data=f,
                        file_name=filename,
                        mime=f"audio/{mime}",
                        on_click=on_download,
                    )
    except DownloadError as e:
        if "not a valid URL" in e.msg:
            st.error(f"'{url}' is not a valid URL.")
        else:
            st.error(e.msg)
    except Exception as e:
        st.exception(e)
