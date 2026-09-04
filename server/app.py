import os
from enum import Enum

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from pydantic import BaseModel, ValidationError, HttpUrl
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
migrate = Migrate()


def create_app():
    app = Flask(__name__)

    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    migrate.init_app(app, db)

    return app


app = create_app()


class QueuedDownload(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(nullable=False)
    tvdbid: Mapped[str] = mapped_column(nullable=False)
    mega_url: Mapped[str] = mapped_column(nullable=False)


class DownloadMode(str, Enum):
    MOVIE = "m"
    SHOW = "s"


class CreateQueuedDownloadRequest(BaseModel):
    mode: DownloadMode
    mega_url: HttpUrl
    tvdbid: str


@app.post("/api/queue")
def queue():
    try:
        data = CreateQueuedDownloadRequest(**request.json).model_dump()
    except ValidationError as error:
        return jsonify(error.errors()), 400

    with db.session.begin():
        db.session.add(QueuedDownload(**data))

    return jsonify({"message": "Download queued successfully"}), 201


def background_downloader():
    while True:
        with db.session.begin():
            download = db.session.query(QueuedDownload).first()
            if download:

                db.session.delete(download)
                db.session.commit()
                print(f"Downloaded {download.tvdbid}")


with app.app_context():
    db.create_all()


