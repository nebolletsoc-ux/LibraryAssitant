import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, jsonify

from library.isbn import find_isbn
from library.oakland import search


app = Flask(__name__)

MAX_WORKERS = 8

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

jobs = {}

def load_books(csv_file):
    text = csv_file.read().decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(text))

    print("CSV columns:", reader.fieldnames)

    books = []

    for row in reader:
        title = (row.get("Title") or "").strip()
        author = (row.get("Authors") or "").strip()
        read_status = (row.get("Read Status") or "").strip().lower()

        # Only analyze StoryGraph books marked "to-read"
        if read_status != "to-read":
            continue

        if not title:
            continue

        books.append({
            "title": title,
            "author": author,
            "isbn": None,
            "oakland": [],
            "state": "waiting",
            "message": "Waiting to be analyzed",
        })

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
        "state": book["state"],
        "message": book["message"],
        "oakland": [
            serialize_result(result)
            for result in book["oakland"]
        ],
    }


def analyze_book(job_id, index):
    job = jobs[job_id]
    book = job["books"][index]

    title = book["title"]
    author = book["author"]

    try:
        book["state"] = "checking"
        book["message"] = "Finding ISBN…"

        print(f"Analyzing: {title} — {author}")

        isbn = find_isbn(title, author)

        book["isbn"] = isbn

        print(f"ISBN: {isbn}")

        book["message"] = "Checking Oakland Library…"

        if isbn:
            oakland = search(title, author)
        else:
            oakland = []

        book["oakland"] = oakland

        print(f"Oakland: {oakland}")

        book["state"] = "complete"
        book["message"] = "Complete"

    except Exception as error:
        print(f"ERROR analyzing {title}: {error}")

        book["state"] = "error"
        book["message"] = f"Error: {error}"

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

    books = load_books(csv_file)

    job_id = str(id(books))

    jobs[job_id] = {
        "books": books,
        "completed": 0,
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

    job = jobs.get(job_id)

    if not job:
        return jsonify({
            "error": "Job not found"
        }), 404

    with job["lock"]:
        completed = job["completed"]

    books = [
        serialize_book(book)
        for book in job["books"]
        if book["state"] != "waiting"
    ]

    return jsonify({
        "total": len(job["books"]),
        "completed": completed,
        "books": books,
        "finished": completed >= len(job["books"]),
    })


if __name__ == "__main__":
    app.run(debug=True)