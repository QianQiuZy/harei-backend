import csv
import json
from pathlib import Path
from typing import ClassVar, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field


class SourceStream(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    title: str
    platform: str
    url: str | None


class SourcePerformance(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    date: str
    stream: SourceStream
    clip_url: str | None = Field(alias="clipUrl")


class SourceSong(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    title: str
    artist: str
    artists: list[str]
    genre: str
    language: str
    work_type: str = Field(alias="workType")
    notes: str
    metadata_status: str = Field(alias="metadataStatus")
    performances: list[SourcePerformance]


class SourceCatalog(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    songs: list[SourceSong]


SOURCE: Final = "https://harei-songs.pages.dev/data/songs.json"
ROOT: Final = Path(__file__).resolve().parents[1] / "data"

with httpx.Client(timeout=60) as client:
    response = client.get(SOURCE, headers={"User-Agent": "harei-backend-seed-generator/1.0"})
    _ = response.raise_for_status()
    catalog = SourceCatalog.model_validate_json(response.content)

ROOT.mkdir(exist_ok=True)
with (ROOT / "songs.csv").open("w", encoding="utf-8", newline="\n") as stream:
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "source_key",
            "title",
            "artist",
            "artists_json",
            "genre",
            "language",
            "work_type",
            "notes",
            "metadata_status",
            "status",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for song in catalog.songs:
        writer.writerow(
            {
                "source_key": song.id,
                "title": song.title,
                "artist": song.artist,
                "artists_json": json.dumps(song.artists, ensure_ascii=False),
                "genre": song.genre,
                "language": song.language,
                "work_type": song.work_type,
                "notes": song.notes,
                "metadata_status": song.metadata_status,
                "status": "active",
            }
        )

with (ROOT / "song_performances.csv").open("w", encoding="utf-8", newline="\n") as stream:
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "source_key",
            "song_source_key",
            "performed_on",
            "platform",
            "stream_id",
            "stream_title",
            "stream_url",
            "clip_url",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for song in catalog.songs:
        for performance in song.performances:
            writer.writerow(
                {
                    "source_key": performance.id,
                    "song_source_key": song.id,
                    "performed_on": performance.date,
                    "platform": performance.stream.platform,
                    "stream_id": performance.stream.id,
                    "stream_title": performance.stream.title,
                    "stream_url": performance.stream.url,
                    "clip_url": performance.clip_url,
                }
            )
