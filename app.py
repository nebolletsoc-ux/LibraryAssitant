import csv
import io
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from requests import RequestException
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, jsonify, redirect, url_for

from models import db, Book, UserBook, Availability, LibraryConfig
from library.isbn import find_isbn
from library.oakland import search_libraries


app = Flask(__name__)

def _normalize_database_url(raw):
    """Normalize a DATABASE_URL for SQLAlchemy.

    Accepts bare "postgres://" (common in go/env hosting) and converts it to
    the "postgresql+psycopg2://" scheme. Returns None when unset so callers
    keep their SQLite fallback.
    """
    url = (raw or "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


# Database configuration
#
# Set DATABASE_URL to a persistent database (e.g. a hosted Postgres from
# Neon/Supabase) so the list survives restarts and redeploys. Without it we
# fall back to the local SQLite file in the instance folder, which is
# ephemeral on Render's free tier (wiped on every deploy/restart).
database_url = _normalize_database_url(os.environ.get("DATABASE_URL"))
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    # Postgres: recycle long-lived pooled connections and re-check them.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_recycle": 300}
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///library_assistant.db"
    # Allow concurrent scan threads to wait out SQLite write locks.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"timeout": 30},
    }
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Initialize database
db.init_app(app)

# Shipped library presets. No library is auto-added or enabled: the app
# starts with an empty configuration and the Add-library screen offers
# these as opt-in choices (plus a custom-library form). This matches the
# "start with no libraries selected" default state.
LIBRARY_PRESETS = {
    "lapl": {"key": "lapl", "label": "Los Angeles Public Library", "overdrive": "lapl"},
    "oakland": {"key": "oakland", "label": "Oakland Public Library", "bibliocommons": "oaklandlibrary"},
    "berkeley": {"key": "berkeley", "label": "Berkeley Public Library", "overdrive": "berkeleypubliclibrary"},
    "redwood_city": {"key": "redwood_city", "label": "Redwood City Public Library", "bibliocommons": "rcpl"},
    "hoopla": {"key": "hoopla", "label": "Hoopla", "hoopla": True},
    "sfpl": {"key": "sfpl", "label": "San Francisco Public Library", "bibliocommons": "sfpl"},
    "ssfpl": {"key": "ssfpl", "label": "South San Francisco Public Library", "bibliocommons": "ssfpl"},
    "alameda_county": {"key": "alameda_county", "label": "Alameda County Library", "bibliocommons": "aclibrary"},
    "contra_costa_county": {"key": "contra_costa_county", "label": "Contra Costa County Library", "bibliocommons": "ccclib"},
}

# Create all tables on app startup
with app.app_context():
    db.create_all()

    # No libraries are seeded by default — the app starts with an empty
    # configuration (see LIBRARY_PRESETS above). Nothing to initialize.


# Optional shared-password gate. Off by default (no env var set = no prompt,
# same as running locally today). Set APP_PASSWORD once this has a public
# URL if you want to keep it to just the people you've shared it with —
# anyone without the password gets a browser login prompt, any username works.
APP_PASSWORD = os.environ.get("APP_PASSWORD")


@app.before_request
def require_password():
    if not APP_PASSWORD:
        return None

    if request.path == "/healthz":
        return None

    auth = request.authorization
    if not auth or auth.password != APP_PASSWORD:
        return (
            "Password required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Library Assistant"'},
        )

    return None


@app.route("/healthz")
def healthz():
    # Lightweight endpoint for hosting platforms to confirm the app is alive.
    return jsonify({"status": "ok"})


MAX_WORKERS = 8
JOB_TIMEOUT_SECONDS = 3600  # Clean up jobs after 1 hour

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Single active background scan at a time; protects job progress counters.
_scan_lock = threading.Lock()
_active_scan = None

jobs = {}

def validate_csv_structure(fieldnames):
    """Validate CSV has required columns. Returns (is_valid, error_message)."""
    if not fieldnames:
        return False, "CSV file is empty or cannot be read."

    fieldset = set(fieldnames)

    storygraph_required = {"Title", "Authors", "Read Status"}
    goodreads_required = {"Title", "Author", "Exclusive Shelf"}

    if storygraph_required <= fieldset or goodreads_required <= fieldset:
        return True, None

    return False, (
        "Unrecognized CSV format. Expected a StoryGraph export "
        f"(columns: {', '.join(sorted(storygraph_required))}) or a "
        f"Goodreads export (columns: {', '.join(sorted(goodreads_required))})."
    )


def categorize_error(error):
    """Categorize error to provide better user feedback."""
    error_str = str(error).lower()
    
    # Network/transient errors
    if any(term in error_str for term in ["timeout", "connection", "network", "refused"]):
        return "network", "Service unavailable. Please try again in a moment."
    
    # Not found errors
    if any(term in error_str for term in ["not found", "404"]):
        return "not_found", "Book not found in ISBN database or library."
    
    # Rate limiting
    if any(term in error_str for term in ["rate", "429", "too many"]):
        return "rate_limited", "Service rate limit exceeded. Please try again later."
    
    # Default
    return "unknown", f"Error: {error}"


def fetch_synopsis(isbn, title, author):
    """
    Fetch a synopsis and genre for a book from Open Library.

    Important: Open Library's edition-level record (what /isbn/{isbn}.json
    and the legacy /api/books?jscmd=data endpoint return) almost never
    carries a "description" — that field lives on the separate parent
    "work" record. So we look up the edition first (mainly to find its
    work key, and to grab any edition-level subjects as a genre fallback),
    then fetch the work record for the actual description/subjects.
    """
    if not isbn:
        return None, None

    headers = {
        # Open Library asks for a descriptive User-Agent; a generic/missing
        # one risks being deprioritized or blocked under load.
        "User-Agent": "LibraryAssistant/1.0 (personal reading-list tool)"
    }

    synopsis = None
    genre = None
    work_key = None

    # Step 1: edition lookup — gives us the work key, and sometimes subjects.
    try:
        edition_url = f"https://openlibrary.org/isbn/{isbn}.json"
        response = requests.get(edition_url, headers=headers, timeout=4)

        if response.status_code == 200:
            edition_data = response.json()

            works = edition_data.get("works") or []
            if works and isinstance(works[0], dict):
                work_key = works[0].get("key")

            subjects = edition_data.get("subjects")
            if subjects:
                genre = subjects[0] if isinstance(subjects[0], str) else str(subjects[0])

    except Exception as e:
        print(f"Edition lookup failed for '{title}' ({isbn}): {e}")

    # Step 1.5: if the ISBN lookup didn't resolve a work (e.g. synthetic
    # "synthetic-*" / "cover-*" ISBNs from the search/add flow), search
    # Open Library by title+author to find the work record.
    if not work_key:
        try:
            query = f"title:{title}"
            if author:
                query += f" author:{author}"
            response = requests.get(
                "https://openlibrary.org/search.json",
                params={"q": query, "limit": 3},
                headers=headers,
                timeout=4,
            )
            response.raise_for_status()
            docs = (response.json() or {}).get("docs") or []
            for doc in docs:
                for key in doc.get("seed") or []:
                    if isinstance(key, str) and key.startswith("/works/"):
                        work_key = key
                        break
                if work_key:
                    break
        except Exception as e:
            print(f"Title-search work lookup failed for '{title}': {e}")

    # Step 2: work lookup — this is where the description usually lives.
    if work_key:
        try:
            work_url = f"https://openlibrary.org{work_key}.json"
            response = requests.get(work_url, headers=headers, timeout=4)
            response.raise_for_status()

            work_data = response.json()

            desc = work_data.get("description")
            if isinstance(desc, dict) and "value" in desc:
                synopsis = desc["value"]
            elif isinstance(desc, str):
                synopsis = desc

            if not genre:
                subjects = work_data.get("subjects")
                if subjects:
                    genre = subjects[0] if isinstance(subjects[0], str) else str(subjects[0])

        except Exception as e:
            print(f"Work lookup failed for '{title}' ({work_key}): {e}")

    # Fallback: the legacy bibkeys endpoint occasionally carries a
    # description directly on the edition even when the above doesn't.
    if not synopsis:
        try:
            url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
            response = requests.get(url, headers=headers, timeout=4)
            response.raise_for_status()

            data = response.json()
            for key, book_data in data.items():
                if "description" in book_data:
                    desc = book_data["description"]
                    if isinstance(desc, dict) and "value" in desc:
                        synopsis = desc["value"]
                    else:
                        synopsis = str(desc) if desc else None

                if not genre:
                    subjects = book_data.get("subjects")
                    if subjects:
                        genre = subjects[0].get("name") if isinstance(subjects[0], dict) else str(subjects[0])

        except Exception as e:
            print(f"Bibkeys fallback failed for '{title}': {e}")

    return synopsis, genre

def _clean_goodreads_isbn(value):
    """Goodreads wraps ISBN cells like ="9780262384254" to stop spreadsheet
    apps from mangling leading zeros/formatting. Strip that wrapper."""
    value = (value or "").strip()
    if value.startswith('="') and value.endswith('"'):
        value = value[2:-1]
    return value.strip()


def load_books(csv_file):
    """Load and validate books from CSV. Raises ValueError on validation failure."""
    try:
        text = csv_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV file is not valid UTF-8 encoded.")
    
    if not text.strip():
        raise ValueError("CSV file is empty.")

    reader = csv.DictReader(io.StringIO(text))

    # Validate CSV structure
    is_valid, error_msg = validate_csv_structure(reader.fieldnames)
    if not is_valid:
        raise ValueError(error_msg)

    fieldset = set(reader.fieldnames)
    is_goodreads = "Exclusive Shelf" in fieldset and "Read Status" not in fieldset

    print("CSV columns:", reader.fieldnames)
    print("Detected format:", "Goodreads" if is_goodreads else "StoryGraph")

    books = []

    try:
        for row_num, row in enumerate(reader, start=2):  # start=2 accounts for header
            title = (row.get("Title") or "").strip()

            if is_goodreads:
                author = (row.get("Author") or "").strip()
                read_status = (row.get("Exclusive Shelf") or "").strip().lower()
                isbn = (
                    _clean_goodreads_isbn(row.get("ISBN13"))
                    or _clean_goodreads_isbn(row.get("ISBN"))
                )
                genre = None  # Goodreads has no dedicated genre column
            else:
                author = (row.get("Authors") or "").strip()
                read_status = (row.get("Read Status") or "").strip().lower()
                isbn = (row.get("ISBN/UID") or "").strip()  # Extract ISBN from CSV if available
                genre = (row.get("Tags") or "").strip()  # StoryGraph has no dedicated genre column; Tags is closest

            # Only analyze books marked "to-read"
            if read_status != "to-read":
                continue

            if not title:
                print(f"Warning: Row {row_num} skipped (missing title)")
                continue

            books.append({
                "title": title,
                "author": author,
                "isbn": isbn or None,  # Use ISBN from CSV; will look up if empty
                "synopsis": None,  # Will be fetched during analysis
                "genre": genre or None,
                "oakland": [],
                "state": "waiting",
                "message": "Waiting to be analyzed",
            })
    except csv.Error as e:
        raise ValueError(f"Error parsing CSV: {e}")

    if not books:
        raise ValueError("No 'to-read' books found in CSV.")

    print(f"Imported {len(books)} to-read books")

    return books


def serialize_result(result):
    return {
        "library": getattr(result, "library", None),
        "provider": getattr(result, "provider", None),
        "format": getattr(result, "format", None),
        "available": getattr(result, "available", False),
        "wait": getattr(result, "wait", None),
        "url": getattr(result, "url", None),
        "holds": getattr(result, "holds", None),
        "waitWeeks": getattr(result, "wait_weeks", None),
    }


def serialize_book(book):
    return {
        "title": book["title"],
        "author": book["author"],
        "isbn": book["isbn"],
        "synopsis": book.get("synopsis"),
        "genre": book.get("genre"),
        "state": book["state"],
        "message": book["message"],
        "oakland": [
            serialize_result(result)
            for result in book["oakland"]
        ],
    }


def analyze_book(job_id, index):
    """Analyze a single book and update its state. Always completes (or marks error)."""
    job = jobs.get(job_id)
    if not job:
        return  # Job was cleaned up
    
    book = job["books"][index]
    title = book["title"]
    author = book["author"]

    try:
        book["state"] = "checking"
        book["message"] = "Finding ISBN…"

        print(f"Analyzing: {title} — {author}")

        # Only look up ISBN if not already provided in CSV
        if book["isbn"]:
            isbn = book["isbn"]
            print(f"ISBN (from CSV): {isbn}")
        else:
            try:
                isbn = find_isbn(title, author)
                book["isbn"] = isbn
                print(f"ISBN (lookup): {isbn}")
            except Exception as e:
                error_type, error_msg = categorize_error(e)
                book["state"] = "error"
                book["message"] = error_msg
                print(f"ISBN lookup failed for '{title}': {error_type} - {e}")
                return

        # Fetch synopsis/genre in the background so it never slows down library search
        if isbn:
            def apply_metadata(book=book, isbn=isbn, title=title, author=author):
                synopsis, genre = fetch_synopsis(isbn, title, author)
                book["synopsis"] = synopsis
                if not book.get("genre") and genre:  # Keep CSV genre if present
                    book["genre"] = genre

            threading.Thread(target=apply_metadata, daemon=True).start()

        book["message"] = "Checking libraries…"

        try:
            if isbn:
                # By this point the upload route has already rejected any
                # request with zero libraries selected, so this should
                # always be populated — job.get(...) here is just a
                # defensive fallback, not a real default library choice.
                library_configs = job.get("library_configs") or []
                results = search_libraries(title, author, library_configs)
            else:
                results = []
                book["message"] = "ISBN not found; skipped library search."
        except Exception as e:
            error_type, error_msg = categorize_error(e)
            book["state"] = "error"
            book["message"] = error_msg
            print(f"Library search failed for '{title}': {error_type} - {e}")
            return

        book["oakland"] = results
        print(f"Results: {results}")

        book["state"] = "complete"
        book["message"] = "Complete"

    except Exception as error:
        print(f"Unexpected error analyzing {title}: {error}")
        book["state"] = "error"
        book["message"] = "Unexpected error. Please try again."

    finally:
        with job["lock"]:
            job["completed"] += 1


@app.route("/", methods=["GET"])
def home():
    # The TBR list (templates/tbr.html) is now the app's home screen.
    return redirect(url_for("tbr"))


@app.route("/status")
def status():

    job_id = request.args.get("job_id")

    if not job_id:
        return jsonify({
            "error": "Missing job_id parameter."
        }), 400

    job = jobs.get(job_id)

    if not job:
        return jsonify({
            "error": "Job not found. It may have expired."
        }), 404

    # Check if job has expired
    if time.time() - job["created_at"] > JOB_TIMEOUT_SECONDS:
        del jobs[job_id]
        return jsonify({
            "error": "Job expired after 1 hour."
        }), 410  # 410 Gone

    with job["lock"]:
        completed = job["completed"]

    books = [
        serialize_book(book)
        for book in job["books"]
        if book["state"] != "waiting"
    ]

    finished = completed >= len(job["books"])
    
    # Clean up completed jobs after response is sent
    if finished:
        # Could add: del jobs[job_id]  # but keep for UI to poll a few more times
        pass

    return jsonify({
        "total": len(job["books"]),
        "completed": completed,
        "books": books,
        "finished": finished,
    })


@app.route("/tbr")
def tbr():
    """Render the Phase 1 standalone TBR list interface."""
    return render_template("tbr.html")


# ============================================================================
# PHASE 1: STANDALONE TBR LIST — NEW API ENDPOINTS
# ============================================================================

@app.route("/api/books", methods=["GET"])
def list_books():
    """
    Get the user's TBR list.
    
    Returns list of UserBook entries with full book data.
    """
    try:
        user_books = UserBook.query.filter_by(user_id=1, status="tbr").all()
        books = []
        for user_book in user_books:
            book_data = user_book.to_dict_with_book()
            availability = user_book.book.availability if user_book.book else []

            # Full cached rows for the frontend's borrow-options/status model:
            # one entry per library/provider/format with holds and wait info.
            book_data["availability"] = [result.to_dict() for result in availability]

            # Per-format summary used by the list-view filters and icon dots.
            formats_by_name = {}
            for result in availability:
                format_name = result.format or "Unknown"
                current = formats_by_name.get(format_name)
                if current and current["available"]:
                    continue
                formats_by_name[format_name] = {
                    "format": format_name,
                    "available": result.available,
                    "wait_text": result.wait_text,
                }
            book_data["availability_summary"] = {
                "checked": user_book.last_checked_at is not None,
                "available_now": any(result.available for result in availability),
                "formats": list(formats_by_name.values()),
            }
            books.append(book_data)

        return jsonify(books)
    except Exception as e:
        print(f"Error listing books: {e}")
        return jsonify({"error": "Failed to load books"}), 500


@app.route("/api/books/search", methods=["POST"])
def search_books():
    """
    Search for a book by title and author.
    
    Uses Open Library API to find matching books.
    
    Request body:
        {
            "title": "The Overstory",
            "author": "Richard Powers"
        }
    
    Returns a list of potential matches.
    """
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    
    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    try:
        # Use Open Library's search API
        query = " ".join(part for part in (title, author) if part)
        search_params = {"q": query, "limit": 10}

        response = requests.get(
            "https://openlibrary.org/search.json",
            params=search_params,
            timeout=5,
            headers={"User-Agent": "LibraryAssistant/1.0"}
        )

        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            return jsonify({
                "error": "Book search is temporarily unavailable. Please try again."
            }), 503

        if not isinstance(data, dict):
            return jsonify({
                "error": "Book search is temporarily unavailable. Please try again."
            }), 503

        results = []
        
        for doc in data.get("docs", []):
            isbn = None
            if doc.get("isbn"):
                isbn = doc["isbn"][0]  # Take first ISBN
            
            result = {
                "title": doc.get("title", ""),
                "author": doc.get("author_name", ["Unknown"])[0] if doc.get("author_name") else "Unknown",
                "isbn": isbn,
                "cover_id": doc.get("cover_id"),
                "year": doc.get("first_publish_year"),
            }
            
            # Include all results regardless of ISBN
            # (ISBN will be looked up when book is added to library search)
            results.append(result)
        
        return jsonify({"results": results[:10]})

    except RequestException as e:
        print(f"Open Library search error: {e}")
        return jsonify({
            "error": "Book search is temporarily unavailable. Please try again."
        }), 503

    except Exception as e:
        print(f"Search error: {e}")
        return jsonify({"error": "Search failed"}), 500


@app.route("/api/books", methods=["POST"])
def add_book():
    """
    Add a book to the user's TBR list.
    
    Request body:
        {
            "title": "The Overstory",
            "author": "Richard Powers",
            "isbn": "9780393635522",
            "cover_url": "https://...",
            "synopsis": "...",
            "genre": "Fiction"
        }
    
    If the book already exists in the database, reuse it.
    Then create a UserBook entry if not already in the user's list.
    """
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    isbn = (data.get("isbn") or "").strip()

    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    # Generate a synthetic ISBN if not provided (using cover_id or title+author hash)
    if not isbn:
        import hashlib
        cover_id = data.get("cover_id")
        if cover_id:
            # Use cover_id as part of the synthetic ISBN
            isbn = f"cover-{cover_id}"
        else:
            # Use hash of title + author
            hash_input = f"{title}|{author}"
            isbn = f"synthetic-{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"
    
    try:
        # Check if book already exists
        book = Book.query.filter_by(isbn=isbn).first()
        
        if not book:
            # Create new book record
            book = Book(
                isbn=isbn,
                title=title,
                author=author,
                cover_url=data.get("cover_url"),
                synopsis=data.get("synopsis"),
                genre=data.get("genre"),
            )
            db.session.add(book)
            db.session.commit()
        
        # Check if already in user's TBR
        user_book = UserBook.query.filter_by(
            user_id=1,
            book_id=book.id,
            status="tbr"
        ).first()
        
        if user_book:
            return jsonify({"error": "Book already in your TBR"}), 409
        
        # Add to user's TBR
        user_book = UserBook(
            user_id=1,
            book_id=book.id,
            status="tbr"
        )
        db.session.add(user_book)
        db.session.commit()
        
        return jsonify(user_book.to_dict_with_book()), 201
    
    except Exception as e:
        db.session.rollback()
        print(f"Error adding book: {e}")
        return jsonify({"error": "Failed to add book"}), 500


@app.route("/api/books/<int:user_book_id>/synopsis", methods=["POST"])
def book_synopsis(user_book_id):
    """
    Fetch and cache a synopsis for a book in the user's TBR list.

    Books added via CSV import or the online search never got a synopsis
    fetched at add time; this endpoint resolves it lazily (with Open Library
    and a persisted cache on the Book record) the first time the detail
    sheet is opened.
    """
    user_book = UserBook.query.filter_by(id=user_book_id).first()
    if not user_book or not user_book.book:
        return jsonify({"error": "Book not found"}), 404

    book = user_book.book
    if not book.synopsis and not book.genre:
        try:
            synopsis, genre = fetch_synopsis(book.isbn, book.title, book.author)
            if synopsis:
                book.synopsis = synopsis
            if genre:
                book.genre = genre
            db.session.commit()
        except Exception as e:
            print(f"Synopsis fetch failed for '{book.title}': {e}")

    return jsonify({
        "id": book.id,
        "title": book.title,
        "synopsis": book.synopsis,
        "genre": book.genre,
    })


@app.route("/api/books/import-csv", methods=["POST"])
def import_tbr_csv():
    """Import to-read books from a Goodreads or StoryGraph export."""
    csv_file = request.files.get("books_file")
    if not csv_file or not csv_file.filename:
        return jsonify({"error": "Choose a Goodreads or StoryGraph CSV file."}), 400

    if not csv_file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Upload a CSV file."}), 400

    try:
        imported_books = load_books(csv_file)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    added = 0
    skipped = 0
    try:
        for imported_book in imported_books:
            title = imported_book["title"]
            author = imported_book.get("author") or "Unknown"
            isbn = imported_book.get("isbn")

            if not isbn:
                import hashlib
                hash_input = f"{title}|{author}"
                isbn = f"synthetic-{hashlib.md5(hash_input.encode()).hexdigest()[:12]}"

            book = Book.query.filter_by(isbn=isbn).first()
            if not book:
                book = Book(
                    isbn=isbn,
                    title=title,
                    author=author,
                    synopsis=imported_book.get("synopsis"),
                    genre=imported_book.get("genre"),
                )
                db.session.add(book)
                db.session.flush()

            user_book = UserBook.query.filter_by(
                user_id=1,
                book_id=book.id,
                status="tbr",
            ).first()
            if user_book:
                skipped += 1
                continue

            db.session.add(UserBook(user_id=1, book_id=book.id, status="tbr"))
            added += 1

        db.session.commit()
        return jsonify({"total": len(imported_books), "added": added, "skipped": skipped}), 201
    except Exception as e:
        db.session.rollback()
        print(f"Error importing TBR CSV: {e}")
        return jsonify({"error": "Failed to import this CSV."}), 500


@app.route("/api/books/<int:user_book_id>", methods=["DELETE"])
def remove_book(user_book_id):
    """
    Remove a book from the user's TBR list.
    """
    try:
        user_book = UserBook.query.filter_by(id=user_book_id, user_id=1).first()
        
        if not user_book:
            return jsonify({"error": "Book not found"}), 404
        
        db.session.delete(user_book)
        db.session.commit()
        
        return jsonify({"message": "Book removed"}), 200
    
    except Exception as e:
        db.session.rollback()
        print(f"Error removing book: {e}")
        return jsonify({"error": "Failed to remove book"}), 500


@app.route("/api/books/clear", methods=["DELETE"])
def clear_tbr_list():
    """
    Remove every book from the user's TBR list.

    Guarded by a required "confirm" token so it can't be triggered
    accidentally; Book/Availability records are left in place.
    """
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "clear_all":
        return jsonify({"error": "Confirmation required"}), 400

    try:
        deleted = UserBook.query.filter_by(user_id=1, status="tbr").delete()
        db.session.commit()
        return jsonify({"message": "Reading list cleared", "deleted": deleted}), 200
    except Exception as e:
        db.session.rollback()
        print(f"Error clearing TBR list: {e}")
        return jsonify({"error": "Failed to clear list"}), 500


@app.route("/api/books/<int:user_book_id>/check", methods=["POST"])
def check_availability(user_book_id):
    """
    Trigger an availability check for a book and return results.
    
    This is a synchronous check (not queued like CSV import).
    Results are cached in the database.
    """
    try:
        user_book = UserBook.query.filter_by(id=user_book_id, user_id=1).first()
        
        if not user_book or not user_book.book:
            return jsonify({"error": "Book not found"}), 404
        
        _refresh_availability(user_book, _library_search_configs())
        
        # Return results
        availability_data = [
            availability.to_dict()
            for availability in Availability.query.filter_by(book_id=user_book.book.id).all()
        ]
        
        return jsonify({
            "book": user_book.book.to_dict(),
            "availability": availability_data,
        }), 200
    
    except Exception as e:
        db.session.rollback()
        print(f"Error checking availability: {e}")
        return jsonify({"error": "Failed to check availability"}), 500


def _library_search_configs():
    """Return enabled library settings in the catalog search format."""
    configs = []
    for library in LibraryConfig.query.filter_by(user_id=1, enabled=True).all():
        config = {"key": library.library_key}
        if library.bibliocommons:
            config["bibliocommons"] = library.bibliocommons
        if library.overdrive:
            config["overdrive"] = library.overdrive
        if library.hoopla:
            config["hoopla"] = True
        configs.append(config)
    return configs


def _refresh_availability(user_book, configs):
    """Replace cached availability for one TBR entry."""
    book = user_book.book
    results = search_libraries(book.title, book.author, configs)
    Availability.query.filter_by(book_id=book.id).delete()

    for result in results:
        db.session.add(Availability(
            book_id=book.id,
            library=getattr(result, "library", "unknown"),
            provider=getattr(result, "provider", "unknown"),
            format=getattr(result, "format", "unknown"),
            available=getattr(result, "available", False),
            wait_text=getattr(result, "wait", None),
            holds=getattr(result, "holds", None),
            wait_weeks=getattr(result, "wait_weeks", None),
            url=getattr(result, "url", None),
        ))

    user_book.last_checked_at = datetime.now(timezone.utc)
    db.session.commit()


def _refresh_one_book(user_book_id, configs):
    """Refresh one TBR entry in a worker thread.

    Each worker pushes its own app context so Flask-SQLAlchemy hands it a
    private scoped session (no cross-thread session sharing).
    """
    with app.app_context():
        user_book = UserBook.query.filter_by(id=user_book_id).first()
        if not user_book or not user_book.book:
            return {"id": user_book_id, "title": None, "error": "missing"}
        title = user_book.book.title
        try:
            _refresh_availability(user_book, configs)
            return {"id": user_book_id, "title": title}
        except Exception as e:
            db.session.rollback()
            print(f"Error checking {title}: {e}")
            return {"id": user_book_id, "title": title, "error": str(e)}


@app.route("/api/books/check-all", methods=["POST"])
def start_check_all():
    """Start a background availability scan of every TBR entry.

    Returns immediately with a job id; the scan continues on the shared
    worker pool and progress is read via GET /api/books/scan-progress/<id>.
    This keeps the request short so slow scans can't time out mobile or
    desktop browsers (a full scan can take minutes on real networks).
    """
    global _active_scan

    configs = _library_search_configs()
    if not configs:
        return jsonify({"error": "No libraries configured"}), 400

    with _scan_lock:
        if _active_scan and not _active_scan["done"]:
            return jsonify({
                "job_id": _active_scan["id"],
                "total": _active_scan["total"],
                "already_running": True,
            }), 202

        user_books = UserBook.query.filter_by(user_id=1, status="tbr").all()
        if not user_books:
            return jsonify({"total": 0, "checked": 0, "failures": [], "done": True}), 200

        job = {
            "id": uuid.uuid4().hex[:12],
            "total": len(user_books),
            "processed": 0,
            "checked": 0,
            "failures": [],
            "done": False,
        }
        _active_scan = job

    scan_ids = [ub.id for ub in user_books]
    threading.Thread(target=_run_scan, args=(job, scan_ids, configs), daemon=True).start()

    return jsonify({"job_id": job["id"], "total": job["total"], "already_running": False}), 202


def _run_scan(job, user_book_ids, configs):
    """Fan out a scan across the worker pool and track progress on the job.

    Each _refresh_one_book pushes its own app context, so this supervisor
    thread never touches a scoped session.
    """
    try:
        futures = [
            executor.submit(_refresh_one_book, user_book_id, configs)
            for user_book_id in user_book_ids
        ]
        for future, user_book_id in zip(futures, user_book_ids):
            result = None
            try:
                result = future.result()
                if result.get("error"):
                    with _scan_lock:
                        job["failures"].append({"id": result["id"], "title": result["title"]})
            except Exception as e:
                result = {"error": str(e)}
                with _scan_lock:
                    job["failures"].append({"id": user_book_id, "title": None, "error": str(e)})
            finally:
                with _scan_lock:
                    job["processed"] += 1
                    if not (result or {}).get("error"):
                        job["checked"] += 1
    finally:
        with _scan_lock:
            job["done"] = True


@app.route("/api/books/scan-progress/<job_id>", methods=["GET"])
def scan_progress(job_id):
    """Progress of a running/finished availability scan."""
    with _scan_lock:
        if not _active_scan or _active_scan["id"] != job_id:
            return jsonify({"error": "Unknown scan"}), 404
        snapshot = dict(_active_scan)
        snapshot["failures"] = list(_active_scan["failures"])
    return jsonify(snapshot), 200


@app.route("/api/books/<int:user_book_id>/availability", methods=["GET"])
def get_availability(user_book_id):
    """
    Get cached availability for a book.
    """
    try:
        user_book = UserBook.query.filter_by(id=user_book_id, user_id=1).first()
        
        if not user_book or not user_book.book:
            return jsonify({"error": "Book not found"}), 404
        
        book = user_book.book
        availability = Availability.query.filter_by(book_id=book.id).all()
        
        return jsonify({
            "book": book.to_dict(),
            "availability": [a.to_dict() for a in availability],
            "last_checked_at": user_book.last_checked_at.isoformat() if user_book.last_checked_at else None,
        }), 200
    
    except Exception as e:
        print(f"Error getting availability: {e}")
        return jsonify({"error": "Failed to get availability"}), 500


@app.route("/api/libraries", methods=["GET"])
def get_libraries():
    """
    Get all available libraries and their current enabled status.
    """
    try:
        libraries = LibraryConfig.query.filter_by(user_id=1).all()
        return jsonify([lib.to_dict() for lib in libraries])
    except Exception as e:
        print(f"Error getting libraries: {e}")
        return jsonify({"error": "Failed to get libraries"}), 500


@app.route("/api/libraries/available", methods=["GET"])
def available_libraries():
    """Libraries the user has not added yet (from the shipped presets)."""
    try:
        existing = {
            c.library_key
            for c in LibraryConfig.query.filter_by(user_id=1).all()
        }
        result = [
            {
                "library_key": key,
                "label": preset["label"],
                "sub": (
                    "Unlimited borrows" if preset.get("hoopla")
                    else "Libby / OverDrive" if preset.get("overdrive")
                    else "Bibliocommons"
                ),
            }
            for key, preset in LIBRARY_PRESETS.items()
            if key not in existing
        ]
        return jsonify(result)
    except Exception as e:
        print(f"Error listing available libraries: {e}")
        return jsonify({"error": "Failed to list libraries"}), 500


@app.route("/api/libraries/<int:library_id>", methods=["PATCH"])
def update_library(library_id):
    """
    Enable or disable a library.
    
    Request body:
        {
            "enabled": true/false
        }
    """
    try:
        library = LibraryConfig.query.filter_by(id=library_id, user_id=1).first()
        
        if not library:
            return jsonify({"error": "Library not found"}), 404
        
        data = request.get_json() or {}
        library.enabled = bool(data.get("enabled", library.enabled))
        
        db.session.commit()
        return jsonify(library.to_dict()), 200
    
    except Exception as e:
        db.session.rollback()
        print(f"Error updating library: {e}")
        return jsonify({"error": "Failed to update library"}), 500


@app.route("/api/libraries", methods=["POST"])
def add_library():
    """
    Add a library to the user's configuration and enable it.

    Preset: {"library_key": "sfpl"}
    Custom: {"library_key": "my_lib", "label": "...", "bibliocommons": "subdomain"}
            or {"library_key": "my_lib", "label": "...", "overdrive": "subdomain"}
            or {"library_key": "my_lib", "label": "..."}  # label only; not searched
    """
    try:
        data = request.get_json() or {}
        library_key = (data.get("library_key") or "").strip().lower()
        if not library_key:
            return jsonify({"error": "library_key is required"}), 400

        existing = LibraryConfig.query.filter_by(user_id=1, library_key=library_key).first()
        if existing:
            return jsonify({"error": "That library is already configured"}), 409

        preset = LIBRARY_PRESETS.get(library_key)
        if preset:
            label = preset["label"]
            bibliocommons = preset.get("bibliocommons")
            overdrive = preset.get("overdrive")
            hoopla = preset.get("hoopla", False)
        else:
            label = (data.get("label") or "").strip()
            if not label:
                return jsonify({"error": "Unknown library_key; provide a label for a custom library"}), 400
            bibliocommons = (data.get("bibliocommons") or "").strip() or None
            overdrive = (data.get("overdrive") or "").strip() or None
            hoopla = bool(data.get("hoopla"))
            # A label-only custom library is allowed (it just isn't searched).

        new_library = LibraryConfig(
            user_id=1,
            library_key=library_key,
            label=label,
            bibliocommons=bibliocommons,
            overdrive=overdrive,
            hoopla=hoopla,
            enabled=True,
        )
        db.session.add(new_library)
        db.session.commit()
        return jsonify(new_library.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        print(f"Error adding library: {e}")
        return jsonify({"error": "Failed to add library"}), 500


if __name__ == "__main__":
    # PORT is set by hosting platforms (Render, Railway, etc.) at runtime;
    # 5001 is the local fallback, chosen to avoid colliding with macOS's
    # AirPlay Receiver, which uses 5000.
    port = int(os.environ.get("PORT", 5001))

    # debug=True must NEVER be on for anything reachable outside your own
    # machine — Flask's debugger allows arbitrary code execution to anyone
    # who can reach it. Set FLASK_DEBUG=1 locally if you want it back for
    # development; it's off by default now so a deploy can't accidentally
    # ship with it on.
    debug = os.environ.get("FLASK_DEBUG") == "1"

    # host="0.0.0.0" makes this reachable from other devices (e.g. your
    # phone) on the same network, and is also what hosting platforms expect.
    app.run(debug=debug, host="0.0.0.0", port=port)