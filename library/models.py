from dataclasses import dataclass

@dataclass
class LibraryResult:
    library: str
    provider: str
    format: str
    available: bool
    wait: str | None = None
    url: str | None = None

