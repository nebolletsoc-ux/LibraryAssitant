"""
Database models for Library Assistant.

Supports storing user's TBR (To Be Read) list and books with
availability results cached from library systems.
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Book(db.Model):
    """
    A book record with metadata from ISBN lookups.
    
    Multiple users may reference the same book (via UserBook).
    Books are identified by ISBN; title+author alone is not unique.
    """
    __tablename__ = "books"
    
    id = db.Column(db.Integer, primary_key=True)
    isbn = db.Column(db.String(20), unique=True, nullable=False, index=True)
    title = db.Column(db.String(500), nullable=False)
    author = db.Column(db.String(500), nullable=False)
    cover_url = db.Column(db.String(1000), nullable=True)
    synopsis = db.Column(db.Text, nullable=True)
    genre = db.Column(db.String(200), nullable=True)
    
    # Timestamp when this record was added to the database
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    user_books = db.relationship("UserBook", back_populates="book", cascade="all, delete-orphan")
    availability = db.relationship("Availability", back_populates="book", cascade="all, delete-orphan")
    
    def to_dict(self):
        return {
            "id": self.id,
            "isbn": self.isbn,
            "title": self.title,
            "author": self.author,
            "cover_url": self.cover_url,
            "synopsis": self.synopsis,
            "genre": self.genre,
        }


class UserBook(db.Model):
    """
    A book in a user's TBR list.
    
    Represents the relationship between a user and a book they want to read.
    """
    __tablename__ = "user_books"
    
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    
    # For future multi-user support; currently assumed to be a single "default" user
    user_id = db.Column(db.Integer, default=1, nullable=False)
    
    # Status: "tbr" (to be read), "reading", "read", etc
    status = db.Column(db.String(50), default="tbr", nullable=False)
    
    # User notes
    notes = db.Column(db.Text, nullable=True)
    
    # Timestamp when added to TBR
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Last time availability was checked for this book
    last_checked_at = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    book = db.relationship("Book", back_populates="user_books")
    
    def to_dict_with_book(self):
        return {
            "id": self.id,
            "status": self.status,
            "notes": self.notes,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "book": self.book.to_dict() if self.book else None,
        }


class Availability(db.Model):
    """
    Cached availability result from a library search.
    
    Results are time-limited; availability changes frequently.
    Each result is tied to a specific book, library, provider, and format.
    """
    __tablename__ = "availability"
    
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    
    # Which library (oakland, berkeley, redwood_city, etc)
    library = db.Column(db.String(100), nullable=False)
    
    # Provider (Hoopla, Libby, etc)
    provider = db.Column(db.String(100), nullable=False)
    
    # Format (eBook, Audiobook, Physical, etc)
    format = db.Column(db.String(100), nullable=False)
    
    # Availability status
    available = db.Column(db.Boolean, nullable=False)
    
    # Wait info (if on waitlist)
    wait_text = db.Column(db.String(255), nullable=True)  # e.g. "2-week wait"
    holds = db.Column(db.Integer, nullable=True)  # Number of holds
    wait_weeks = db.Column(db.Integer, nullable=True)  # Estimated wait weeks
    
    # URL to the library's listing
    url = db.Column(db.String(1000), nullable=True)
    
    # When this cached result was retrieved
    checked_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    book = db.relationship("Book", back_populates="availability")
    
    def to_dict(self):
        return {
            "id": self.id,
            "library": self.library,
            "provider": self.provider,
            "format": self.format,
            "available": self.available,
            "wait_text": self.wait_text,
            "holds": self.holds,
            "wait_weeks": self.wait_weeks,
            "url": self.url,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


class LibraryConfig(db.Model):
    """
    User's library configuration.
    
    Stores which libraries the user has selected to search.
    """
    __tablename__ = "library_config"
    
    id = db.Column(db.Integer, primary_key=True)
    
    # For future multi-user support
    user_id = db.Column(db.Integer, default=1, nullable=False)
    
    # Library key (oakland, berkeley, hoopla, etc)
    library_key = db.Column(db.String(100), nullable=False, unique=True)
    
    # Library label for display
    label = db.Column(db.String(200), nullable=False)
    
    # Bibliocommons subdomain (if applicable)
    bibliocommons = db.Column(db.String(100), nullable=True)
    
    # OverDrive/Libby subdomain (if applicable)
    overdrive = db.Column(db.String(100), nullable=True)
    
    # Whether this is a Hoopla library
    hoopla = db.Column(db.Boolean, default=False, nullable=False)
    
    # Whether this library is currently selected
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    
    def to_dict(self):
        return {
            "id": self.id,
            "library_key": self.library_key,
            "label": self.label,
            "bibliocommons": self.bibliocommons,
            "overdrive": self.overdrive,
            "hoopla": self.hoopla,
            "enabled": self.enabled,
        }
