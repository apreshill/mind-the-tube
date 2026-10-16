# Mind the Tube

A Pixeltable app that turns a YouTube URL or an uploaded video into timestamped chapters. Inserting a video extracts a wav, splits it on silence, transcribes each chunk with local Whisper, and rolls those chunks into one chapters value per video. Pixeltable Cloud hosts the database and the HTTP API. There is no browser page: the API key must not live in a page.

## Pipeline

`source` is an optional string. `upload` is an optional video file. `resolve_video` uses the upload when it is present. Otherwise a `youtube.com` or `youtu.be` URL is downloaded with `yt-dlp` as a single mp4, and any other string is treated as a media path the worker can see. A path on another computer is not visible to the Cloud worker; send the file as `upload`, or insert from a machine that has the file (Cloud uploads a local path).

`video` is a computed column with `stored=False`. An insert only materializes stored columns, so that download is folded into `audio` and the mp4 is not kept. `audio` stays stored, because the segments view reads that wav when the row is inserted. Chunks come from `audio_splitter`. What stays stored is the wav, each chunk's start, end, Whisper result, and text.

```python
audio_splitter(
    Videos.audio,
    duration=30.0,
    overlap=0.0,
    min_silence_len=0.3,
    silence_thresh=-40.0,
    trim_leading_silence=True,
    min_segment_duration=1.0,
)
```

`audio_splitter` stores each chunk file, and it has no `seek()`, so `unstored_cols` cannot be set on it. The view in `app.py` subclasses that iterator, adds `seek()`, and marks `audio_segment` unstored. That subclass is local to this app. It is not a Pixeltable function. See [Iterators](https://docs.pixeltable.com/platform/iterators).

`min_silence_len=0.3` snaps each cut to the latest silence at or before the 30 second budget. Overlap stays 0 so adjacent chapter text does not repeat. `trim_leading_silence` moves `segment_start` to the first audible packet.

Each chunk is `whisper.transcribe(..., model='base.en')` (local `openai-whisper`, no API key). Change `WHISPER_MODEL` in `app.py` to `small.en`, `medium.en`, or a multilingual model without `.en`.

The chapters value is one JSON object:

- `segments`: `{start, end, text}` in time order
- `youtube_chapters`: lines joined with newlines, `M:SS - text` (`H:MM:SS` once the start is an hour or more)

The first line is always `0:00 - ...`, because YouTube requires a chapter at the start. Silence trimming can push the first audible start past one second, so that displayed timestamp is pinned.

## Local schema

Python 3.11 or 3.12. From this directory, with the project virtualenv active:

```bash
pxt schema update app.py mind_the_tube
```

A Python insert of a local file:

```python
import pixeltable as pxt

videos = pxt.get_table('mind_the_tube.videos')
videos.insert([{'source': '/path/to/video.mp4'}])
```

## Read every transcript

From this directory, one command returns every video in the local catalog. `chapters` is computed on read, so name it in `--cols` or the CLI skips it. `-n` caps at 1000 rows.

```bash
pxt rows mind_the_tube/chapters --cols id,source,chapters -n 1000 --json
```

Each object is one video. `chapters.segments` holds each chunk as `start`, `end`, and `text`. `chapters.youtube_chapters` is those lines as `M:SS - text`.

One video, by its id:

```bash
pxt get mind_the_tube/chapters VIDEO_ID --cols id,source,chapters --json
```

## Pixeltable Cloud

To host the API, sign in with `pxt login` (or `pxt new` for a trial) and add a second `[[tool.pixeltable.database]]` entry named with your own org, for example `pxt://your-org:main`. `requirements.txt` must be present so the image includes `pixeltable[serve]`, `openai-whisper`, and `yt-dlp`. Include `system_dependencies = ['ffmpeg']`. After `pxt db status` shows `AVAILABLE` for that URI:

```bash
pxt db update pxt://your-org:main
pxt schema update app.py pxt://your-org:main
pxt service update app.py pxt://your-org:main
pxt service list pxt://your-org:main
```

`pxt service list` prints the host. Callers send `X-api-key` from `pxt key create`. The key stays out of any page.

## HTTP

`POST /videos` accepts `source` as a form field and `upload` as a file. It runs in the background, because Whisper on a real video can take minutes. The response is a job id and `job_url`. Poll `job_url` until `status` is `done`. `result.id` is the video id (the job id is not).

```bash
curl -X POST "$HOST/videos" \
  -H "X-api-key: $PXT_API_KEY" \
  -F "source=https://www.youtube.com/watch?v=VIDEO_ID"

curl -X POST "$HOST/videos" \
  -H "X-api-key: $PXT_API_KEY" \
  -F "upload=@/path/to/video.mp4"
```

`GET /videos/chapters?video_id=...` returns that video's `id` and `chapters` (`segments` and `youtube_chapters`).

```bash
curl "$HOST/videos/chapters?video_id=VIDEO_ID" \
  -H "X-api-key: $PXT_API_KEY"
```
