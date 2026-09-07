from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

COURSE_HEADING = "## Course à Pied"


@dataclass(frozen=True)
class SimpleEndpoint:
    endpoint: str
    section_heading: str | None
    source_dir: Path


SIMPLE_ENDPOINTS = (
    ("notes", None),
    ("course", COURSE_HEADING),
)


def endpoint_candidates(anchor: Path, endpoint: str) -> list[Path]:
    candidates: list[Path] = []
    if anchor.name == endpoint:
        candidates.append(anchor)
    candidates.append(anchor / endpoint)
    candidates.append(anchor.parent / endpoint)
    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)
    return unique_candidates


def resolve_endpoint_dir(endpoint: str, *anchors: Path) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for anchor in anchors:
        for candidate in endpoint_candidates(anchor, endpoint):
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if not candidates:
        raise RuntimeError(
            f"No directory candidates available for endpoint: {endpoint}"
        )
    return candidates[0]


def load_simple_endpoints(*anchors: Path) -> list[SimpleEndpoint]:
    return [
        SimpleEndpoint(
            endpoint=endpoint,
            section_heading=section_heading,
            source_dir=resolve_endpoint_dir(endpoint, *anchors),
        )
        for endpoint, section_heading in SIMPLE_ENDPOINTS
    ]


def resolve_simple_endpoint_dirs(*anchors: Path) -> dict[str, Path]:
    return {
        endpoint.endpoint: endpoint.source_dir
        for endpoint in load_simple_endpoints(*anchors)
    }
