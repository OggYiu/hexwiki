"""Validated survey inventories that drive every model-written wiki note.

The survey is deliberately separate from drafting.  It enumerates the source's
functional units into small JSON artifacts, this module validates and freezes
that inventory, and later stages write exactly the notes the inventory names.
That makes silent under-coverage observable instead of trusting model memory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .audit import atomic_json
from .profile import sha256_json


SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ORDERED_SLUG_RE = re.compile(r"^\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$")
CONCEPT_KINDS = {"substantive", "methodological", "epistemic"}
AUTHOR_GROUP = "author"

OUTLINE_FILE = "outline.json"
CONCEPTS_FILE = "concepts.json"
ROSTER_FILE = "roster.json"
FROZEN_FILE = "inventory.json"

FLOOR_KEYS = {
    "case_dossiers": "episodes",
    "concept_notes": "concepts",
    "section_notes": "sections",
    "claims": "claims",
    "motifs": "motifs",
}


class PlanError(ValueError):
    """A survey artifact cannot safely drive compilation."""


OUTLINE_SCHEMA = """{
  "chapter": {
    "number": <positive int>,
    "slug": "<nn>-<kebab-title>",
    "title": "<the scoped division's title>",
    "organizing_question": "<one sentence>",
    "argument_steps": ["<ordered inference step>", ...],
    "establishes": ["<supported result>", ...],
    "leaves_open": ["<unresolved question>", ...]
  },
  "sections": [{
    "order": <positive int>,
    "slug": "<nn>-<kebab-title>",
    "title": "<source title or faithful descriptive title>",
    "pdf_pages": [<int>, ...],
    "summary": "<what this section does in the argument>"
  }, ...]
}"""

CONCEPTS_SCHEMA = """{
  "concepts": [{
    "slug": "<kebab-slug>",
    "title": "<short noun phrase>",
    "kind": "substantive|methodological|epistemic",
    "pdf_pages": [<int>, ...],
    "summary": "<what the idea is and what work it does>"
  }, ...]
}"""

ROSTER_SCHEMA = """{
  "people": [{
    "name": "<name exactly as the source gives it>",
    "group": "author|<kebab role group>",
    "pdf_pages": [<int>, ...],
    "role": "<role in this scope>",
    "caution": "<source-reading caution>"
  }, ...],
  "claims": [{
    "claim": "<substantive claim>",
    "owner": "<who makes it>",
    "evidence": "<in-scope evidence>",
    "limit": "<evidential limit>"
  }, ...],
  "motifs": ["<feature recurring across episodes>", ...]
}"""

EPISODES_SCHEMA = """{
  "episodes": [{
    "slug": "<kebab-slug>",
    "title": "<short descriptive title>",
    "pdf_pages": [<int>, ...],
    "summary": "<what is narrated>",
    "support": "<support present and absent>",
    "section_order": <positive int>
  }, ...]
}"""


def episodes_file(part: int) -> str:
    if isinstance(part, bool) or not isinstance(part, int) or part < 1:
        raise ValueError("episode part must be a positive integer")
    return f"episodes-{part}.json"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _slug_ok(value: Any, *, ordered: bool = False) -> bool:
    pattern = ORDERED_SLUG_RE if ordered else SLUG_RE
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _pages_ok(values: Any, scope: set[int]) -> bool:
    return (
        isinstance(values, list)
        and bool(values)
        and all(
            not isinstance(page, bool) and isinstance(page, int) and page in scope
            for page in values
        )
        and values == sorted(set(values))
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PlanError(f"{label} produced no file at {path}")
    text = path.read_text(encoding="utf-8-sig").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(f"{label} output is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise PlanError(f"{label} output must be one JSON object")
    return value


def floor(profile: dict[str, Any], key: str) -> int | None:
    """Return one declared architecture floor, preserving an intentional null."""
    if key not in FLOOR_KEYS:
        raise KeyError(key)
    minimums = profile["architecture"]["minimums"]
    value = minimums[key]
    return None if value is None else int(value)


def count_clause(
    profile: dict[str, Any],
    key: str,
    *,
    floor_text: str,
    open_text: str,
) -> str:
    value = floor(profile, key)
    return open_text if value is None else floor_text.format(n=value)


def load_inventory(
    plan_dir: Path,
    profile: dict[str, Any],
    episode_parts: int,
) -> dict[str, Any]:
    """Merge the independently generated survey parts and validate the result."""
    data = _load_json(plan_dir / OUTLINE_FILE, "survey-outline")
    data["concepts"] = _load_json(
        plan_dir / CONCEPTS_FILE, "survey-concepts"
    ).get("concepts")
    data.update(_load_json(plan_dir / ROSTER_FILE, "survey-roster"))
    episodes: list[Any] = []
    for part in range(1, episode_parts + 1):
        value = _load_json(
            plan_dir / episodes_file(part), f"survey-episodes-{part}"
        ).get("episodes")
        _require(
            isinstance(value, list),
            f"survey-episodes-{part} has no episodes list",
        )
        episodes.extend(value)
    data["episodes"] = episodes
    return validate_inventory(data, profile)


def _validate_floor(profile: dict[str, Any], key: str, items: list[Any]) -> None:
    minimum = floor(profile, key)
    if minimum is not None:
        _require(
            len(items) >= minimum,
            f"inventory.{FLOOR_KEYS[key]} needs at least {minimum} entries",
        )


def validate_inventory(data: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Validate every field later write stages rely on and return normalized data."""
    _require(isinstance(data, dict), "inventory must be a JSON object")
    required = {"chapter", "sections", "episodes", "concepts", "people", "claims", "motifs"}
    _require(not (required - set(data)), f"inventory is missing {sorted(required - set(data))}")
    scope = {
        int(page)
        for page in profile["primary_pages"] + profile["apparatus_pages"]
    }

    chapter = data["chapter"]
    _require(isinstance(chapter, dict), "inventory.chapter must be an object")
    _require(
        isinstance(chapter.get("number"), int)
        and not isinstance(chapter.get("number"), bool)
        and chapter["number"] >= 1,
        "inventory.chapter.number must be a positive integer",
    )
    for field in ("title", "organizing_question"):
        _require(_nonempty(chapter.get(field)), f"inventory.chapter.{field} is empty")
    _require(
        _slug_ok(chapter.get("slug"), ordered=True),
        f"inventory.chapter.slug must look like '01-some-title', got {chapter.get('slug')!r}",
    )
    steps = chapter.get("argument_steps")
    _require(
        isinstance(steps, list)
        and len(steps) >= 4
        and all(_nonempty(item) for item in steps),
        "inventory.chapter.argument_steps needs at least four non-empty steps",
    )
    for field in ("establishes", "leaves_open"):
        value = chapter.get(field)
        _require(
            isinstance(value, list) and bool(value) and all(_nonempty(item) for item in value),
            f"inventory.chapter.{field} must be a non-empty string list",
        )

    sections = data["sections"]
    _require(isinstance(sections, list) and bool(sections), "inventory.sections is empty")
    _validate_floor(profile, "section_notes", sections)
    section_orders: set[int] = set()
    section_slugs: set[str] = set()
    for item in sections:
        _require(isinstance(item, dict), "each section must be an object")
        order = item.get("order")
        _require(
            isinstance(order, int) and not isinstance(order, bool) and order >= 1,
            "section.order must be a positive integer",
        )
        _require(order not in section_orders, f"duplicate section order {order}")
        section_orders.add(order)
        slug = item.get("slug")
        _require(_slug_ok(slug, ordered=True), f"bad section slug {slug!r}")
        _require(slug not in section_slugs, f"duplicate section slug {slug}")
        section_slugs.add(slug)
        _require(_nonempty(item.get("title")), f"section {slug} has no title")
        _require(_nonempty(item.get("summary")), f"section {slug} has no summary")
        _require(
            _pages_ok(item.get("pdf_pages"), scope),
            f"section {slug} has invalid or out-of-scope pdf_pages",
        )

    episodes = data["episodes"]
    _require(isinstance(episodes, list) and bool(episodes), "inventory.episodes is empty")
    _validate_floor(profile, "case_dossiers", episodes)
    episode_slugs: set[str] = set()
    for item in episodes:
        _require(isinstance(item, dict), "each episode must be an object")
        slug = item.get("slug")
        _require(_slug_ok(slug), f"bad episode slug {slug!r}")
        _require(slug not in episode_slugs, f"duplicate episode slug {slug}")
        episode_slugs.add(slug)
        for field in ("title", "summary", "support"):
            _require(_nonempty(item.get(field)), f"episode {slug}.{field} is empty")
        _require(
            _pages_ok(item.get("pdf_pages"), scope),
            f"episode {slug} has invalid or out-of-scope pdf_pages",
        )
        _require(
            item.get("section_order") in section_orders,
            f"episode {slug}.section_order does not name a surveyed section",
        )

    concepts = data["concepts"]
    _require(isinstance(concepts, list) and bool(concepts), "inventory.concepts is empty")
    _validate_floor(profile, "concept_notes", concepts)
    concept_slugs: set[str] = set()
    concept_kinds: set[str] = set()
    for item in concepts:
        _require(isinstance(item, dict), "each concept must be an object")
        slug = item.get("slug")
        _require(_slug_ok(slug), f"bad concept slug {slug!r}")
        _require(slug not in concept_slugs, f"duplicate concept slug {slug}")
        _require(slug not in episode_slugs, f"concept slug {slug} collides with an episode")
        concept_slugs.add(slug)
        _require(_nonempty(item.get("title")), f"concept {slug}.title is empty")
        _require(_nonempty(item.get("summary")), f"concept {slug}.summary is empty")
        kind = item.get("kind")
        _require(kind in CONCEPT_KINDS, f"concept {slug}.kind is invalid")
        concept_kinds.add(kind)
        _require(
            _pages_ok(item.get("pdf_pages"), scope),
            f"concept {slug} has invalid or out-of-scope pdf_pages",
        )
    _require(
        concept_kinds == CONCEPT_KINDS,
        "inventory.concepts must cover substantive, methodological, and epistemic kinds",
    )

    people = data["people"]
    _require(isinstance(people, list) and bool(people), "inventory.people is empty")
    author_names: list[str] = []
    for item in people:
        _require(isinstance(item, dict), "each person must be an object")
        for field in ("name", "role", "caution"):
            _require(_nonempty(item.get(field)), f"person.{field} is empty")
        group = item.get("group")
        _require(_slug_ok(group), f"bad person group {group!r}")
        if group == AUTHOR_GROUP:
            author_names.append(str(item["name"]).strip())
        _require(
            _pages_ok(item.get("pdf_pages"), scope),
            f"person {item.get('name')} has invalid or out-of-scope pdf_pages",
        )
    _require(len(author_names) == 1, "inventory.people must contain exactly one author")
    _require(
        author_names[0].casefold() == str(profile["document_author"]).strip().casefold(),
        "inventory author does not match the document profile",
    )

    claims = data["claims"]
    _require(isinstance(claims, list) and bool(claims), "inventory.claims is empty")
    _validate_floor(profile, "claims", claims)
    for item in claims:
        _require(isinstance(item, dict), "each claim must be an object")
        for field in ("claim", "owner", "evidence", "limit"):
            _require(_nonempty(item.get(field)), f"claim.{field} is empty")

    motifs = data["motifs"]
    _require(
        isinstance(motifs, list) and bool(motifs) and all(_nonempty(item) for item in motifs),
        "inventory.motifs must be a non-empty string list",
    )
    _validate_floor(profile, "motifs", motifs)

    normalized = dict(data)
    normalized["sections"] = sorted(sections, key=lambda item: item["order"])
    return normalized


def freeze_inventory(plan_dir: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    """Write the accepted aggregate exactly once with its canonical digest."""
    path = plan_dir / FROZEN_FILE
    if path.exists():
        raise FileExistsError(path)
    record = {"inventory_sha256": sha256_json(inventory), "inventory": inventory}
    atomic_json(path, record)
    return record


def narrow_for_smoke(plan_dir: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    """Create a production-shaped small inventory without mutating the frozen plan."""
    narrowed = json.loads(json.dumps(inventory))
    narrowed["sections"] = narrowed["sections"][:1]
    kept_order = narrowed["sections"][0]["order"]
    matching = [
        item for item in narrowed["episodes"] if item["section_order"] == kept_order
    ]
    narrowed["episodes"] = (matching or narrowed["episodes"])[:2]
    by_kind = {
        kind: next(item for item in narrowed["concepts"] if item["kind"] == kind)
        for kind in sorted(CONCEPT_KINDS)
    }
    narrowed["concepts"] = list(by_kind.values())
    authors = [item for item in narrowed["people"] if item["group"] == AUTHOR_GROUP]
    others = [item for item in narrowed["people"] if item["group"] != AUTHOR_GROUP]
    first_group = others[0]["group"] if others else None
    narrowed["people"] = authors + [
        item for item in others if item["group"] == first_group
    ][:4]
    atomic_json(
        plan_dir / "smoke-inventory.json",
        {"inventory_sha256": sha256_json(narrowed), "inventory": narrowed},
    )
    return narrowed


def role_groups(inventory: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for person in inventory["people"]:
        grouped.setdefault(str(person["group"]), []).append(person)
    return grouped


def person_slug(name: str, profile: dict[str, Any]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return f"{base}-in-{profile['scope_id']}"


def author_note_paths(
    inventory: dict[str, Any], profile: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"people/{person_slug(item['name'], profile)}.md", item)
        for item in role_groups(inventory).get(AUTHOR_GROUP, [])
    ]


def expected_notes(inventory: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    expected = {
        "overview.md": "scope overview",
        "reading-guide.md": "reading guide",
        f"chapters/{inventory['chapter']['slug']}.md": "scope hub",
        "synthesis/argument-map.md": "argument map",
        "synthesis/claim-evidence-matrix.md": "claim-evidence matrix",
        "synthesis/motif-matrix.md": "motif matrix",
        "synthesis/critical-reading.md": "critical reading",
        "synthesis/open-questions.md": "open questions",
    }
    for item in inventory["sections"]:
        expected[f"sections/{item['slug']}.md"] = f"section: {item['title']}"
    for item in inventory["episodes"]:
        expected[f"cases/{item['slug']}.md"] = f"episode: {item['title']}"
    for item in inventory["concepts"]:
        expected[f"concepts/{item['slug']}.md"] = f"concept: {item['title']}"
    for group, members in role_groups(inventory).items():
        if group == AUTHOR_GROUP:
            for relative, person in author_note_paths(inventory, profile):
                expected[relative] = f"author in scope: {person['name']}"
        else:
            expected[f"people/{group}.md"] = f"role roster: {group} ({len(members)} named)"
    return expected


def missing_notes(
    wiki_dir: Path,
    inventory: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, str]:
    return {
        relative: reason
        for relative, reason in expected_notes(inventory, profile).items()
        if not (wiki_dir / relative).is_file()
    }


def batches(items: list[Any], size: int) -> list[list[Any]]:
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError("batch size must be a positive integer")
    return [items[offset : offset + size] for offset in range(0, len(items), size)]
