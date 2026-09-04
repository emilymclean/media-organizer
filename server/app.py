import os
import threading
from enum import Enum
from time import sleep
from typing import Annotated

import backoff
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from pydantic import BaseModel, ValidationError, AnyHttpUrl, AfterValidator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from media_organizer.media_organizer import mega_login, TVDBProvider, MediaRequest, fetch, MetadataProvider


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
migrate = Migrate()


def validate_host(url: AnyHttpUrl) -> AnyHttpUrl:
    if url.host != "mega.nz":
        raise ValueError(f"Host '{url.host}' is not allowed. Must be mega.nz")
    return url


def create_app():
    app = Flask(__name__)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URI")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    if os.environ.get("MOVIE_LIBRARY_ROOT"):
        app.config["MOVIE_LIBRARY_ROOT"] = os.environ.get("MOVIE_LIBRARY_ROOT")
    elif os.environ.get("LIBRARY_ROOT"):
        app.config["MOVIE_LIBRARY_ROOT"] = os.environ.get("LIBRARY_ROOT")
    else:
        raise Exception("MOVIE_LIBRARY_ROOT or LIBRARY_ROOT environment variable must be set")

    if os.environ.get("SHOW_LIBRARY_ROOT"):
        app.config["SHOW_LIBRARY_ROOT"] = os.environ.get("SHOW_LIBRARY_ROOT")
    elif os.environ.get("LIBRARY_ROOT"):
        app.config["SHOW_LIBRARY_ROOT"] = os.environ.get("LIBRARY_ROOT")
    else:
        raise Exception("SHOW_LIBRARY_ROOT or LIBRARY_ROOT environment variable must be set")

    if not os.environ.get("TVDB_API_KEY"):
        raise Exception("TVDB_API_KEY environment variable must be set")

    app.config["TVDB_API_KEY"] = os.environ.get("TVDB_API_KEY")

    app.config["MEGA_EMAIL"] = os.environ.get("MEGA_EMAIL")
    app.config["MEGA_PASSWORD"] = os.environ.get("MEGA_PASSWORD")

    db.init_app(app)
    migrate.init_app(app, db)

    return app


app = create_app()


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    SUCCESS = "success"
    FAILED = "failed"


class QueuedDownload(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(nullable=False)
    tvdbid: Mapped[str] = mapped_column(nullable=False)
    mega_url: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(default=DownloadStatus.QUEUED)


class DownloadMode(str, Enum):
    MOVIE = "m"
    SHOW = "s"


class CreateQueuedDownloadRequest(BaseModel):
    mode: DownloadMode
    mega_url: Annotated[AnyHttpUrl, AfterValidator(validate_host)]
    tvdbid: str


@app.post("/api/queue")
def queue():
    try:
        data = CreateQueuedDownloadRequest(**request.json).model_dump()
    except ValidationError as error:
        return jsonify(error.errors()), 400

    with db.session.begin():
        db.session.add(QueuedDownload(
            mode=data["mode"],
            tvdbid=data["tvdbid"],
            mega_url=str(data["mega_url"])
        ))

    return jsonify({"message": "Download queued successfully"}), 201


@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def download_media(provider: MetadataProvider, download: QueuedDownload):
    candidate = MediaRequest(download.mode, download.tvdbid, [download.mega_url])

    if download.mode == 'm':
        library_path = app.config["MOVIE_LIBRARY_PATH"]
    else:
        library_path = app.config["SHOW_LIBRARY_PATH"]

    fetch(candidate, provider, library_path)


# I know a task queue like celery would be better
def background_downloader():
    if app.config.get("MEGA_EMAIL") and app.config.get("MEGA_PASSWORD"):
        mega_login(app.config["MEGA_EMAIL"], app.config["MEGA_PASSWORD"])

    provider = TVDBProvider(api_key=app.config["TVDB_API_KEY"], pin=None)
    while True:
        with app.app_context():
            with db.session.begin():
                download = db.session.query(QueuedDownload).filter(
                    QueuedDownload.status == DownloadStatus.QUEUED
                ).first()
                if not download:
                    print(f"No candidates")
                    sleep(60)
                    continue

                print(f"Attempting download of {download.tvdbid}")

                try:
                    download_media(provider, download)
                    download.status = DownloadStatus.SUCCESS
                except Exception as e:
                    print(f"Failed to download {download.tvdbid}: {e}")
                    download.status = DownloadStatus.FAILED

                db.session.commit()
                print(f"Downloaded {download.tvdbid}")


with app.app_context():
    db.create_all()

    thread = threading.Thread(target=background_downloader, daemon=True)
    thread.start()


