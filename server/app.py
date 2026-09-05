import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from time import sleep
from typing import Annotated

import backoff
from flask import Flask, request, jsonify, render_template
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
    if url.scheme != "https":
        raise ValueError(f"Scheme '{url.scheme}' is not allowed. Must be https")

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
    ACTIVE = "active"
    QUEUED = "queued"
    SUCCESS = "success"
    FAILED = "failed"


class QueuedDownload(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(nullable=False)
    tvdb_id: Mapped[str] = mapped_column(nullable=False)
    mega_url: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(default=DownloadStatus.QUEUED)
    order: Mapped[int] = mapped_column(default=0)
    last_updated: Mapped[datetime] = mapped_column(default=datetime.now(timezone.utc))


class DownloadMode(str, Enum):
    MOVIE = "m"
    SHOW = "s"


class CreateQueuedDownloadRequest(BaseModel):
    mode: DownloadMode
    mega_url: Annotated[AnyHttpUrl, AfterValidator(validate_host)]
    tvdb_id: str


@app.post("/api/download")
def queue():
    try:
        data = CreateQueuedDownloadRequest(**request.json).model_dump()
    except ValidationError as error:
        return jsonify(error.errors()), 400

    with db.session.begin():
        db.session.add(QueuedDownload(
            mode=data["mode"],
            tvdb_id=data["tvdb_id"],
            mega_url=str(data["mega_url"])
        ))

    return jsonify({"message": "Download queued successfully"}), 201


@dataclass
class QueuedDownloadModel:
    id: int
    mode: DownloadMode
    tvdb_id: str
    mega_url: str
    order: str
    status: DownloadStatus


@dataclass
class GetQueuedDownloadsResponse:
    downloads: list[QueuedDownloadModel]


@app.get("/api/downloads")
def get_queued_downloads():
    with db.session.begin():
        downloads = db.session.query(QueuedDownload).filter(
            QueuedDownload.status != DownloadStatus.SUCCESS
        ).order_by(
            QueuedDownload.status != DownloadStatus.ACTIVE, QueuedDownload.last_updated.desc(), QueuedDownload.id.asc()
        ).limit(
            500
        ).all()

    out = list(map(lambda x: QueuedDownloadModel(
        id=x.id,
        mode=x.mode,
        tvdb_id=x.tvdb_id,
        mega_url=x.mega_url,
        order=x.order,
        status=x.status
    ), downloads))

    return jsonify(GetQueuedDownloadsResponse(downloads=out))


@app.put("/api/download/<int:download_id>/retry")
def retry_download(download_id: int):
    with db.session.begin():
        download = db.session.query(QueuedDownload).filter_by(id=download_id).first()
        if not download:
            return jsonify({}), 404

        download.status = DownloadStatus.QUEUED
        download.last_updated = datetime.now()

        return jsonify({"message": "Download queued successfully"})


@app.delete("/api/download/<int:download_id>")
def delete_download(download_id: int):
    with db.session.begin():
        download = db.session.query(QueuedDownload).filter_by(id=download_id).first()
        if not download:
            return jsonify({}), 404

        if download.status == DownloadStatus.ACTIVE:
            return jsonify({"message": "Download cannot be deleted while it is active"}), 400

        db.session.delete(download)

        return jsonify({"message": "Download deleted successfully"})


@app.route('/')
def index():
    return render_template('index.html')


@backoff.on_exception(backoff.expo, Exception, max_tries=5)
def download_media(provider: MetadataProvider, download: QueuedDownload):
    with app.app_context():
        candidate = MediaRequest(download.mode, download.tvdb_id, [download.mega_url])

        if download.mode == 'm':
            library_path = app.config["MOVIE_LIBRARY_ROOT"]
        else:
            library_path = app.config["SHOW_LIBRARY_ROOT"]

        try:
            fetch(candidate, provider, library_path)
        except Exception as e:
            print(f"Failed to download {download.tvdb_id}: {e}")
            raise e


# I know a task queue like celery would be better
def background_downloader():
    provider = TVDBProvider(api_key=app.config["TVDB_API_KEY"], pin=None)
    while True:
        with app.app_context():
            with db.session.begin():
                download = db.session.query(QueuedDownload).filter(
                    QueuedDownload.status == DownloadStatus.QUEUED or
                    QueuedDownload.status == DownloadStatus.ACTIVE
                ).order_by(
                    QueuedDownload.status != DownloadStatus.ACTIVE, QueuedDownload.order.desc(), QueuedDownload.id.asc()
                ).first()
                if not download:
                    sleep(60)
                    continue

                print(f"Attempting download of {download.tvdb_id}")

                download.status = DownloadStatus.ACTIVE
                download.last_updated = datetime.now(timezone.utc)
                db.session.commit()

            try:
                download_media(provider, download)
                print(f"Downloaded {download.tvdb_id}")
                succeeded = True
            except Exception as e:
                succeeded = False

            if succeeded:
                download.status = DownloadStatus.SUCCESS
            else:
                download.status = DownloadStatus.FAILED
            download.last_updated = datetime.now(timezone.utc)
            db.session.commit()


with app.app_context():
    db.create_all()

    thread = threading.Thread(target=background_downloader, daemon=True)
    thread.start()


