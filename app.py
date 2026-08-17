import csv
import io
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, jsonify

from library.isbn import find_isbn
from library.oakland import search


app = Flask(__name__)

MAX_WORKERS = 8
JOB_TIMEOUT_SECONDS = 3600  # Clean up jobs after 1 hour

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

jobs = {}


def validate_csv_structure(fieldnames):
    """Validate CSV has required columns. Returns (is_valid, error_message)."""
    if not fieldnames:
        return False, "CSV file is empty or cannot be read."
    
    required = {"Title", "Authors", "Read Status"}
    missing = required - set(fieldnames)
    
    if missing:
        return False, f"Missing required columns: {', '.join(sorted(missing))}. Expected: {', '.join(sorted(required))}"
    
    return True, None


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
    """Fetch synopsis and genre (subjects) from Open Library API in one request."""
    if not isbn:
        return None, None

    try:
        # Try by ISBN first (with short timeout for network-restricted environments)
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
        response = requests.get(url, timeout=2)  # Reduced from 5 to 2 seconds
        response.raise_for_status()

        data = response.json()
        synopsis = None
        genre = None
        for key, book_data in data.items():
            if "description" in book_data:
                desc = book_data["description"]
                # Handle both string and dict descriptions
                if isinstance(desc, dict) and "value" in desc:
                    synopsis = desc["value"]
                else:
                    synopsis = str(desc) if desc else None
            subjects = book_data.get("subjects")
            if subjects:
                genre = subjects[0].get("name") if isinstance(subjects[0], dict) else str(subjects[0])

        return synopsis, genre
    except Exception as e:
        print(f"Synopsis fetch failed for {title}: {e}")
        return None, None

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

    print("CSV columns:", reader.fieldnames)

    books = []

    try:
        for row_num, row in enumerate(reader, start=2):  # start=2 accounts for header
            title = (row.get("Title") or "").strip()
            author = (row.get("Authors") or "").strip()
            read_status = (row.get("Read Status") or "").strip().lower()
            csv_isbn = (row.get("ISBN/UID") or "").strip()  # Extract ISBN from CSV if available
            genre = (row.get("Tags") or "").strip()  # StoryGraph has no dedicated genre column; Tags is closest

            # Only analyze StoryGraph books marked "to-read"
            if read_status != "to-read":
                continue

            if not title:
                print(f"Warning: Row {row_num} skipped (missing title)")
                continue

            books.append({
                "title": title,
                "author": author,
                "isbn": csv_isbn or None,  # Use ISBN from CSV; will look up if empty
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
                # Check which libraries are selected
                selected_libs = job.get("selected_libraries", ["oakland", "berkeley"])
                oakland = search(title, author) if selected_libs else []
            else:
                oakland = []
                book["message"] = "ISBN not found; skipped library search."
        except Exception as e:
            error_type, error_msg = categorize_error(e)
            book["state"] = "error"
            book["message"] = error_msg
            print(f"Oakland search failed for '{title}': {error_type} - {e}")
            return

        # Filter results by selected libraries
        selected_libs = job.get("selected_libraries", ["oakland", "berkeley"])
        filtered_oakland = [
            result for result in oakland
            if (result.library or "").lower() in selected_libs
        ]
        
        book["oakland"] = filtered_oakland
        print(f"Oakland (filtered): {filtered_oakland}")

        book["state"] = "complete"
        book["message"] = "Complete"

    except Exception as error:
        print(f"Unexpected error analyzing {title}: {error}")
        book["state"] = "error"
        book["message"] = "Unexpected error. Please try again."

    finally:
        with job["lock"]:
            job["completed"] += 1


@app.route("/", methods=["GET", "POST"])
def home():

    if request.method == "GET":
        return render_template(
            "index.html",
            books=[],
            total_books=0,
            uploaded=False,
        )

    csv_file = request.files.get("books_file")

    if not csv_file:
        return jsonify({
            "error": "No CSV file uploaded."
        }), 400

    if not csv_file.filename:
        return jsonify({
            "error": "CSV file must have a filename."
        }), 400

    try:
        books = load_books(csv_file)
    except ValueError as e:
        return jsonify({
            "error": str(e)
        }), 400
    except Exception as e:
        print(f"Unexpected error loading CSV: {e}")
        return jsonify({
            "error": "Failed to process CSV file. Please check the file format."
        }), 400

    job_id = str(id(books))
    
    # Get selected libraries from request
    selected_libraries = request.form.get("libraries", "oakland,berkeley").split(",")
    selected_libraries = [lib.strip().lower() for lib in selected_libraries if lib.strip()]

    jobs[job_id] = {
        "books": books,
        "completed": 0,
        "created_at": time.time(),
        "selected_libraries": selected_libraries,
        "lock": threading.Lock(),
    }

    for index in range(len(books)):
        executor.submit(
            analyze_book,
            job_id,
            index
        )

    return jsonify({
        "job_id": job_id,
        "total": len(books),
    })


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


if __name__ == "__main__":
    app.run(debug=True)