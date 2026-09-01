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

# Restrict subdomains to safe characters since they're interpolated into request URLs
SUBDOMAIN_RE = re.compile(r"^[a-zA-Z0-9-]+$")

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
    holds=None,
    wait_weeks=None,
):
    return LibraryResult(
        library=library,
        provider=provider,
        format=format_name,
        available=available,
        wait=wait,
        url=url,
        holds=holds,
        wait_weeks=wait_weeks,
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

    Returns a tuple: (display_text, estimated_weeks). estimated_weeks is
    None when no explicit duration was found in the text (e.g. a bare
    "Join waitlist" link with no stated wait time).
    """

    lowered = text.lower()

    patterns = [
        (r"(\d+)[\s-]*week[s]?\s*(?:wait|hold)", "week"),
        (r"(\d+)[\s-]*week[s]?\s*(?:waitlist|waiting list)", "week"),
        (r"(\d+)[\s-]*day[s]?\s*(?:wait|hold)", "day"),
        (r"(\d+)[\s-]*month[s]?\s*(?:wait|hold)", "month"),
    ]

    for pattern, unit in patterns:

        match = re.search(
            pattern,
            lowered
        )

        if match:
            number = int(match.group(1))

            if unit == "week":
                return f"{number}-week wait", number

            if unit == "day":
                weeks = max(1, round(number / 7))
                return f"{number}-day wait", weeks

            if unit == "month":
                weeks = round(number * 4.345)
                return f"{number}-month wait", weeks

    if (
        "join waitlist" in lowered
        or "join the waitlist" in lowered
        or "place hold" in lowered
        or "place a hold" in lowered
        or "wait list" in lowered
        or "waiting list" in lowered
    ):
        return "Waitlist", None

    return None, None


def _extract_holds(text):
    """
    Attempt to identify a hold/waitlist count from surrounding HTML text.

    Handles common phrasings such as:
        "12 people are waiting for 3 copies"
        "12 patrons waiting"
        "12 holds"
        "Holds: 12"
    """

    lowered = text.lower()

    patterns = [
        r"(\d+)\s*(?:people|patrons?|users?|holds?)\s*(?:are\s*|is\s*)?waiting",
        r"(\d+)\s*holds?\b",
        r"holds?\s*[:\-]?\s*(\d+)",
        r"(\d+)\s*(?:on\s*)?(?:the\s*)?wait\s*list",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            lowered
        )

        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                continue

    return None


def _extract_holds_from_item(item):
    """
    Look for a hold/waitlist count in common OverDrive/Libby JSON field
    names. Field naming varies across OverDrive API versions, so we check
    several plausible keys rather than assuming one.
    """

    if not isinstance(item, dict):
        return None

    for key in (
        "holdsCount",
        "holds_count",
        "numberOfHolds",
        "numHolds",
        "waitlistSize",
        "waitListSize",
        "estimatedHolds",
    ):
        value = item.get(key)

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)

    return None


def _extract_wait_weeks_from_item(item):
    """
    Look for an explicit wait-time estimate in common OverDrive/Libby
    JSON field names, converting days to weeks when that's the unit used.
    """

    if not isinstance(item, dict):
        return None

    for key in ("estimatedWaitDays", "estimated_wait_days"):
        value = item.get(key)

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(1, round(value / 7))

    for key in ("estimatedWaitWeeks", "estimated_wait_weeks"):
        value = item.get(key)

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(value)

    return None


def _detect_libby_availability(item, html):
    """
    Determine availability conservatively.

    Important:
    existence of a Libby title does NOT mean it is
    immediately borrowable.

    Returns a 4-tuple: (available, wait_text, holds, wait_weeks).
    holds and wait_weeks are None when no such data could be found.
    """

    text = json.dumps(item).lower()

    # Prefer structured JSON fields when OverDrive's response includes
    # them; fall back to scraping the surrounding HTML text otherwise.
    holds = _extract_holds_from_item(item)
    wait_weeks = _extract_wait_weeks_from_item(item)

    # Explicit availability flags in OverDrive data.
    for key in (
        "isAvailable",
        "available",
        "isAvailableNow",
    ):
        value = item.get(key)

        if isinstance(value, bool):
            if value:
                return True, None, None, None

            wait_text, text_weeks = _extract_wait(text)

            if not wait_text:
                wait_text, html_weeks = _extract_wait(html)
                if text_weeks is None:
                    text_weeks = html_weeks

            if wait_weeks is None:
                wait_weeks = text_weeks

            if holds is None:
                holds = _extract_holds(html)

            return False, wait_text or "Waitlist", holds, wait_weeks

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
        return True, None, None, None

    # If the surrounding HTML clearly says there is a hold/wait,
    # do not call it available.
    wait_text, text_weeks = _extract_wait(html)

    if wait_weeks is None:
        wait_weeks = text_weeks

    if holds is None:
        holds = _extract_holds(html)

    if wait_text:
        return False, wait_text, holds, wait_weeks

    lowered = html.lower()

    # Strong indications of an immediate checkout.
    if (
        "borrow" in lowered
        or "borrow now" in lowered
        or "available now" in lowered
    ):
        return True, None, None, None

    # We don't know.
    return False, None, holds, wait_weeks


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

        availability, wait, holds, wait_weeks = (
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
                holds=holds,
                wait_weeks=wait_weeks,
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
        wait, wait_weeks = _extract_wait(snippet)
        holds = _extract_holds(snippet)

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
                holds=holds,
                wait_weeks=wait_weeks,
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

    Note: Results are tagged with library="hoopla" because Hoopla is a
    shared catalog, not tied to any specific library. This ensures they
    display correctly in the frontend regardless of which library's search
    found them.
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
                library="hoopla",
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
# GENERIC CATALOG SEARCH (any Bibliocommons / OverDrive library)
#
# Oakland and Berkeley are themselves just presets of these generic
# functions. Any library running the same catalog software works too.
# ----------------------------------------------------------------------

def search_bibliocommons(subdomain, library_key, title, author, timeout=15):
    """Search a Bibliocommons-powered catalog (same platform as Oakland)."""
    if not SUBDOMAIN_RE.match(subdomain or ""):
        print(f"Bibliocommons ERROR: invalid subdomain '{subdomain}'")
        return []

    query = f"{title} {author}".strip()
    catalog_url = f"https://{subdomain}.bibliocommons.com/v2/search"

    try:
        response = requests.get(
            catalog_url,
            params={"query": query, "searchType": "smart"},
            headers=HEADERS,
            timeout=timeout,
        )
        print(f"{library_key} catalog: {response.status_code} {len(response.text)} bytes")
    except Exception as error:
        print(f"{library_key} catalog ERROR: {error}")
        return []

    if response.status_code >= 400:
        return []

    results = _extract_oakland_hoopla(response.text, title, author)

    # Retag non-Hoopla results with the requested library key.
    # Hoopla results already have library="hoopla" and should not be changed.
    return [
        result(
            library=r.library if r.provider == "Hoopla" else library_key,
            provider=r.provider,
            format_name=r.format,
            available=r.available,
            wait=r.wait,
            url=r.url,
            holds=r.holds,
            wait_weeks=r.wait_weeks,
        )
        for r in results
    ]


def search_overdrive_libby(subdomain, library_key, title, author, timeout=15):
    """Search an OverDrive/Libby-powered catalog (same platform as Berkeley)."""
    if not SUBDOMAIN_RE.match(subdomain or ""):
        print(f"OverDrive ERROR: invalid subdomain '{subdomain}'")
        return []

    query = f"{title} {author}".strip()
    base_url = f"https://{subdomain}.overdrive.com"
    search_url = f"{base_url}/search"

    try:
        response = requests.get(
            search_url,
            params={"query": query},
            headers=HEADERS,
            timeout=timeout,
        )
        print(f"{library_key} Libby: {response.status_code} {len(response.text)} bytes")
    except Exception as error:
        print(f"{library_key} Libby ERROR: {error}")
        return []

    if response.status_code >= 400:
        return []

    html = response.text

    title_collection = _extract_js_object(html, "titleCollection")
    media_items = _extract_js_object(html, "mediaItems")

    if isinstance(title_collection, list):
        print(f"{library_key} Libby: titleCollection has {len(title_collection)} objects")

    if isinstance(media_items, dict):
        print(f"{library_key} Libby: mediaItems has {len(media_items)} objects")

    candidates = []
    if isinstance(title_collection, list):
        candidates.extend(title_collection)
    if isinstance(media_items, dict):
        candidates.extend(media_items.values())

    results = []
    seen = set()

    for item in candidates:
        if not isinstance(item, dict):
            continue

        item_title = _libby_title(item)
        item_author = _libby_author(item)

        combined = f"{item_title} {item_author} {json.dumps(item)}"

        if not title_matches(combined, title, author):
            continue

        if item_title and not title_matches(item_title, title, author):
            continue

        format_name = _libby_format(item)
        url = _libby_url(item, base_url)

        if not url:
            continue

        availability, wait, holds, wait_weeks = _detect_libby_availability(item, html)

        key = (url, format_name)
        if key in seen:
            continue
        seen.add(key)

        print(
            f"{library_key} Libby MATCH:", item_title or title, "/", format_name, "/",
            "AVAILABLE" if availability else wait or "STATUS UNKNOWN",
        )

        results.append(
            result(
                library=library_key,
                provider="Libby",
                format_name=format_name,
                available=availability,
                wait=wait,
                url=url,
                holds=holds,
                wait_weeks=wait_weeks,
            )
        )

    if not results:
        print(f"{library_key} Libby: no specific match for {title}")

    return results


def search_hoopla(library_key, title, author, timeout=15):
    """Search Hoopla's shared catalog (not tied to a specific library's domain)."""
    query = f"{title} {author}".strip()

    try:
        response = requests.get(
            HOOPLA_SEARCH_URL,
            params={"q": query},
            headers=HEADERS,
            timeout=timeout,
        )
        print(f"Hoopla: {response.status_code} {len(response.text)} bytes")
    except Exception as error:
        print(f"Hoopla ERROR: {error}")
        return []

    if response.status_code >= 400:
        return []

    html = response.text
    urls = _find_hoopla_candidates(html, title, author)

    if not urls:
        print("Hoopla: no title URLs found for", title)
        return []

    results = []
    seen = set()

    for url in urls:
        position = html.lower().find(url.lower())
        start = max(0, position - 3000)
        end = min(len(html), position + 5000)
        snippet = html[start:end]

        format_name = detect_format(snippet)
        wait, wait_weeks = _extract_wait(snippet)
        holds = _extract_holds(snippet)
        available = wait is None

        key = (url, format_name)
        if key in seen:
            continue
        seen.add(key)

        print("Hoopla MATCH:", title, "/", format_name, "/", "AVAILABLE" if available else wait)

        results.append(
            result(
                library=library_key,
                provider="Hoopla",
                format_name=format_name,
                available=available,
                wait=wait,
                url=url,
                holds=holds,
                wait_weeks=wait_weeks,
            )
        )

    return results


def search_libraries(title, author, library_configs, timeout=15):
    """
    Generic multi-library search entry point.

    library_configs is a list of dicts, each shaped like:
        {"key": "oakland", "bibliocommons": "oaklandlibrary", "hoopla": True}
        {"key": "berkeley", "overdrive": "berkeleypubliclibrary"}
        {"key": "redwood_city", "bibliocommons": "rcpl"}

    Hoopla's catalog is shared/global, so it's only searched once
    even if multiple configs request it.
    """
    if not title or not library_configs:
        return []

    all_results = []
    hoopla_searched = False

    for config in library_configs:
        key = config.get("key") or "library"

        if config.get("bibliocommons"):
            try:
                all_results.extend(
                    search_bibliocommons(config["bibliocommons"], key, title, author, timeout=timeout)
                )
            except Exception as error:
                print(f"{key} Bibliocommons ERROR: {error}")

        if config.get("overdrive"):
            try:
                all_results.extend(
                    search_overdrive_libby(config["overdrive"], key, title, author, timeout=timeout)
                )
            except Exception as error:
                print(f"{key} OverDrive ERROR: {error}")

        if config.get("hoopla") and not hoopla_searched:
            try:
                all_results.extend(search_hoopla(key, title, author, timeout=timeout))
                hoopla_searched = True
            except Exception as error:
                print(f"Hoopla ERROR: {error}")

    # Deduplicate final results
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