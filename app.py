"""Silence-split a video and roll the Whisper chunks into YouTube chapters."""

import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pixeltable as pxt
from pixeltable.functions.audio import audio_splitter
from pixeltable.functions.uuid import uuid7
from pixeltable.functions.video import extract_audio
from pixeltable.functions.whisper import transcribe
from pixeltable.serving import FastAPIRouter

# One-line model switch: small.en, medium.en, or a multilingual model without .en.
WHISPER_MODEL = 'base.en'

TableModel = pxt.model_base()


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    return host == 'youtu.be' or host.endswith('youtube.com') or host.endswith('youtube-nocookie.com')


def _download_youtube(url: str) -> str:
    import yt_dlp

    out_dir = Path(tempfile.mkdtemp(prefix='mind-the-tube-'))
    opts = {
        'format': 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best',
        'outtmpl': str(out_dir / '%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        for item in info.get('requested_downloads') or []:
            filepath = item.get('filepath')
            if filepath and Path(filepath).is_file():
                return filepath
        prepared = Path(ydl.prepare_filename(info))
        if prepared.is_file():
            return str(prepared)
        mp4 = prepared.with_suffix('.mp4')
        if mp4.is_file():
            return str(mp4)
        files = [path for path in out_dir.iterdir() if path.is_file()]
        if len(files) == 1:
            return str(files[0])
    raise RuntimeError(f'yt-dlp did not produce a video file for {url}')


@pxt.udf
def resolve_video(upload: pxt.Video | None, source: str | None) -> str:
    """Use an uploaded file when one was sent; otherwise a YouTube URL or a media path."""
    if upload:
        return str(upload)
    if source is None or not str(source).strip():
        raise ValueError('Provide a YouTube URL or a video file')
    source = str(source).strip()
    if _is_youtube(source):
        return _download_youtube(source)
    return source


def _format_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def _one_line(text: str | None) -> str:
    return ' '.join((text or '').split())


@pxt.uda(requires_order_by=True)
class chapter_rollup(pxt.Aggregator):
    """One {segments, youtube_chapters} object, in segment_start order.

    The first argument is the order key and is not passed to update, so start
    is supplied twice.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def update(self, start: float, end: float, text: str | None) -> None:
        if start is None:
            return
        self.rows.append(
            {
                'start': float(start),
                'end': None if end is None else float(end),
                'text': _one_line(text),
            }
        )

    def value(self) -> dict:
        segments = []
        lines = []
        for index, row in enumerate(self.rows):
            # YouTube requires a chapter at 0:00. Silence trimming can push the
            # first audible start well past one second.
            timestamp = '0:00' if index == 0 else _format_ts(row['start'])
            segments.append({'start': row['start'], 'end': row['end'], 'text': row['text']})
            lines.append(f'{timestamp} - {row["text"]}')
        return {'segments': segments, 'youtube_chapters': '\n'.join(lines)}


@pxt.udf
def first_row(rows: list | None) -> dict | None:
    """A scalar query stores a one-element list; the column should be the object."""
    if not rows:
        return None
    return rows[0]


# The built-in splitter stores audio_segment and has no seek(), so an unstored
# chunk column is not available on it. This subclass only adds seek(); the
# segment wav is recomputed when that column is read and is not kept.
@pxt.iterator(unstored_cols=['audio_segment'])
class silence_splitter(audio_splitter.decorated_callable):  # type: ignore[name-defined]
    def seek(self, pos: int, **kwargs: object) -> None:
        import av

        self.close()
        self.container = av.open(str(self.audio_path))
        self.in_stream = self.container.streams.audio[0]
        self._segments = self._iter_segments(size_mode=hasattr(self, 'max_size'))
        for _ in range(pos):
            next(self)


class Videos(TableModel, name='videos'):
    id = pxt.Column(value=uuid7(), primary_key=True)
    source: pxt.String | None
    upload: pxt.Video | None
    # The insert computes stored columns only. `video` is folded into `audio` and not kept.
    # `audio` stays stored: the segments view reads that wav when the row is inserted.
    video = pxt.Column(value=resolve_video(upload, source).astype(pxt.Video), stored=False)
    audio = extract_audio(video, format='wav')


class Segments(
    TableModel,
    name='segments',
    base=Videos,
    iterator=silence_splitter(
        Videos.audio,
        duration=30.0,
        overlap=0.0,
        min_silence_len=0.3,
        silence_thresh=-40.0,
        trim_leading_silence=True,
        min_segment_duration=1.0,
    ),
):
    transcription = transcribe(audio_segment, model=WHISPER_MODEL)
    text = transcription.text.astype(pxt.String | None)


@pxt.query(return_scalar=True)
def chapters_for(video_id: uuid.UUID):
    return Segments.where(Segments.id == video_id).select(
        chapter_rollup(
            Segments.segment_start,
            Segments.segment_start,
            Segments.segment_end,
            Segments.text,
        )
    )


class Chapters(TableModel, name='chapters', base=Videos):
    # Evaluated on read. A stored value is computed before Segments rows exist in the
    # same insert, and this column is not invalidated when those rows change.
    chapter_rows = pxt.Column(value=chapters_for(Videos.id), stored=False)
    chapters = pxt.Column(value=first_row(chapter_rows), stored=False)


@pxt.query
def video_chapters(video_id: uuid.UUID):
    return Chapters.where(Chapters.id == video_id).select(Chapters.id, Chapters.chapters)


router = FastAPIRouter(name='mind_the_tube')
router.add_insert_route(
    Videos,
    path='/videos',
    inputs=[Videos.source],
    uploadfile_inputs=[Videos.upload],
    outputs=[Videos.id, Videos.source],
    background=True,
)
router.add_query_route(path='/videos/chapters', query=video_chapters, one_row=True, method='get')
