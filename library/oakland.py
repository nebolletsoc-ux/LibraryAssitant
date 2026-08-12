import json
import re
from urllib.parse import quote_plus

import requests

from library.models import LibraryResult


HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

OAKLAND_CATALOG_URL = (
    "https://oaklandlibrary.bibliocommons.com/v2/search"
)

BERKELEY_LIBBY_URL = (
    "https://berkeleypubliclibrary.overdrive.com/search"
)

HOOPLA_SEARCH_URL = (
    "https://www.hoopladigital.com/search"
)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by",
    "for", "from", "in", "is", "it", "of", "on", "or",
    "the", "to", "with"
}


# ----------------------------------------------------------------------
# TEXT MATCHING
# ----------------------------------------------------------------------

def normalize_text(value):
    if not value:
        return ""

    value = value.lower()

    # Normalize punctuation and apostrophes.
    value = value.replace("’", "'")
    value = re.sub(r"[^a-z0-9]+", " ", value)

    return " ".join(value.split())


def title_tokens(title):
    return [
        token
        for token in normalize_text(title).split()
        if token and token not in STOPWORDS
    ]


def title_matches(text, title, author=""):
    """
    Reasonably conservative title/author matching.

    We intentionally do NOT try to recognize anthology appearances.
    """

    haystack = normalize_text(text)
    wanted = normalize_text(title)

    if not wanted:
        return False

    if wanted in haystack:
        return True

    tokens = title_tokens(title)

    if not tokens:
        return False

    matches = sum(
        1 for token in tokens
        if token in haystack
    )

    # Short titles need stronger evidence.
    if len(tokens) == 1:
        if matches == 1 and author:
            author_tokens = [
                x for x in normalize_text(author).split()
                if x
            ]

            return any(
                token in haystack
                for token in author_tokens[-2:]
            )

        return matches == 1

    # For normal multi-word titles require at least two
    # significant title words.
    return matches >= min(2, len(tokens))


def author_matches(text, author):
    if not author:
        return True

    haystack = normalize_text(text)
    tokens = normalize_text(author).split()

    if not tokens:
        return True

    # Last name is generally the strongest signal.
    return tokens[-1] in haystack


# ----------------------------------------------------------------------
# RESULT HELPERS
# ----------------------------------------------------------------------

def result(
    library,
    provider,
    format_name,
    available,
    wait,
    url,
):
    return LibraryResult(
        library=library,
        provider=provider,
        format=format_name,
        available=available,
        wait=wait,
        url=url,
    )


def clean_url(url):
    if not url:
        return None

    # Occasionally the HTML contains an already-marked-up URL.
    url = url.replace("&amp;", "&")

    return url


def detect_format(text):
    lowered = text.lower()

    if "audiobook" in lowered or "audio book" in lowered:
        return "Audiobook"

    if "ebook" in lowered or "e-book" in lowered:
        return "eBook"

    return "Digital"


# ----------------------------------------------------------------------
# LIBBY / OVERDRIVE
# ----------------------------------------------------------------------

def _extract_js_object(html, variable_name):
    """
    Extract a JavaScript object assigned like:

        window.OverDrive.titleCollection = [...]

    or

        window.OverDrive.mediaItems = {...}
    """

    marker = f"window.OverDrive.{variable_name}"

    start = html.find(marker)

    if start < 0:
        return None

    equals = html.find("=", start)

    if equals < 0:
        return None

    value_start = equals + 1

    # Find the first JSON delimiter.
    while (
        value_start < len(html)
        and html[value_start].isspace()
    ):
        value_start += 1

    if value_start >= len(html):
        return None

    opening = html[value_start]

    if opening not in "[{":
        return None

    closing = "]" if opening == "[" else "}"

    depth = 0
    in_string = False
    escaped = False

    for i in range(value_start, len(html)):

        char = html[i]

        if in_string:

            if escaped:
                escaped = False
                continue

            if char == "\\":
                escaped = True
                continue

            if char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True
            continue

        if char == opening:
            depth += 1

        elif char == closing:
            depth -= 1

            if depth == 0:
                raw = html[value_start:i + 1]

                try:
                    return json.loads(raw)
                except Exception:
                    return None

    return None


def _libby_format(item):
    text = json.dumps(item).lower()

    if "audiobook" in text:
        return "Audiobook"

    if "ebook" in text or "e-book" in text:
        return "eBook"

    return "Digital"


def _libby_title(item):
    """
    Pull title from the various structures used by OverDrive.
    """

    if not isinstance(item, dict):
        return ""

    for key in (
        "title",
        "name",
        "displayTitle",
    ):
        value = item.get(key)

        if isinstance(value, str):
            return value

    return ""


def _libby_author(item):
    if not isinstance(item, dict):
        return ""

    creators = item.get("creators")

    if isinstance(creators, list):

        names = []

        for creator in creators:

            if not isinstance(creator, dict):
                continue

            role = str(
                creator.get("role", "")
            ).lower()

            if role == "author":
                name = creator.get("name")

                if name:
                    names.append(name)

        if names:
            return " ".join(names)

    return ""


def _libby_url(item, base_url):
    """
    Prefer the actual media URL when available.
    """

    if not isinstance(item, dict):
        return None

    # Some versions expose a direct URL.
    for key in (
        "url",
        "href",
        "mediaUrl",
        "permalink",
    ):
        value = item.get(key)

        if isinstance(value, str):
            if value.startswith("http"):
                return value

            if value.startswith("/"):
                return base_url.rstrip("/") + value

    # Some OverDrive data contains a media ID.
    for key in (
        "id",
        "mediaId",
        "reserveId",
    ):
        value = item.get(key)

        if isinstance(value, str) and value:

            # UUIDs are generally media identifiers.
            if "-" in value or len(value) > 20:
                return (
                    base_url.rstrip("/")
                    + "/media/"
                    + value
                )

    return None


def _extract_wait(text):
    """
    Attempt to identify a human-readable hold/wait indication.
    """

    lowered = text.lower()

    patterns = [
        r"(\d+)\s*week[s]?\s*(?:wait|hold)",
        r"(\d+)\s*week[s]?\s*(?:waitlist|waiting list)",
        r"(\d+)\s*day[s]?\s*(?:wait|hold)",
        r"(\d+)\s*month[s]?\s*(?:wait|hold)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            lowered
        )

        if match:
            number = match.group(1)

            if "week" in pattern:
                return f"{number}-week wait"

            if "day" in pattern:
                return f"{number}-day wait"

            if "month" in pattern:
                return f"{number}-month wait"

    if (
        "join waitlist" in lowered
        or "join the waitlist" in lowered
        or "place hold" in lowered
        or "place a hold" in lowered
        or "wait list" in lowered
        or "waiting list" in lowered
    ):
        return "Waitlist"

    return None


def _detect_libby_availability(item, html):
    """
    Determine availability conservatively.

    Important:
    existence of a Libby title does NOT mean it is
    immediately borrowable.
    """

    text = json.dumps(item).lower()

    # Explicit availability flags in OverDrive data.
    for key in (
        "isAvailable",
        "available",
        "isAvailableNow",
    ):
        value = item.get(key)

        if isinstance(value, bool):
            if value:
                return True, None

            wait = _extract_wait(text)

            return False, wait or "Waitlist"

    # Search the item's serialized data for useful indicators.
    if any(
        phrase in text
        for phrase in (
            '"availability":"available"',
            '"availability": "available"',
            '"available":true',
            '"isavailable":true',
        )
    ):
        return True, None

    # If the surrounding HTML clearly says there is a hold/wait,
    # do not call it available.
    wait = _extract_wait(html)

    if wait:
        return False, wait

    lowered = html.lower()

    # Strong indications of an immediate checkout.
    if (
        "borrow" in lowered
        or "borrow now" in lowered
        or "available now" in lowered
    ):
        return True, None

    # We don't know.
    return False, None


def search_berkeley_libby(
    title,
    author,
    timeout=15,
):
    """
    Search Berkeley's OverDrive collection.

    Returns every strong title match found in titleCollection
    and mediaItems, deduplicated by URL/format.
    """

    query = f"{title} {author}".strip()

    try:
        response = requests.get(
            BERKELEY_LIBBY_URL,
            params={"query": query},
            headers=HEADERS,
            timeout=timeout,
        )

        print(
            f"Berkeley Libby: "
            f"{response.status_code} "
            f"{len(response.text)} bytes"
        )

    except Exception as error:

        print(
            f"Berkeley Libby ERROR: {error}"
        )

        return []

    if response.status_code >= 400:
        return []

    html = response.text

    title_collection = _extract_js_object(
        html,
        "titleCollection"
    )

    media_items = _extract_js_object(
        html,
        "mediaItems"
    )

    if isinstance(title_collection, list):
        print(
            "Berkeley Libby: "
            f"titleCollection has "
            f"{len(title_collection)} objects"
        )

    if isinstance(media_items, dict):
        print(
            "Berkeley Libby: "
            f"mediaItems has "
            f"{len(media_items)} objects"
        )

    candidates = []

    if isinstance(title_collection, list):
        candidates.extend(title_collection)

    if isinstance(media_items, dict):
        candidates.extend(
            media_items.values()
        )

    results = []
    seen = set()

    for item in candidates:

        if not isinstance(item, dict):
            continue

        item_title = _libby_title(item)

        item_author = _libby_author(item)

        combined = (
            f"{item_title} "
            f"{item_author} "
            f"{json.dumps(item)}"
        )

        if not title_matches(
            combined,
            title,
            author,
        ):
            continue

        # Don't match a completely different work just because
        # the search result contains the requested title somewhere.
        if item_title:
            if not title_matches(
                item_title,
                title,
                author,
            ):
                continue

        format_name = _libby_format(item)

        url = _libby_url(
            item,
            "https://berkeleypubliclibrary.overdrive.com",
        )

        if not url:
            continue

        availability, wait = (
            _detect_libby_availability(
                item,
                html,
            )
        )

        key = (
            url,
            format_name,
        )

        if key in seen:
            continue

        seen.add(key)

        print(
            "Berkeley Libby MATCH:",
            item_title or title,
            "/",
            format_name,
            "/",
            "AVAILABLE" if availability
            else wait or "STATUS UNKNOWN",
        )

        results.append(
            result(
                library="berkeley",
                provider="Libby",
                format_name=format_name,
                available=availability,
                wait=wait,
                url=url,
            )
        )

    if not results:
        print(
            "Berkeley Libby: "
            f"no specific match for {title}"
        )

    return results


# ----------------------------------------------------------------------
# HOOPLA
# ----------------------------------------------------------------------

def _find_hoopla_candidates(
    html,
    title,
    author,
):
    """
    Find ALL Hoopla title URLs associated with the requested
    title rather than stopping after the first URL.

    Hoopla currently uses both numeric IDs and UUID-like IDs.
    """

    patterns = [
        r'https?://(?:www\.)?hoopladigital\.com/title/[^"\'<>\s]+',
        r'/title/[A-Za-z0-9_-]+',
    ]

    candidates = []

    for pattern in patterns:

        for match in re.findall(
            pattern,
            html,
            re.IGNORECASE,
        ):

            url = match

            if url.startswith("/"):
                url = (
                    "https://www.hoopladigital.com"
                    + url
                )

            url = clean_url(url)

            if url not in candidates:
                candidates.append(url)

    # Now verify each URL against the nearby HTML.
    verified = []

    for url in candidates:

        position = html.lower().find(
            url.lower()
        )

        if position < 0:
            continue

        start = max(
            0,
            position - 3000
        )

        end = min(
            len(html),
            position + 5000
        )

        snippet = html[start:end]

        if title_matches(
            snippet,
            title,
            author,
        ):
            verified.append(url)

    # If the URL itself wasn't present literally because of
    # HTML encoding, do a broader title-based fallback.
    if not verified:

        lowered = html.lower()

        if title_matches(
            lowered,
            title,
            author,
        ):
            verified = candidates

    return verified


def search_berkeley_hoopla(
    title,
    author,
    timeout=15,
):
    """
    Hoopla is deliberately searched ONLY through Berkeley.

    This reflects the user's actual setup: Hoopla is connected
    to one library, while Libby can contain multiple libraries.
    """

    query = f"{title} {author}".strip()

    try:

        response = requests.get(
            HOOPLA_SEARCH_URL,
            params={"q": query},
            headers=HEADERS,
            timeout=timeout,
        )

        print(
            f"Hoopla: "
            f"{response.status_code} "
            f"{len(response.text)} bytes"
        )

    except Exception as error:

        print(
            f"Hoopla ERROR: {error}"
        )

        return []

    if response.status_code >= 400:
        return []

    html = response.text

    urls = _find_hoopla_candidates(
        html,
        title,
        author,
    )

    if not urls:

        print(
            "Hoopla: no title URLs found for",
            title
        )

        return []

    results = []
    seen = set()

    for url in urls:

        # Examine the local portion of the result page.
        position = html.lower().find(
            url.lower()
        )

        start = max(
            0,
            position - 3000
        )

        end = min(
            len(html),
            position + 5000
        )

        snippet = html[start:end]

        format_name = detect_format(
            snippet
        )

        # Hoopla normally permits immediate borrowing.
        # If the page explicitly says otherwise, preserve that.
        wait = _extract_wait(snippet)

        available = wait is None

        key = (
            url,
            format_name,
        )

        if key in seen:
            continue

        seen.add(key)

        print(
            "Hoopla MATCH:",
            title,
            "/",
            format_name,
            "/",
            "AVAILABLE" if available
            else wait,
        )

        results.append(
            result(
                library="berkeley",
                provider="Hoopla",
                format_name=format_name,
                available=available,
                wait=wait,
                url=url,
            )
        )

    return results


# ----------------------------------------------------------------------
# OAKLAND
# ----------------------------------------------------------------------

def _extract_oakland_hoopla(
    html,
    title,
    author,
):
    """
    Oakland catalog search.

    Oakland may expose Hoopla links through its catalog.
    We return all matching Hoopla title URLs found there.
    """

    urls = _find_hoopla_candidates(
        html,
        title,
        author,
    )

    results = []
    seen = set()

    for url in urls:

        position = html.lower().find(
            url.lower()
        )

        start = max(
            0,
            position - 3000
        )

        end = min(
            len(html),
            position + 5000
        )

        snippet = html[start:end]

        format_name = detect_format(
            snippet
        )

        key = (
            url,
            format_name,
        )

        if key in seen:
            continue

        seen.add(key)

        results.append(
            result(
                library="oakland",
                provider="Hoopla",
                format_name=format_name,
                available=True,
                wait=None,
                url=url,
            )
        )

    return results


def search_oakland(
    title,
    author,
    timeout=15,
):
    query = f"{title} {author}".strip()

    try:

        response = requests.get(
            OAKLAND_CATALOG_URL,
            params={
                "query": query,
                "searchType": "smart",
            },
            headers=HEADERS,
            timeout=timeout,
        )

        print(
            f"oakland catalog: "
            f"{response.status_code} "
            f"{len(response.text)} bytes"
        )

    except Exception as error:

        print(
            f"Oakland ERROR: {error}"
        )

        return []

    if response.status_code >= 400:
        return []

    return _extract_oakland_hoopla(
        response.text,
        title,
        author,
    )


# ----------------------------------------------------------------------
# PUBLIC SEARCH FUNCTION
# ----------------------------------------------------------------------

def search(
    title,
    author,
    timeout=15,
):
    """
    Main search entry point used by app.py.

    IMPORTANT:

    Oakland:
        - Oakland catalog / Oakland Hoopla

    Berkeley:
        - Berkeley Libby
        - Berkeley Hoopla

    We do NOT search Hoopla independently for Oakland and Berkeley.
    Hoopla is tied to the user's Berkeley account.

    Libby is searched independently because the user can have
    multiple library cards/accounts in Libby.
    """

    if not title:
        return []

    all_results = []

    # --------------------------------------------------------------
    # Oakland catalog / Oakland Hoopla
    # --------------------------------------------------------------

    try:
        all_results.extend(
            search_oakland(
                title,
                author,
                timeout=timeout,
            )
        )
    except Exception as error:
        print(
            f"Oakland search ERROR: {error}"
        )

    # --------------------------------------------------------------
    # Berkeley Libby
    # --------------------------------------------------------------

    try:
        all_results.extend(
            search_berkeley_libby(
                title,
                author,
                timeout=timeout,
            )
        )
    except Exception as error:
        print(
            f"Berkeley Libby ERROR: {error}"
        )

    # --------------------------------------------------------------
    # Berkeley Hoopla
    #
    # This is intentionally ONLY one Hoopla search.
    # --------------------------------------------------------------

    try:
        all_results.extend(
            search_berkeley_hoopla(
                title,
                author,
                timeout=timeout,
            )
        )
    except Exception as error:
        print(
            f"Berkeley Hoopla ERROR: {error}"
        )

    # --------------------------------------------------------------
    # Deduplicate final results.
    # --------------------------------------------------------------

    deduped = []
    seen = set()

    for item in all_results:

        key = (
            getattr(item, "library", None),
            getattr(item, "provider", None),
            getattr(item, "format", None),
            getattr(item, "url", None),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(item)

    return deduped