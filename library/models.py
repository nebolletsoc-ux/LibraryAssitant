from dataclasses import dataclass

@dataclass
class LibraryResult:
    library: str
    provider: str
    format: str
    available: bool
    wait: str | None = None
    url: str | None = None
    holds: int | None = None
    wait_weeks: float | None = None
    language: str | None = None

