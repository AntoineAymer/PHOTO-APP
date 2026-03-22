"""SQLAlchemy models for PhotoFrame & Shadow Game."""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime,
    ForeignKey, Enum as SAEnum, create_engine, text
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import enum
import os

DB_DIR = os.path.expanduser("~/.photoframe")
DB_PATH = os.path.join(DB_DIR, "photoframe.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

Base = declarative_base()


class SlideType(str, enum.Enum):
    FRAME = "frame"
    GAME = "game"


class MediaType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"


class GameState(str, enum.Enum):
    LOBBY = "lobby"
    PLAYING = "playing"
    PAUSED = "paused"
    REVEALING = "revealing"
    LEADERBOARD = "leaderboard"
    BREAK = "break"
    FINISHED = "finished"


class Media(Base):
    __tablename__ = "media"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_path = Column(String, nullable=False, unique=True)
    filename = Column(String, nullable=False)
    media_type = Column(SAEnum(MediaType), nullable=False)
    format = Column(String, nullable=False)  # jpeg, png, heic, mp4, etc.
    width = Column(Integer)
    height = Column(Integer)
    duration = Column(Float)  # seconds, for video
    thumbnail_path = Column(String)
    web_path = Column(String)  # browser-compatible version (converted HEIC, transcoded video)
    exif_date = Column(DateTime)
    source_folder = Column(String)
    category = Column(String)  # user-assigned tag/year
    imported_at = Column(DateTime, default=datetime.utcnow)

    slides = relationship("Slide", back_populates="media")


class Experience(Base):
    __tablename__ = "experiences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    language = Column(String, default="en")  # en or fr
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Frame settings
    default_image_duration = Column(Integer, default=8)  # seconds
    max_video_duration = Column(Integer, default=60)  # seconds cap
    transition_effect = Column(String, default="fade")  # fade, slide, kenburns

    # Game settings
    default_question_timer = Column(Integer, default=15)  # seconds
    quiz_intro_duration = Column(Integer, default=3)  # seconds for "Quiz Time!" intro screen
    relaxed_mode = Column(Boolean, default=False)  # no timer, all correct = 100pts
    show_leaderboard_between = Column(Boolean, default=True)
    leaderboard_duration = Column(Integer, default=5)  # seconds
    sound_enabled = Column(Boolean, default=True)

    # Scoring settings
    speed_scoring = Column(Boolean, default=True)  # True=speed-based, False=flat points
    max_points = Column(Integer, default=100)  # points for fastest correct / flat correct
    min_points = Column(Integer, default=10)  # points for slowest correct (speed mode only)
    wrong_points = Column(Integer, default=0)  # 0=no penalty, negative=deduct points

    # Player phone display: "choices_only", "question_and_choices", "full" (image+question+choices)
    player_display_mode = Column(String, default="question_and_choices")

    slides = relationship("Slide", back_populates="experience", order_by="Slide.position")


class Slide(Base):
    __tablename__ = "slides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    experience_id = Column(Integer, ForeignKey("experiences.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False)  # order in sequence
    slide_type = Column(SAEnum(SlideType), nullable=False)

    # Media reference
    media_id = Column(Integer, ForeignKey("media.id"), nullable=False)

    # FRAME-specific
    display_duration = Column(Integer)  # override per-slide, seconds

    # GAME-specific
    quiz_type = Column(String)  # "shadow", "missing", or "zoom"
    silhouette_path = Column(String)  # generated silhouette image path
    question_timer = Column(Integer)  # override per-slide, seconds
    answer_a = Column(String)
    answer_b = Column(String)
    answer_c = Column(String)
    answer_d = Column(String)
    correct_answer = Column(String)  # "a", "b", "c", or "d"

    experience = relationship("Experience", back_populates="slides")
    media = relationship("Media", back_populates="slides")
    answers = relationship("PlayerAnswer", back_populates="slide")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(5), unique=True, nullable=False)
    experience_id = Column(Integer, ForeignKey("experiences.id"), nullable=False)
    state = Column(SAEnum(GameState), default=GameState.LOBBY)
    current_slide_index = Column(Integer, default=0)
    is_locked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    players = relationship("Player", back_populates="room")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    nickname = Column(String(20), nullable=False)
    sid = Column(String)  # Socket.IO session ID
    is_connected = Column(Boolean, default=True)
    disconnected_at = Column(DateTime)
    total_score = Column(Integer, default=0)
    joined_at = Column(DateTime, default=datetime.utcnow)

    room = relationship("Room", back_populates="players")
    answers = relationship("PlayerAnswer", back_populates="player")


class PlayerAnswer(Base):
    __tablename__ = "player_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    slide_id = Column(Integer, ForeignKey("slides.id", ondelete="CASCADE"), nullable=False)
    answer = Column(String(1))  # "a", "b", "c", "d"
    is_correct = Column(Boolean, default=False)
    time_taken = Column(Float)  # seconds from question start
    points_earned = Column(Integer, default=0)
    answered_at = Column(DateTime, default=datetime.utcnow)

    player = relationship("Player", back_populates="answers")
    slide = relationship("Slide", back_populates="answers")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_pin = Column(String)  # hashed PIN
    language = Column(String, default="en")
    gemini_api_key_set = Column(Boolean, default=False)  # key is in .env, not DB
    family_context = Column(String, default="")  # birth years for age-based date estimation


# Engine setup
def get_engine():
    os.makedirs(DB_DIR, exist_ok=True)
    return create_async_engine(DATABASE_URL, echo=False)


def get_session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight migration: add columns that may be missing in older DBs
        try:
            await conn.execute(text("ALTER TABLE settings ADD COLUMN family_context TEXT DEFAULT ''"))
        except Exception:
            pass  # column already exists
        try:
            await conn.execute(text("ALTER TABLE slides ADD COLUMN quiz_type TEXT"))
        except Exception:
            pass  # column already exists
        for col, typ, default in [
            ("speed_scoring", "BOOLEAN", "1"),
            ("max_points", "INTEGER", "100"),
            ("min_points", "INTEGER", "10"),
            ("wrong_points", "INTEGER", "0"),
            ("quiz_intro_duration", "INTEGER", "3"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE experiences ADD COLUMN {col} {typ} DEFAULT {default}"))
            except Exception:
                pass  # column already exists
